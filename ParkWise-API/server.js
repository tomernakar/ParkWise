const express = require('express');
const http = require('http');
const { Server } = require('socket.io');
const mysql = require('mysql2/promise');
const cors = require('cors');
const path = require('path');
const bcrypt = require('bcryptjs');
const jwt = require('jsonwebtoken');
const { OAuth2Client } = require('google-auth-library');
require('dotenv').config();

const GOOGLE_CLIENT_ID = process.env.GOOGLE_CLIENT_ID || '';

// Emails granted the `admin` role automatically (comma-separated). These users
// are promoted on server start and again whenever they authenticate, so the
// first admin can be bootstrapped with zero manual SQL.
const ADMIN_EMAILS = (process.env.ADMIN_EMAILS || '')
  .split(',')
  .map(e => e.trim().toLowerCase())
  .filter(Boolean);

// Canonical lot the demo runs on: the vision pipeline (ParkWise-Vision/zones.json)
// posts here and the user app fetches it. Reconciles the old demo_lot_1 /
// parkwise_demo / campus split onto a single id.
const DEFAULT_LOT_ID = process.env.DEFAULT_LOT_ID || 'demo_lot_1';

// App reservations auto-expire so a spot isn't held forever if the driver
// never arrives (the CV detector clears it sooner when the car is seen).
const RESERVATION_TTL_MS = (parseInt(process.env.RESERVATION_TTL_MIN, 10) || 15) * 60 * 1000;

function isAdminEmail(email) {
  return ADMIN_EMAILS.includes((email || '').trim().toLowerCase());
}

const app = express();
const server = http.createServer(app);
const io = new Server(server, { cors: { origin: '*' } });
const PORT = process.env.PORT || 3000;
const JWT_SECRET = process.env.JWT_SECRET || 'parkwise-secret-change-in-production';

app.use(cors());
app.use(express.json());
// Admin dashboard (desktop) — mounted before the catch-all static roots so
// /admin/* resolves to the dashboard rather than the phone app.
app.use('/admin', express.static(path.join(__dirname, '..', 'ParkWise-Admin')));
app.use(express.static(path.join(__dirname, '..', 'ParkWise-Vision', 'ParkWise')));
app.use(express.static(path.join(__dirname, 'public')));

const pool = mysql.createPool({
  host:     process.env.DB_HOST     || 'localhost',
  user:     process.env.DB_USER     || 'root',
  password: process.env.DB_PASSWORD || '',
  database: process.env.DB_NAME     || 'parkwise',
  waitForConnections: true,
  connectionLimit: 10,
});

// Add a column only if it's missing — MySQL lacks ADD COLUMN IF NOT EXISTS, so
// check information_schema first. Keeps existing installs migrating cleanly.
async function ensureColumn(conn, table, column, definition) {
  const [rows] = await conn.query(
    `SELECT 1 FROM information_schema.columns
     WHERE table_schema = DATABASE() AND table_name = ? AND column_name = ?`,
    [table, column]
  );
  if (!rows.length) {
    await conn.query(`ALTER TABLE \`${table}\` ADD COLUMN ${column} ${definition}`);
    console.log(`Migrated: added ${table}.${column}`);
  }
}

