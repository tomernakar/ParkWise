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

const app = express();
const server = http.createServer(app);
const io = new Server(server, { cors: { origin: '*' } });
const PORT = process.env.PORT || 3000;
const JWT_SECRET = process.env.JWT_SECRET || 'parkwise-secret-change-in-production';

app.use(cors());
app.use(express.json());
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
        created_at TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
      )
    `);
    await conn.query(`
      CREATE TABLE IF NOT EXISTS parking_sessions (
        id           INT AUTO_INCREMENT PRIMARY KEY,
        user_id      INT NOT NULL,
        spot_id      VARCHAR(20),
        lot_id       VARCHAR(50)  DEFAULT 'campus',
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
        updated_at TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (lot_id, spot_id)
      )
    `);
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

    const hashed = await bcrypt.hash(password, 10);
    const [result] = await pool.query(
      'INSERT INTO users (name, email, phone, password) VALUES (?, ?, ?, ?)',
      [name.trim(), email.trim().toLowerCase(), phone.trim(), hashed]
    );
    const user = { id: result.insertId, name: name.trim(), email: email.trim().toLowerCase(), phone: phone.trim() };
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

    if (!user) {
      const [result] = await pool.query(
        'INSERT INTO users (name, email, google) VALUES (?, ?, 1)',
        [name, email]
      );
      user = { id: result.insertId, name, email, phone: '', google: 1 };
    } else if (!user.google) {
      await pool.query('UPDATE users SET google = 1 WHERE id = ?', [user.id]);
      user.google = 1;
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
    const [rows] = await pool.query('SELECT id, name, email, phone, google FROM users WHERE id = ?', [req.user.id]);
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
      [req.user.id, spot_id, lot_id || 'campus', vehicle_type || 'regular', duration, started_at, ended_at]
    );
    res.json({ ok: true });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ── Spots ─────────────────────────────────────────────────────────────────────
app.get('/api/spots', async (req, res) => {
  try {
    const lotId = req.query.lot_id || 'parkwise_demo';
    const [rows] = await pool.query(
      'SELECT spot_id as id, status, score, updated_at FROM parking_spots WHERE lot_id = ? ORDER BY spot_id',
      [lotId]
    );
    res.json({ lot_id: lotId, timestamp: new Date().toISOString(), spots: rows });
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
      }
      await conn.commit();
    } catch (err) {
      await conn.rollback();
      throw err;
    } finally {
      conn.release();
    }

    io.emit('spots:update', { lot_id, spots, timestamp: new Date().toISOString() });
    res.json({ ok: true, updated: spots.length });
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