// ── DB init: create tables if they don't exist ────────────────────────────────
async function initDB() {
  const conn = await pool.getConnection();
  try {
    await conn.query(`
      CREATE TABLE IF NOT EXISTS users (
        id         INT AUTO_INCREMENT PRIMARY KEY,
        name       VARCHAR(100) NOT NULL,
        email      VARCHAR(150) NOT NULL UNIQUE,
        phone      VARCHAR(40)  DEFAULT '',
        password   VARCHAR(255),
        google     TINYINT(1)   DEFAULT 0,
        role       VARCHAR(20)  DEFAULT 'user',
        created_at TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
      )
    `);
    // Migrate pre-existing users tables that predate the role column.
    await ensureColumn(conn, 'users', 'role', "VARCHAR(20) DEFAULT 'user'");

    await conn.query(`
      CREATE TABLE IF NOT EXISTS parking_sessions (
        id           INT AUTO_INCREMENT PRIMARY KEY,
        user_id      INT NOT NULL,
        spot_id      VARCHAR(20),
        lot_id       VARCHAR(50)  DEFAULT 'demo_lot_1',
        vehicle_type VARCHAR(30)  DEFAULT 'regular',
        duration     VARCHAR(20),
        started_at   BIGINT,
        ended_at     BIGINT,
        created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
      )
    `);
    await conn.query(`
      CREATE TABLE IF NOT EXISTS parking_spots (
        lot_id     VARCHAR(50)  NOT NULL,
        spot_id    VARCHAR(20)  NOT NULL,
        status     VARCHAR(20)  DEFAULT 'free',
        score      FLOAT        DEFAULT 0,
        out_of_service TINYINT(1) DEFAULT 0,
        reserved_by INT          DEFAULT NULL,
        reserved_at BIGINT       DEFAULT NULL,
        updated_at TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (lot_id, spot_id)
      )
    `);
    // Admin "out of service" flag — survives CV updates (CV only writes status/score).
    await ensureColumn(conn, 'parking_spots', 'out_of_service', 'TINYINT(1) DEFAULT 0');
    // App reservations (driver assigned a spot, car not yet detected).
    await ensureColumn(conn, 'parking_spots', 'reserved_by', 'INT DEFAULT NULL');
    await ensureColumn(conn, 'parking_spots', 'reserved_at', 'BIGINT DEFAULT NULL');
    // Lots — let admins manage real lot metadata instead of hard-coding it.
    await conn.query(`
      CREATE TABLE IF NOT EXISTS parking_lots (
        lot_id      VARCHAR(50)  PRIMARY KEY,
        name        VARCHAR(150) NOT NULL,
        address     VARCHAR(255) DEFAULT '',
        total_spots INT          DEFAULT 0,
        created_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
      )
    `);
    // Issue reports raised from the user app (today they go nowhere).
    await conn.query(`
      CREATE TABLE IF NOT EXISTS reports (
        id          INT AUTO_INCREMENT PRIMARY KEY,
        user_id     INT          DEFAULT NULL,
        lot_id      VARCHAR(50)  DEFAULT 'demo_lot_1',
        spot_id     VARCHAR(20)  DEFAULT '',
        issue_type  VARCHAR(40)  NOT NULL,
        note        VARCHAR(500) DEFAULT '',
        status      VARCHAR(20)  DEFAULT 'open',
        created_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
        resolved_at TIMESTAMP    NULL DEFAULT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
      )
    `);

    // Seed the canonical demo lot so lot management has something to show.
    await conn.query(
      `INSERT INTO parking_lots (lot_id, name, address, total_spots)
       VALUES (?, ?, ?, ?)
       ON DUPLICATE KEY UPDATE lot_id = lot_id`,
      [DEFAULT_LOT_ID, 'Exelerator Demo Lot', 'Exelerator Campus, Level 1', 27]
    );

    // Bootstrap admins from ADMIN_EMAILS for users that already exist.
    if (ADMIN_EMAILS.length) {
      const [res] = await conn.query(
        `UPDATE users SET role = 'admin' WHERE email IN (?) AND role <> 'admin'`,
        [ADMIN_EMAILS]
      );
      if (res.affectedRows) console.log(`Promoted ${res.affectedRows} user(s) to admin`);
    }

    console.log('DB tables ready');
  } finally {
    conn.release();
  }
}

// ── Auth helpers ──────────────────────────────────────────────────────────────
function signToken(user) {
  return jwt.sign({ id: user.id, email: user.email }, JWT_SECRET, { expiresIn: '90d' });
}

function authMiddleware(req, res, next) {
  const header = req.headers.authorization || '';
  const token = header.startsWith('Bearer ') ? header.slice(7) : null;
  if (!token) return res.status(401).json({ error: 'No token' });
  try {
    req.user = jwt.verify(token, JWT_SECRET);
    next();
  } catch {
    res.status(401).json({ error: 'Invalid token' });
  }
}

// Gate admin-only routes. Runs after authMiddleware and re-checks the role
// against the DB (not the token) so a demotion takes effect immediately.
async function requireAdmin(req, res, next) {
  try {
    const [rows] = await pool.query('SELECT role FROM users WHERE id = ?', [req.user.id]);
    if (!rows[0]) return res.status(401).json({ error: 'User not found' });
    if (rows[0].role !== 'admin') return res.status(403).json({ error: 'Admin access required' });
    req.user.role = 'admin';
    next();
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
}

// Attaches req.user if a valid token is present; otherwise continues as guest.
function optionalAuth(req, res, next) {
  const header = req.headers.authorization || '';
  const token = header.startsWith('Bearer ') ? header.slice(7) : null;
  if (token) { try { req.user = jwt.verify(token, JWT_SECRET); } catch {} }
  next();
}

// The single source of truth for what status the app/dashboard should show.
// Admin out-of-service wins; an unexpired reservation shows as 'reserved'
// unless the camera already reports the spot occupied.
function effectiveStatus(row, now = Date.now()) {
  if (row.out_of_service) return 'out_of_service';
  const reserved = row.reserved_by != null && row.reserved_at != null &&
    (now - Number(row.reserved_at) < RESERVATION_TTL_MS);
  if (reserved && row.status !== 'occupied') return 'reserved';
  return row.status;
}

// Normalise a parking_spots row into the shape every client consumes.
function toApiSpot(row) {
  const eff = effectiveStatus(row);
  return {
    id: row.spot_id != null ? row.spot_id : row.id,
    status: eff,
    raw_status: row.status,
    out_of_service: !!row.out_of_service,
    reserved: eff === 'reserved',
    score: row.score,
    updated_at: row.updated_at,
  };
}

// ── Config ────────────────────────────────────────────────────────────────────
app.get('/api/config', (req, res) => {
  res.json({ googleClientId: GOOGLE_CLIENT_ID });
});

// ── Auth routes ───────────────────────────────────────────────────────────────
app.post('/api/auth/register', async (req, res) => {
  try {
    const { name, email, password, phone = '' } = req.body;
    if (!name || !email || !password) return res.status(400).json({ error: 'Missing fields' });
    if (password.length < 6)          return res.status(400).json({ error: 'Password too short' });

    const cleanEmail = email.trim().toLowerCase();
    const role = isAdminEmail(cleanEmail) ? 'admin' : 'user';
    const hashed = await bcrypt.hash(password, 10);
    const [result] = await pool.query(
      'INSERT INTO users (name, email, phone, password, role) VALUES (?, ?, ?, ?, ?)',
      [name.trim(), cleanEmail, phone.trim(), hashed, role]
    );
    const user = { id: result.insertId, name: name.trim(), email: cleanEmail, phone: phone.trim(), role };
    res.json({ token: signToken(user), user });
  } catch (err) {
    if (err.code === 'ER_DUP_ENTRY') return res.status(409).json({ error: 'Email already registered' });
    console.error(err);
    res.status(500).json({ error: err.message });
  }
});

app.post('/api/auth/login', async (req, res) => {
  try {
    const { email, password } = req.body;
    if (!email || !password) return res.status(400).json({ error: 'Missing fields' });

    const [rows] = await pool.query('SELECT * FROM users WHERE email = ?', [email.trim().toLowerCase()]);
    const user = rows[0];
    if (!user) return res.status(401).json({ error: 'Wrong email or password' });

    if (user.google) return res.status(401).json({ error: 'Use Google sign-in for this account' });

    const ok = await bcrypt.compare(password, user.password);
    if (!ok) return res.status(401).json({ error: 'Wrong email or password' });

    // Self-healing admin bootstrap: promote if the email is now in ADMIN_EMAILS.
    if (isAdminEmail(user.email) && user.role !== 'admin') {
      await pool.query('UPDATE users SET role = ? WHERE id = ?', ['admin', user.id]);
      user.role = 'admin';
    }

    const { password: _, ...safe } = user;
    res.json({ token: signToken(safe), user: safe });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.post('/api/auth/google', async (req, res) => {
  try {
    const { idToken } = req.body;
    if (!idToken) return res.status(400).json({ error: 'Missing idToken' });
    if (!GOOGLE_CLIENT_ID) return res.status(503).json({ error: 'Google sign-in not configured on server' });

    const client = new OAuth2Client(GOOGLE_CLIENT_ID);
    const ticket = await client.verifyIdToken({ idToken, audience: GOOGLE_CLIENT_ID });
    const payload = ticket.getPayload();
    const email = payload.email;
    const name = payload.name || email.split('@')[0];

    let [rows] = await pool.query('SELECT * FROM users WHERE email = ?', [email]);
    let user = rows[0];

    const role = isAdminEmail(email) ? 'admin' : 'user';
    if (!user) {
      const [result] = await pool.query(
        'INSERT INTO users (name, email, google, role) VALUES (?, ?, 1, ?)',
        [name, email, role]
      );
      user = { id: result.insertId, name, email, phone: '', google: 1, role };
    } else {
      if (!user.google) {
        await pool.query('UPDATE users SET google = 1 WHERE id = ?', [user.id]);
        user.google = 1;
      }
      // Self-healing admin bootstrap on sign-in.
      if (role === 'admin' && user.role !== 'admin') {
        await pool.query('UPDATE users SET role = ? WHERE id = ?', ['admin', user.id]);
        user.role = 'admin';
      }
    }

    const { password: _, ...safe } = user;
    res.json({ token: signToken(safe), user: safe });
  } catch (err) {
    console.error('Google auth error:', err.message);
    res.status(401).json({ error: 'Google sign-in failed' });
  }
});

app.get('/api/auth/me', authMiddleware, async (req, res) => {
  try {
    const [rows] = await pool.query('SELECT id, name, email, phone, google, role FROM users WHERE id = ?', [req.user.id]);
    if (!rows[0]) return res.status(404).json({ error: 'User not found' });
    res.json({ user: rows[0] });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ── Parking history ───────────────────────────────────────────────────────────
app.get('/api/history', authMiddleware, async (req, res) => {
  try {
    const [rows] = await pool.query(
      'SELECT * FROM parking_sessions WHERE user_id = ? ORDER BY created_at DESC LIMIT 30',
      [req.user.id]
    );
    res.json({ history: rows });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.post('/api/history', authMiddleware, async (req, res) => {
  try {
    const { spot_id, lot_id, vehicle_type, duration, started_at, ended_at } = req.body;
    await pool.query(
      'INSERT INTO parking_sessions (user_id, spot_id, lot_id, vehicle_type, duration, started_at, ended_at) VALUES (?,?,?,?,?,?,?)',
      [req.user.id, spot_id, lot_id || DEFAULT_LOT_ID, vehicle_type || 'regular', duration, started_at, ended_at]
    );
    res.json({ ok: true });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ── Spots ─────────────────────────────────────────────────────────────────────
app.get('/api/spots', async (req, res) => {
  try {
    const lotId = req.query.lot_id || DEFAULT_LOT_ID;
    const [rows] = await pool.query(
      'SELECT spot_id, status, score, out_of_service, reserved_by, reserved_at, updated_at FROM parking_spots WHERE lot_id = ? ORDER BY spot_id',
      [lotId]
    );
    res.json({ lot_id: lotId, timestamp: new Date().toISOString(), spots: rows.map(toApiSpot) });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.post('/api/spots/update', async (req, res) => {
  try {
    const { lot_id, spots } = req.body;
    if (!lot_id || !Array.isArray(spots)) return res.status(400).json({ error: 'lot_id and spots array required' });

    const conn = await pool.getConnection();
    try {
      await conn.beginTransaction();
      for (const spot of spots) {
        await conn.query(
          `INSERT INTO parking_spots (lot_id, spot_id, status, score)
           VALUES (?, ?, ?, ?)
           ON DUPLICATE KEY UPDATE status = VALUES(status), score = VALUES(score)`,
          [lot_id, spot.id, spot.status, spot.score || 0]
        );
        // Car arrived on a reserved spot → reservation fulfilled, clear the hold.
        if (spot.status === 'occupied') {
          await conn.query(
            'UPDATE parking_spots SET reserved_by = NULL, reserved_at = NULL WHERE lot_id = ? AND spot_id = ?',
            [lot_id, spot.id]
          );
        }
      }
      await conn.commit();
    } catch (err) {
      await conn.rollback();
      throw err;
    } finally {
      conn.release();
    }

    // Broadcast the effective status (admin OOS + active reservations override CV).
    const ids = spots.map(s => s.id);
    let emitted = spots.map(s => ({ id: s.id, status: s.status, score: s.score || 0 }));
    if (ids.length) {
      const [rows] = await pool.query(
        'SELECT spot_id, status, score, out_of_service, reserved_by, reserved_at FROM parking_spots WHERE lot_id = ? AND spot_id IN (?)',
        [lot_id, ids]
      );
      const byId = new Map(rows.map(r => [r.spot_id, r]));
      emitted = emitted.map(s => byId.has(s.id) ? { ...s, status: effectiveStatus(byId.get(s.id)) } : s);
    }

    io.emit('spots:update', { lot_id, spots: emitted, timestamp: new Date().toISOString() });
    res.json({ ok: true, updated: spots.length });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Broadcast one spot's effective status to every connected client.
async function broadcastSpot(lot_id, spot_id) {
  const [[row]] = await pool.query(
    'SELECT spot_id, status, score, out_of_service, reserved_by, reserved_at FROM parking_spots WHERE lot_id = ? AND spot_id = ?',
    [lot_id, spot_id]
  );
  if (!row) return;
  io.emit('spots:update', {
    lot_id,
    spots: [{ id: spot_id, status: effectiveStatus(row), score: row.score }],
    timestamp: new Date().toISOString(),
  });
}

// ── Reservations (app holds a spot until the car is detected) ──────────────────
app.post('/api/spots/reserve', authMiddleware, async (req, res) => {
  try {
    const lot_id = req.body.lot_id || DEFAULT_LOT_ID;
    const spot_id = (req.body.spot_id || '').trim();
    if (!spot_id) return res.status(400).json({ error: 'spot_id required' });

    const [[row]] = await pool.query('SELECT * FROM parking_spots WHERE lot_id = ? AND spot_id = ?', [lot_id, spot_id]);
    if (!row) return res.status(404).json({ error: 'Spot not found' });
    if (row.out_of_service) return res.status(409).json({ error: 'Spot is out of service' });
    if (row.status === 'occupied') return res.status(409).json({ error: 'Spot is occupied' });
    const heldByOther = row.reserved_by != null && row.reserved_by !== req.user.id &&
      row.reserved_at != null && (Date.now() - Number(row.reserved_at) < RESERVATION_TTL_MS);
    if (heldByOther) return res.status(409).json({ error: 'Spot is already reserved' });

    await pool.query('UPDATE parking_spots SET reserved_by = ?, reserved_at = ? WHERE lot_id = ? AND spot_id = ?',
      [req.user.id, Date.now(), lot_id, spot_id]);
    await broadcastSpot(lot_id, spot_id);
    res.json({ ok: true, spot_id, status: 'reserved' });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.post('/api/spots/release', authMiddleware, async (req, res) => {
  try {
    const lot_id = req.body.lot_id || DEFAULT_LOT_ID;
    const spot_id = (req.body.spot_id || '').trim();
    if (!spot_id) return res.status(400).json({ error: 'spot_id required' });

    const [[row]] = await pool.query('SELECT reserved_by FROM parking_spots WHERE lot_id = ? AND spot_id = ?', [lot_id, spot_id]);
    if (!row) return res.status(404).json({ error: 'Spot not found' });
    if (row.reserved_by != null && row.reserved_by !== req.user.id) {
      return res.status(403).json({ error: 'Not your reservation' });
    }
    await pool.query('UPDATE parking_spots SET reserved_by = NULL, reserved_at = NULL WHERE lot_id = ? AND spot_id = ?', [lot_id, spot_id]);
    await broadcastSpot(lot_id, spot_id);
    res.json({ ok: true, spot_id });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ── Public lots list (so the app's lot picker comes from the DB) ────────────────
app.get('/api/lots', async (req, res) => {
  try {
    const [lots] = await pool.query('SELECT lot_id, name, address, total_spots FROM parking_lots ORDER BY name');
    res.json({ lots });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ── Issue reports (raised from the app, triaged in the dashboard) ───────────────
app.post('/api/reports', optionalAuth, async (req, res) => {
  try {
    const { lot_id = DEFAULT_LOT_ID, spot_id = '', issue_type, note = '' } = req.body;
    if (!issue_type) return res.status(400).json({ error: 'issue_type required' });
    const userId = req.user ? req.user.id : null;
    const [r] = await pool.query(
      'INSERT INTO reports (user_id, lot_id, spot_id, issue_type, note) VALUES (?,?,?,?,?)',
      [userId, lot_id, String(spot_id).slice(0, 20), String(issue_type).slice(0, 40), String(note).slice(0, 500)]
    );
    io.emit('report:new', { id: r.insertId, lot_id, spot_id, issue_type, status: 'open', created_at: new Date().toISOString() });
    res.json({ ok: true, id: r.insertId });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ── Admin: overview ─────────────────────────────────────────────────────────
// Snapshot for the dashboard landing page: lot occupancy, user/session counts
// and open report count. Everything beyond /api/admin requires the admin role.
app.get('/api/admin/overview', authMiddleware, requireAdmin, async (req, res) => {
  try {
    const lotId = req.query.lot_id || DEFAULT_LOT_ID;

    const [[{ users }]]   = await pool.query('SELECT COUNT(*) AS users FROM users');
    const [[{ admins }]]  = await pool.query("SELECT COUNT(*) AS admins FROM users WHERE role = 'admin'");
    const [[{ active }]]  = await pool.query('SELECT COUNT(*) AS active FROM parking_sessions WHERE ended_at IS NULL OR ended_at = 0');
    const [[{ reports }]] = await pool.query("SELECT COUNT(*) AS reports FROM reports WHERE status = 'open'");

    // Bucket by effective status so out-of-service spots are their own category.
    const [spotRows] = await pool.query(
      `SELECT (CASE WHEN out_of_service = 1 THEN 'out_of_service' ELSE status END) AS status,
              COUNT(*) AS n
       FROM parking_spots WHERE lot_id = ?
       GROUP BY (CASE WHEN out_of_service = 1 THEN 'out_of_service' ELSE status END)`,
      [lotId]
    );
    const byStatus = spotRows.reduce((acc, r) => (acc[r.status] = r.n, acc), {});
    const total = spotRows.reduce((sum, r) => sum + r.n, 0);
    const occupied = byStatus.occupied || 0;
    const outOfService = byStatus.out_of_service || 0;
    const serviceable = total - outOfService;          // % is of usable spaces only

    res.json({
      lot_id: lotId,
      timestamp: new Date().toISOString(),
      users: { total: users, admins },
      sessions: { active },
      reports: { open: reports },
      occupancy: {
        total,
        occupied,
        free: byStatus.free || 0,
        out_of_service: outOfService,
        serviceable,
        by_status: byStatus,
        percent: serviceable ? Math.round((occupied / serviceable) * 100) : 0,
      },
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ── Admin: lots ───────────────────────────────────────────────────────────────
// List lots with live spot counts (effective status, so out-of-service is split out).
app.get('/api/admin/lots', authMiddleware, requireAdmin, async (req, res) => {
  try {
    const [lots] = await pool.query('SELECT * FROM parking_lots ORDER BY name');
    const [counts] = await pool.query(
      `SELECT lot_id,
              COUNT(*) AS spots,
              SUM(status = 'occupied' AND out_of_service = 0) AS occupied,
              SUM(status = 'free'     AND out_of_service = 0) AS free,
              SUM(out_of_service = 1)                          AS out_of_service
       FROM parking_spots GROUP BY lot_id`
    );
    const byLot = counts.reduce((acc, r) => (acc[r.lot_id] = r, acc), {});
    res.json({
      lots: lots.map(l => {
        const c = byLot[l.lot_id] || {};
        return {
          ...l,
          spot_count: Number(c.spots || 0),
          occupied: Number(c.occupied || 0),
          free: Number(c.free || 0),
          out_of_service: Number(c.out_of_service || 0),
        };
      }),
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.post('/api/admin/lots', authMiddleware, requireAdmin, async (req, res) => {
  try {
    const lot_id = (req.body.lot_id || '').trim();
    const name = (req.body.name || '').trim();
    const address = (req.body.address || '').trim();
    const total_spots = parseInt(req.body.total_spots, 10) || 0;
    if (!lot_id || !name) return res.status(400).json({ error: 'lot_id and name are required' });
    if (!/^[a-zA-Z0-9_-]+$/.test(lot_id)) return res.status(400).json({ error: 'lot_id may only contain letters, numbers, _ and -' });

    await pool.query(
      'INSERT INTO parking_lots (lot_id, name, address, total_spots) VALUES (?, ?, ?, ?)',
      [lot_id, name, address, total_spots]
    );
    res.json({ ok: true, lot: { lot_id, name, address, total_spots } });
  } catch (err) {
    if (err.code === 'ER_DUP_ENTRY') return res.status(409).json({ error: 'A lot with that id already exists' });
    res.status(500).json({ error: err.message });
  }
});

app.put('/api/admin/lots/:lotId', authMiddleware, requireAdmin, async (req, res) => {
  try {
    const name = (req.body.name || '').trim();
    const address = (req.body.address || '').trim();
    const total_spots = parseInt(req.body.total_spots, 10) || 0;
    if (!name) return res.status(400).json({ error: 'name is required' });
    const [r] = await pool.query(
      'UPDATE parking_lots SET name = ?, address = ?, total_spots = ? WHERE lot_id = ?',
      [name, address, total_spots, req.params.lotId]
    );
    if (!r.affectedRows) return res.status(404).json({ error: 'Lot not found' });
    res.json({ ok: true });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.delete('/api/admin/lots/:lotId', authMiddleware, requireAdmin, async (req, res) => {
  const conn = await pool.getConnection();
  try {
    await conn.beginTransaction();
    const [r] = await conn.query('DELETE FROM parking_lots WHERE lot_id = ?', [req.params.lotId]);
    await conn.query('DELETE FROM parking_spots WHERE lot_id = ?', [req.params.lotId]);
    await conn.commit();
    if (!r.affectedRows) return res.status(404).json({ error: 'Lot not found' });
    res.json({ ok: true });
  } catch (err) {
    await conn.rollback();
    res.status(500).json({ error: err.message });
  } finally {
    conn.release();
  }
});

// ── Admin: spots ──────────────────────────────────────────────────────────────
// Detailed spot list for a lot (includes raw status + out_of_service flag).
app.get('/api/admin/spots', authMiddleware, requireAdmin, async (req, res) => {
  try {
    const lotId = req.query.lot_id || DEFAULT_LOT_ID;
    const [rows] = await pool.query(
      'SELECT spot_id AS id, status, score, out_of_service, updated_at FROM parking_spots WHERE lot_id = ? ORDER BY spot_id',
      [lotId]
    );
    res.json({
      lot_id: lotId,
      spots: rows.map(r => ({ ...r, out_of_service: !!r.out_of_service })),
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.post('/api/admin/spots', authMiddleware, requireAdmin, async (req, res) => {
  try {
    const lot_id = (req.body.lot_id || '').trim();
    const spot_id = (req.body.spot_id || '').trim();
    if (!lot_id || !spot_id) return res.status(400).json({ error: 'lot_id and spot_id are required' });
    await pool.query(
      "INSERT INTO parking_spots (lot_id, spot_id, status) VALUES (?, ?, 'free')",
      [lot_id, spot_id]
    );
    io.emit('spots:update', { lot_id, spots: [{ id: spot_id, status: 'free', score: 0 }], timestamp: new Date().toISOString() });
    res.json({ ok: true });
  } catch (err) {
    if (err.code === 'ER_DUP_ENTRY') return res.status(409).json({ error: 'That spot already exists in this lot' });
    res.status(500).json({ error: err.message });
  }
});

app.delete('/api/admin/spots', authMiddleware, requireAdmin, async (req, res) => {
  try {
    const { lot_id, spot_id } = req.body;
    if (!lot_id || !spot_id) return res.status(400).json({ error: 'lot_id and spot_id are required' });
    const [r] = await pool.query('DELETE FROM parking_spots WHERE lot_id = ? AND spot_id = ?', [lot_id, spot_id]);
    if (!r.affectedRows) return res.status(404).json({ error: 'Spot not found' });
    res.json({ ok: true });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Override a spot: action = free | occupied | out_of_service | in_service.
// Emits the effective status so the user app and dashboard both stay in sync.
app.post('/api/admin/spots/status', authMiddleware, requireAdmin, async (req, res) => {
  try {
    const { lot_id, spot_id, action } = req.body;
    if (!lot_id || !spot_id || !action) return res.status(400).json({ error: 'lot_id, spot_id and action are required' });

    let sql, params;
    // Admin overrides also clear any app reservation (a deliberate reset).
    if (action === 'out_of_service') {
      sql = 'UPDATE parking_spots SET out_of_service = 1, reserved_by = NULL, reserved_at = NULL WHERE lot_id = ? AND spot_id = ?';
      params = [lot_id, spot_id];
    } else if (action === 'in_service') {
      sql = 'UPDATE parking_spots SET out_of_service = 0 WHERE lot_id = ? AND spot_id = ?';
      params = [lot_id, spot_id];
    } else if (action === 'free' || action === 'occupied') {
      sql = 'UPDATE parking_spots SET status = ?, out_of_service = 0, reserved_by = NULL, reserved_at = NULL WHERE lot_id = ? AND spot_id = ?';
      params = [action, lot_id, spot_id];
    } else {
      return res.status(400).json({ error: 'Invalid action' });
    }

    const [r] = await pool.query(sql, params);
    if (!r.affectedRows) return res.status(404).json({ error: 'Spot not found' });

    // Emit the true effective status (accounts for any remaining reservation).
    const [[row]] = await pool.query(
      'SELECT spot_id, status, score, out_of_service, reserved_by, reserved_at FROM parking_spots WHERE lot_id = ? AND spot_id = ?',
      [lot_id, spot_id]
    );
    const effective = effectiveStatus(row);
    io.emit('spots:update', { lot_id, spots: [{ id: spot_id, status: effective, score: row.score }], timestamp: new Date().toISOString() });
    res.json({ ok: true, spot_id, status: effective });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ── Admin: reports triage ───────────────────────────────────────────────────
app.get('/api/admin/reports', authMiddleware, requireAdmin, async (req, res) => {
  try {
    const { status } = req.query;
    const params = [];
    let where = '';
    if (status === 'open' || status === 'resolved') { where = 'WHERE r.status = ?'; params.push(status); }
    const [rows] = await pool.query(
      `SELECT r.id, r.lot_id, r.spot_id, r.issue_type, r.note, r.status, r.created_at, r.resolved_at,
              u.name AS user_name, u.email AS user_email
       FROM reports r LEFT JOIN users u ON u.id = r.user_id
       ${where} ORDER BY (r.status = 'open') DESC, r.created_at DESC LIMIT 200`,
      params
    );
    const [[{ open }]] = await pool.query("SELECT COUNT(*) AS open FROM reports WHERE status = 'open'");
    res.json({ reports: rows, open });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.patch('/api/admin/reports/:id', authMiddleware, requireAdmin, async (req, res) => {
  try {
    const { status } = req.body;
    if (status !== 'open' && status !== 'resolved') return res.status(400).json({ error: 'status must be open or resolved' });
    const [r] = await pool.query(
      'UPDATE reports SET status = ?, resolved_at = ? WHERE id = ?',
      [status, status === 'resolved' ? new Date() : null, req.params.id]
    );
    if (!r.affectedRows) return res.status(404).json({ error: 'Report not found' });
    res.json({ ok: true });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ── Health ────────────────────────────────────────────────────────────────────
app.get('/health', (req, res) => res.json({ status: 'ok', timestamp: new Date().toISOString() }));

// ── Socket ────────────────────────────────────────────────────────────────────
io.on('connection', socket => {
  console.log('Client connected:', socket.id);
  socket.on('disconnect', () => console.log('Client disconnected:', socket.id));
});

// ── Start ─────────────────────────────────────────────────────────────────────
initDB().then(() => {
  server.listen(PORT, '0.0.0.0', () => {
    console.log(`ParkWise API running → http://localhost:${PORT}`);
    console.log(`On phone → http://10.0.0.6:${PORT}`);
  });
}).catch(err => {
  console.error('DB init failed:', err.message);
  process.exit(1);
});
