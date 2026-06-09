/* ============================================================
   PARKWISE ADMIN — dashboard client (vanilla JS, no build)
   Mirrors the user app's state -> render() pattern. Talks to the
   same API/Socket.io backend; admin-gated by the user's role.
   ============================================================ */

const TOKEN_KEY = "parkwise-admin-token-v1";   // distinct from the user app's token
const DEFAULT_LOT_ID = "demo_lot_1";

const state = {
  view: "login",          // login | overview | spots | users | reports | analytics
  user: null,
  lotId: DEFAULT_LOT_ID,
  overview: null,         // /api/admin/overview payload
  spots: [],              // /api/spots payload (overview live map)
  socketConnected: false,
  loginError: "",
  loginBusy: false,
  booting: true,
  // Spots & Lots management
  lots: [],
  adminSpots: [],         // /api/admin/spots for the managed lot
  manageLotId: null,
  modal: null,            // { type, ... }
};

let socket = null;

// ---------------------------------------------------------------------------
// Auth + API
// ---------------------------------------------------------------------------
const getToken = () => localStorage.getItem(TOKEN_KEY);
const setToken = (t) => localStorage.setItem(TOKEN_KEY, t);
const clearToken = () => localStorage.removeItem(TOKEN_KEY);

async function api(path, opts = {}) {
  const token = getToken();
  const headers = {
    "Content-Type": "application/json",
    ...(token ? { Authorization: "Bearer " + token } : {}),
    ...(opts.headers || {}),
  };
  const res = await fetch(path, { ...opts, headers });
  let data = {};
  try { data = await res.json(); } catch {}
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

async function boot() {
  const token = getToken();
  if (token) {
    try {
      const { user } = await api("/api/auth/me");
      if (user.role === "admin") {
        state.user = user;
        state.view = "overview";
        state.booting = false;
        render();
        afterEnterDashboard();
        return;
      }
      clearToken(); // logged in but not an admin
    } catch {
      clearToken();
    }
  }
  state.booting = false;
  state.view = "login";
  render();
}

async function handleLogin(e) {
  e.preventDefault();
  const email = document.getElementById("email").value.trim();
  const password = document.getElementById("password").value;
  state.loginError = "";
  state.loginBusy = true;
  render();
  try {
    const { token, user } = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    });
    if (user.role !== "admin") {
      state.loginBusy = false;
      state.loginError = "This account doesn't have admin access.";
      render();
      return;
    }
    setToken(token);
    state.user = user;
    state.loginBusy = false;
    state.view = "overview";
    render();
    afterEnterDashboard();
  } catch (err) {
    state.loginBusy = false;
    state.loginError = err.message;
    render();
  }
}

function logout() {
  clearToken();
  if (socket) { try { socket.disconnect(); } catch {} socket = null; }
  state.user = null;
  state.overview = null;
  state.spots = [];
  state.socketConnected = false;
  state.view = "login";
  render();
}

// Called once the admin lands on the dashboard.
async function afterEnterDashboard() {
  connectSocket();
  await Promise.all([loadOverview(), loadSpots()]);
}

async function loadOverview() {
  try {
    state.overview = await api(`/api/admin/overview?lot_id=${encodeURIComponent(state.lotId)}`);
    if (state.overview.lot_id) state.lotId = state.overview.lot_id;
    if (state.view === "overview") { updateKpis(); }
  } catch (err) {
    console.warn("overview load failed:", err.message);
  }
}

async function loadSpots() {
  try {
    const res = await fetch(`/api/spots?lot_id=${encodeURIComponent(state.lotId)}`);
    if (!res.ok) return;
    const data = await res.json();
    state.spots = (data.spots || []).slice().sort(spotSort);
    if (state.view === "overview") renderLotMap();
  } catch {}
}

function spotSort(a, b) {
  const na = parseInt(String(a.id).replace(/\D/g, ""), 10);
  const nb = parseInt(String(b.id).replace(/\D/g, ""), 10);
  if (!isNaN(na) && !isNaN(nb)) return na - nb;
  return String(a.id).localeCompare(String(b.id));
}

// ---------------------------------------------------------------------------
// Lot map — mirrors the user app's physical layout (ParkWise/index.html:
// createSpots + mapSvg). Same U-shaped lot, walls, islands, entry, lanes.
// ---------------------------------------------------------------------------
const SV = { w: 300, h: 440 };
const LAYOUT = (() => {
  const m = {}; let n = 0;
  const add = (type, x, y, w, h) => { m[`S${++n}`] = { x, y, w, h, type }; };
  const SY = 32, DY = 37;
  // Left wall S1..S10
  ["regular","regular","regular","regular","regular","regular","regular","regular","accessible","accessible"]
    .forEach((t, i) => add(t, 7, SY + i * DY, 38, 17));
  // Right wall S11..S20
  ["accessible","accessible","regular","regular","regular","ev","regular","regular","accessible","accessible"]
    .forEach((t, i) => add(t, 255, SY + i * DY, 38, 17));
  // Center upper island S21..S23
  ["accessible","regular","regular"].forEach((t, i) => add(t, 133, 80 + i * 42, 34, 19));
  // Center lower island S24..S27
  ["regular","regular","regular","regular"].forEach((t, i) => add(t, 133, 240 + i * 40, 34, 19));
  return m;
})();
const LAYOUT_IDS = new Set(Object.keys(LAYOUT));

// True when a lot's spots line up with the known physical layout.
function lotUsesMap(lotId, spots) {
  if (lotId !== DEFAULT_LOT_ID) return false;
  return spots.some((s) => LAYOUT_IDS.has(s.id));
}

function lotMapSvg(spots, opts = {}) {
  const clickable = !!opts.clickable;
  const byId = new Map(spots.map((s) => [s.id, s]));
  let svg = `<svg viewBox="0 0 ${SV.w} ${SV.h}" class="lotsvg" role="img" aria-label="Parking lot map">`;
  // Boundary U-shape + center islands + entry + lane arrows (identical to the app)
  svg += `<path d="M 3,8 L 3,418 Q 3,437 22,437 L 278,437 Q 297,437 297,418 L 297,8" fill="#071017" stroke="rgba(255,255,255,.12)" stroke-width="1.5"/>`;
  svg += `<rect x="127" y="72" width="46" height="136" rx="8" fill="rgba(255,255,255,.03)" stroke="rgba(255,255,255,.08)" stroke-width="0.8"/>`;
  svg += `<rect x="127" y="232" width="46" height="176" rx="8" fill="rgba(255,255,255,.03)" stroke="rgba(255,255,255,.08)" stroke-width="0.8"/>`;
  svg += `<rect x="138" y="432" width="24" height="5" rx="2" fill="rgba(41,199,172,.92)"/>`;
  svg += `<text x="150" y="426" text-anchor="middle" font-size="7" fill="rgba(41,199,172,.9)" font-family="IBM Plex Mono,monospace">ENTRY</text>`;
  svg += `<text x="68" y="260" text-anchor="middle" font-size="8" fill="rgba(255,255,255,.12)" font-family="IBM Plex Mono,monospace" transform="rotate(-90 68 260)">&#9650; &#9650; &#9650; &#9650;</text>`;
  svg += `<text x="232" y="200" text-anchor="middle" font-size="8" fill="rgba(255,255,255,.12)" font-family="IBM Plex Mono,monospace" transform="rotate(90 232 200)">&#9650; &#9650; &#9650; &#9650;</text>`;

  Object.entries(LAYOUT).forEach(([id, g]) => {
    const s = byId.get(id);
    const present = !!s;
    const oos = !!(s && s.out_of_service);
    const raw = s ? (s.raw_status != null ? s.raw_status : s.status) : "free";
    let fill = "rgba(52,211,153,.20)", stroke = "rgba(52,211,153,.80)";
    if (!present) { fill = "rgba(255,255,255,.02)"; stroke = "rgba(255,255,255,.10)"; }
    else if (oos) { fill = "rgba(255,255,255,.06)"; stroke = "rgba(255,255,255,.32)"; }
    else if (raw === "occupied") { fill = "rgba(248,113,133,.22)"; stroke = "rgba(248,113,133,.85)"; }
    else if (g.type === "ev") { fill = "rgba(245,176,71,.26)"; stroke = "rgba(245,176,71,.95)"; }
    else if (g.type === "accessible") { fill = "rgba(96,165,250,.24)"; stroke = "rgba(96,165,250,.92)"; }

    const cx = g.x + g.w / 2, cy = g.y + g.h / 2;
    const click = clickable && present ? ` data-spot="${escapeAttr(id)}" onclick="openSpotModal('${escapeAttr(id)}')" style="cursor:pointer"` : "";
    svg += `<rect class="mspot"${click} x="${g.x}" y="${g.y}" width="${g.w}" height="${g.h}" rx="2.5" fill="${fill}" stroke="${stroke}" stroke-width="1.1"/>`;
    // Spot id label (admins need to identify spots). Type still shown via colour.
    const lblColor = !present ? "rgba(255,255,255,.25)"
      : oos ? "rgba(255,255,255,.5)"
      : raw === "occupied" ? "rgba(255,210,214,.95)" : "rgba(235,245,242,.92)";
    const txt = oos ? `${id} ✕` : id;
    svg += `<text x="${cx}" y="${cy + 2.3}" text-anchor="middle" font-size="6.2" font-weight="700" fill="${lblColor}" font-family="IBM Plex Mono,monospace" pointer-events="none">${escapeHtml(txt)}</text>`;
  });
  svg += `</svg>`;
  return svg;
}

function mapLegendHTML() {
  return `<div class="legend">
    <span><i style="background:rgba(52,211,153,.85)"></i>Free</span>
    <span><i style="background:rgba(248,113,133,.85)"></i>Occupied</span>
    <span><i style="background:rgba(96,165,250,.9)"></i>Accessible</span>
    <span><i style="background:rgba(245,176,71,.95)"></i>EV</span>
    <span><i style="background:rgba(255,255,255,.4)"></i>Out of service</span>
  </div>`;
}

// ---------------------------------------------------------------------------
// Socket — live spot updates
// ---------------------------------------------------------------------------
function connectSocket() {
  if (socket) return;
  try {
    socket = io();
    socket.on("connect", () => { state.socketConnected = true; updateLiveDot(); });
    socket.on("disconnect", () => { state.socketConnected = false; updateLiveDot(); });
    socket.on("spots:update", ({ lot_id, spots }) => {
      if (lot_id && lot_id !== state.lotId) return;
      const byId = new Map(state.spots.map((s) => [s.id, s]));
      const changed = [];
      (spots || []).forEach((s) => {
        const cur = byId.get(s.id);
        if (cur) {
          if (cur.status !== s.status) { cur.status = s.status; changed.push(s.id); }
          cur.score = s.score;
        } else {
          state.spots.push({ id: s.id, status: s.status, score: s.score });
          changed.push(s.id);
        }
      });
      if (!changed.length) return;
      state.spots.sort(spotSort);
      if (state.view === "overview") { renderLotMap(); recomputeOccupancy(); }
      // Keep the management map honest if it's open for this lot.
      if (state.view === "spots" && lot_id === state.manageLotId) loadAdminSpots(state.manageLotId).then(render);
    });
  } catch (e) {
    console.warn("socket failed:", e.message);
  }
}

// ---------------------------------------------------------------------------
// Icons
// ---------------------------------------------------------------------------
const I = {
  grid:   `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/></svg>`,
  car:    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5 17H3v-5l2.5-6h11L19 12v5h-2"/><circle cx="7" cy="17" r="2"/><circle cx="17" cy="17" r="2"/><path d="M9 17h6M5 12h14"/></svg>`,
  users:  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87M16 3.13a4 4 0 010 7.75"/></svg>`,
  flag:   `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><line x1="4" y1="22" x2="4" y2="15"/></svg>`,
  chart:  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>`,
  gauge:  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 14l4-4"/><path d="M3.34 19a10 10 0 1117.32 0z"/></svg>`,
  spot:   `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="3"/><path d="M9 17V7h4a3 3 0 010 6H9"/></svg>`,
  clock:  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>`,
  logout: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>`,
  pin:    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0116 0z"/><circle cx="12" cy="10" r="2.6"/></svg>`,
};

const NAV = [
  { id: "overview",  label: "Overview",      icon: I.grid,  ready: true },
  { id: "spots",     label: "Spots & Lots",  icon: I.spot,  ready: true },
  { id: "users",     label: "Users",         icon: I.users, ready: false },
  { id: "reports",   label: "Reports",       icon: I.flag,  ready: false },
  { id: "analytics", label: "Analytics",     icon: I.chart, ready: false },
];
const VIEW_TITLES = {
  overview: "Overview", spots: "Spots & Lots", users: "Users",
  reports: "Reports", analytics: "Analytics",
};

function navigate(view) {
  state.view = view;
  state.modal = null;
  render();
  if (view === "overview") { renderLotMap(); updateKpis(); }
  if (view === "spots") loadSpotsView();
}

// ---------- Spots & Lots data ----------
async function loadSpotsView() {
  await loadLots();
  if (!state.manageLotId) state.manageLotId = state.lots[0]?.lot_id || state.lotId;
  await loadAdminSpots(state.manageLotId);
  if (state.view === "spots") render();
}
async function loadLots() {
  try { state.lots = (await api("/api/admin/lots")).lots || []; } catch (e) { console.warn(e.message); }
}
async function loadAdminSpots(lotId) {
  try {
    const spots = (await api(`/api/admin/spots?lot_id=${encodeURIComponent(lotId)}`)).spots || [];
    state.adminSpots = spots.sort(spotSort);   // numeric (S2 before S10), not string order
  } catch (e) { state.adminSpots = []; }
}
async function refreshSpotsView() {
  await Promise.all([loadLots(), loadAdminSpots(state.manageLotId)]);
  if (state.view === "spots") render();
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------
const root = () => document.getElementById("root");

function render() {
  if (state.booting) { root().innerHTML = ""; return; }
  if (state.view === "login") { root().innerHTML = loginHTML(); wireLogin(); return; }
  root().innerHTML = shellHTML();
}

function loginHTML() {
  return `
    <div class="login-wrap">
      <form class="login-card" id="loginForm">
        <div class="login-logo">
          <span class="mark">${I.pin}</span>
          <h1>ParkWise <span>Admin</span></h1>
        </div>
        <p class="login-sub">Sign in with an administrator account</p>
        <div class="field">
          <label for="email">Email</label>
          <input class="input" id="email" type="email" autocomplete="username" placeholder="you@example.com" required>
        </div>
        <div class="field">
          <label for="password">Password</label>
          <input class="input" id="password" type="password" autocomplete="current-password" placeholder="••••••••" required>
        </div>
        <button class="btn" type="submit" ${state.loginBusy ? "disabled" : ""}>
          ${state.loginBusy ? "Signing in…" : "Sign in"}
        </button>
        ${state.loginError ? `<div class="alert">${escapeHtml(state.loginError)}</div>` : ""}
      </form>
    </div>`;
}
function wireLogin() {
  const f = document.getElementById("loginForm");
  if (f) f.addEventListener("submit", handleLogin);
}

function shellHTML() {
  return `
    <div class="shell">
      <aside class="sidebar">
        <div class="sb-logo">
          <span class="mark">${I.pin}</span>
          <h2>ParkWise <span>Admin</span></h2>
        </div>
        <nav class="nav">
          ${NAV.map(navItemHTML).join("")}
        </nav>
        <div class="sb-foot">v1.0 · live console</div>
      </aside>
      <div class="main">
        <header class="topbar">
          <h1>${VIEW_TITLES[state.view] || ""}</h1>
          <span class="spacer"></span>
          <span class="live-dot ${state.socketConnected ? "" : "off"}" id="liveDot">
            <span class="dot"></span>${state.socketConnected ? "Live" : "Offline"}
          </span>
          <div class="user-chip">
            <div class="avatar">${initials(state.user?.name || "A")}</div>
            <div>
              <div class="uname">${escapeHtml(state.user?.name || "Admin")}</div>
              <div class="urole">Administrator</div>
            </div>
            <button class="nav-item" style="padding:8px;color:var(--muted)" onclick="logout()" title="Sign out" aria-label="Sign out">
              <span class="ico">${I.logout}</span>
            </button>
          </div>
        </header>
        <main class="content" id="content">${viewHTML()}</main>
      </div>
    </div>
    ${state.modal ? modalHTML() : ""}`;
}

function navItemHTML(n) {
  const active = state.view === n.id ? "active" : "";
  return `<a class="nav-item ${active}" onclick="navigate('${n.id}')">
    <span class="ico">${n.icon}</span>${n.label}
    ${n.ready ? "" : `<span class="soon">SOON</span>`}
  </a>`;
}

function viewHTML() {
  if (state.view === "overview") return overviewHTML();
  if (state.view === "spots") return spotsHTML();
  return placeholderHTML(state.view);
}

function placeholderHTML(view) {
  const blurbs = {
    spots: "Manage lots and spots, override CV status, and mark spaces out of service.",
    users: "Browse registered users, see their activity, and grant or revoke admin access.",
    reports: "Triage issue reports raised from the app and mark them resolved.",
    analytics: "Occupancy trends, peak hours, and session utilization over time.",
  };
  const icons = { spots: I.spot, users: I.users, reports: I.flag, analytics: I.chart };
  return `
    <section class="card placeholder">
      <div class="ico">${icons[view] || I.grid}</div>
      <h3>${VIEW_TITLES[view]} — coming soon</h3>
      <p>${blurbs[view] || ""}</p>
    </section>`;
}

// ---------- Spots & Lots ----------
function spotsHTML() {
  const lot = state.lots.find((l) => l.lot_id === state.manageLotId);
  return `
    <section class="lotbar">
      ${state.lots.map(lotChipHTML).join("")}
      <div class="lotchip add" onclick="openLotModal()">＋ New lot</div>
    </section>
    ${lot ? lotPanelHTML(lot) : `<div class="card empty">Select or create a lot to manage.</div>`}`;
}

function lotChipHTML(l) {
  const active = l.lot_id === state.manageLotId ? "active" : "";
  return `<div class="lotchip ${active}" onclick="selectManageLot('${escapeAttr(l.lot_id)}')">
    <span class="ln">${escapeHtml(l.name)}</span>
    <span class="lm">${l.free} free · ${l.occupied} occ${l.out_of_service ? ` · ${l.out_of_service} oos` : ""}</span>
  </div>`;
}

function lotPanelHTML(lot) {
  return `
    <section class="card panel">
      <div class="panel-head">
        <div>
          <h3>${escapeHtml(lot.name)} · <span class="mono" style="color:var(--muted)">${escapeHtml(lot.lot_id)}</span></h3>
          <div style="font-size:12px;color:var(--muted);margin-top:4px">
            ${lot.address ? escapeHtml(lot.address) + " · " : ""}${lot.spot_count} spaces tracked
          </div>
        </div>
        <div class="toolbar">
          <button class="btn-sm" onclick="openLotModal('${escapeAttr(lot.lot_id)}')">Edit lot</button>
          <button class="btn-sm danger" onclick="confirmDeleteLot('${escapeAttr(lot.lot_id)}')">Delete</button>
        </div>
      </div>
      <div class="panel-head" style="margin-bottom:14px">
        ${mapLegendHTML()}
        <span style="font-size:12px;color:var(--faint)">Click a spot to manage it</span>
      </div>
      ${managedSpotsHTML()}
    </section>`;
}

function managedSpotsHTML() {
  const useMap = lotUsesMap(state.manageLotId, state.adminSpots);
  if (useMap) {
    const extra = state.adminSpots.filter((s) => !LAYOUT_IDS.has(s.id));
    return `
      <div class="lotmap-wrap">${lotMapSvg(state.adminSpots, { clickable: true })}</div>
      <div class="lotmap" style="margin-top:16px">
        ${extra.map(adminSpotTileHTML).join("")}
        <div class="spot add" onclick="addSpotPrompt()" title="Add spot">＋</div>
      </div>`;
  }
  return `
    <div class="lotmap">
      ${state.adminSpots.map(adminSpotTileHTML).join("")}
      <div class="spot add" onclick="addSpotPrompt()" title="Add spot">＋</div>
    </div>`;
}

function adminSpotTileHTML(s) {
  const cls = s.out_of_service ? "oos" : s.status;
  const label = s.out_of_service ? "out of service" : s.status;
  return `<div class="spot ${cls} clickable" onclick="openSpotModal('${escapeAttr(s.id)}')">
    <span>${escapeHtml(s.id)}</span><span class="tag">${label}</span>
  </div>`;
}

// ---------- Modals ----------
function modalHTML() {
  const m = state.modal;
  if (m.type === "spot") return spotModalHTML(m);
  if (m.type === "lot") return lotModalHTML(m);
  if (m.type === "confirm") return confirmModalHTML(m);
  return "";
}

function spotModalHTML(m) {
  const s = state.adminSpots.find((x) => x.id === m.spotId) || {};
  const lot = state.manageLotId;
  const oos = s.out_of_service;
  const pillCls = oos ? "sp-oos" : (s.status === "occupied" ? "sp-occupied" : "sp-free");
  const pillTxt = oos ? "Out of service" : (s.status || "free");
  return `
    <div class="overlay" onclick="if(event.target===this)closeModal()">
      <div class="modal">
        <h3>Spot ${escapeHtml(m.spotId)}</h3>
        <p class="sub">${escapeHtml(lot)} · current status
          <span class="status-pill ${pillCls}"><i></i>${escapeHtml(pillTxt)}</span></p>
        <div class="actions">
          <button class="btn-sm" onclick="spotAction('${escapeAttr(m.spotId)}','free')">Set free</button>
          <button class="btn-sm" onclick="spotAction('${escapeAttr(m.spotId)}','occupied')">Set occupied</button>
          ${oos
            ? `<button class="btn-sm primary" onclick="spotAction('${escapeAttr(m.spotId)}','in_service')">Return to service</button>`
            : `<button class="btn-sm" onclick="spotAction('${escapeAttr(m.spotId)}','out_of_service')">Mark out of service</button>`}
        </div>
        <div class="row">
          <button class="btn-sm danger" onclick="deleteSpot('${escapeAttr(m.spotId)}')">Delete spot</button>
          <button class="btn-sm" onclick="closeModal()">Close</button>
        </div>
      </div>
    </div>`;
}

function lotModalHTML(m) {
  const editing = !!m.lotId;
  const lot = editing ? state.lots.find((l) => l.lot_id === m.lotId) || {} : {};
  return `
    <div class="overlay" onclick="if(event.target===this)closeModal()">
      <form class="modal" id="lotForm" onsubmit="return submitLot(event)">
        <h3>${editing ? "Edit lot" : "New lot"}</h3>
        <p class="sub">${editing ? "Update lot details." : "Create a parking lot admins can manage."}</p>
        ${m.error ? `<div class="alert" style="margin:0 0 14px">${escapeHtml(m.error)}</div>` : ""}
        <div class="field">
          <label>Lot ID</label>
          <input class="input" id="f_lot_id" value="${escapeAttr(editing ? lot.lot_id : "")}"
            ${editing ? "disabled" : ""} placeholder="e.g. north_garage" required>
        </div>
        <div class="field"><label>Name</label>
          <input class="input" id="f_name" value="${escapeAttr(lot.name || "")}" placeholder="North Garage" required></div>
        <div class="field"><label>Address</label>
          <input class="input" id="f_address" value="${escapeAttr(lot.address || "")}" placeholder="Level 2"></div>
        <div class="field"><label>Total spots (planned)</label>
          <input class="input" id="f_total" type="number" min="0" value="${lot.total_spots || 0}"></div>
        <div class="row">
          <button type="button" class="btn-sm" onclick="closeModal()">Cancel</button>
          <button type="submit" class="btn-sm primary">${editing ? "Save" : "Create lot"}</button>
        </div>
      </form>
    </div>`;
}

function confirmModalHTML(m) {
  return `
    <div class="overlay" onclick="if(event.target===this)closeModal()">
      <div class="modal">
        <h3>${escapeHtml(m.title)}</h3>
        <p class="sub">${escapeHtml(m.body)}</p>
        <div class="row">
          <button class="btn-sm" onclick="closeModal()">Cancel</button>
          <button class="btn-sm danger" onclick="${m.onConfirm}">${escapeHtml(m.confirmLabel || "Delete")}</button>
        </div>
      </div>
    </div>`;
}

// ---------- Spots & Lots actions ----------
function selectManageLot(lotId) {
  state.manageLotId = lotId;
  loadAdminSpots(lotId).then(() => render());
}
function openSpotModal(spotId) { state.modal = { type: "spot", spotId }; render(); }
function openLotModal(lotId) { state.modal = { type: "lot", lotId: lotId || null }; render(); }
function closeModal() { state.modal = null; render(); }

async function spotAction(spotId, action) {
  try {
    await api("/api/admin/spots/status", {
      method: "POST",
      body: JSON.stringify({ lot_id: state.manageLotId, spot_id: spotId, action }),
    });
    state.modal = null;
    await refreshSpotsView();
  } catch (e) { alert(e.message); }
}
async function deleteSpot(spotId) {
  try {
    await api("/api/admin/spots", {
      method: "DELETE",
      body: JSON.stringify({ lot_id: state.manageLotId, spot_id: spotId }),
    });
    state.modal = null;
    await refreshSpotsView();
  } catch (e) { alert(e.message); }
}
async function addSpotPrompt() {
  const id = prompt("New spot ID (e.g. S28):");
  if (!id) return;
  try {
    await api("/api/admin/spots", {
      method: "POST",
      body: JSON.stringify({ lot_id: state.manageLotId, spot_id: id.trim() }),
    });
    await refreshSpotsView();
  } catch (e) { alert(e.message); }
}
async function submitLot(e) {
  e.preventDefault();
  const editing = !!state.modal.lotId;
  const payload = {
    name: document.getElementById("f_name").value.trim(),
    address: document.getElementById("f_address").value.trim(),
    total_spots: parseInt(document.getElementById("f_total").value, 10) || 0,
  };
  if (!editing) payload.lot_id = document.getElementById("f_lot_id").value.trim();
  try {
    if (editing) {
      await api(`/api/admin/lots/${encodeURIComponent(state.modal.lotId)}`, { method: "PUT", body: JSON.stringify(payload) });
    } else {
      await api("/api/admin/lots", { method: "POST", body: JSON.stringify(payload) });
      state.manageLotId = payload.lot_id;
    }
    state.modal = null;
    await loadSpotsView();
  } catch (err) {
    state.modal.error = err.message;
    render();
  }
  return false;
}
function confirmDeleteLot(lotId) {
  state.modal = {
    type: "confirm",
    title: "Delete lot?",
    body: `This permanently removes "${lotId}" and all of its spots. This cannot be undone.`,
    confirmLabel: "Delete lot",
    onConfirm: `deleteLot('${escapeAttr(lotId)}')`,
  };
  render();
}
async function deleteLot(lotId) {
  try {
    await api(`/api/admin/lots/${encodeURIComponent(lotId)}`, { method: "DELETE" });
    state.modal = null;
    if (state.manageLotId === lotId) state.manageLotId = null;
    await loadSpotsView();
  } catch (e) { alert(e.message); }
}

// ---------- Overview ----------
function overviewHTML() {
  return `
    <section class="kpi-grid" id="kpiGrid">${kpiCardsHTML()}</section>
    <section class="two-col">
      <div class="card panel">
        <div class="panel-head">
          <h3>Live lot map · <span class="mono" style="color:var(--muted)">${escapeHtml(state.lotId)}</span></h3>
          ${mapLegendHTML()}
        </div>
        <div id="lotmap">${overviewMapHTML()}</div>
      </div>
      <div class="card panel">
        <div class="panel-head"><h3>Lot summary</h3></div>
        <div id="lotSummary">${lotSummaryHTML()}</div>
      </div>
    </section>`;
}

function kpiCardsHTML() {
  const o = state.overview;
  const occ = o?.occupancy || { total: 0, free: 0, occupied: 0, percent: 0 };
  const users = o?.users || { total: 0, admins: 0 };
  return `
    ${kpiCard("accent", I.gauge, "Occupancy", `${occ.percent}%`,
       `<div class="bar"><i style="width:${occ.percent}%"></i></div>`, "occVal", "occBar")}
    ${kpiCard("", I.car, "Free spots", occ.free, `${occ.total} total`, "freeVal", null, "freeMeta")}
    ${kpiCard("danger", I.car, "Occupied", occ.occupied, "right now", "occupiedVal")}
    ${kpiCard("info", I.clock, "Active sessions", o?.sessions?.active ?? 0, "in progress", "activeVal")}
    ${kpiCard("", I.users, "Users", users.total, `${users.admins} admin${users.admins === 1 ? "" : "s"}`, "usersVal", null, "usersMeta")}
    ${kpiCard("warn", I.flag, "Open reports", o?.reports?.open ?? 0, "awaiting triage", "reportsVal")}`;
}

function kpiCard(tone, icon, label, val, extra, valId, barId, metaId) {
  const extraHTML = barId
    ? `<div class="bar" id="${barId}"><i style="width:${state.overview?.occupancy?.percent || 0}%"></i></div>`
    : (metaId ? `<div class="meta" id="${metaId}">${extra}</div>` : `<div class="meta">${extra}</div>`);
  return `
    <div class="card kpi ${tone}">
      <div class="top"><span class="label">${label}</span><span class="ico">${icon}</span></div>
      <div class="val" ${valId ? `id="${valId}"` : ""}>${val}</div>
      ${extraHTML}
    </div>`;
}

function overviewMapHTML() {
  if (!state.spots.length) return `<div class="empty">No spot data yet for this lot.</div>`;
  if (lotUsesMap(state.lotId, state.spots)) {
    return `<div class="lotmap-wrap">${lotMapSvg(state.spots)}</div>`;
  }
  // Lot without a known physical layout → simple grid.
  return `<div class="lotmap">${state.spots.map(gridTileHTML).join("")}</div>`;
}
function gridTileHTML(s) {
  const cls = s.out_of_service ? "oos" : s.status;
  const label = s.out_of_service ? "out of service" : s.status;
  return `<div class="spot ${cls}" id="spot-${cssId(s.id)}">
    <span>${escapeHtml(s.id)}</span><span class="tag">${label}</span>
  </div>`;
}

function lotSummaryHTML() {
  const o = state.overview;
  const occ = o?.occupancy || {};
  return `
    <div class="info-row"><span class="k">Lot</span><span class="v">${escapeHtml(state.lotId)}</span></div>
    <div class="info-row"><span class="k">Total spaces</span><span class="v">${occ.total ?? state.spots.length}</span></div>
    <div class="info-row"><span class="k">Available</span><span class="v" style="color:var(--success)">${occ.free ?? freeCount()}</span></div>
    <div class="info-row"><span class="k">Occupied</span><span class="v" style="color:var(--danger)">${occ.occupied ?? occupiedCount()}</span></div>
    <div class="info-row"><span class="k">Active sessions</span><span class="v">${o?.sessions?.active ?? 0}</span></div>
    <div class="info-row"><span class="k">Open reports</span><span class="v">${o?.reports?.open ?? 0}</span></div>
    <div class="info-row"><span class="k">Last refresh</span><span class="v mono" style="font-size:12px">${nowTime()}</span></div>`;
}

// ---------- live DOM patches (avoid full re-render on socket ticks) ----------
function renderLotMap() {
  const el = document.getElementById("lotmap");
  if (el) el.innerHTML = overviewMapHTML();
}
function recomputeOccupancy() {
  if (!state.overview) return;
  const total = state.spots.length;
  const occupied = occupiedCount();
  const free = total - occupied;
  const percent = total ? Math.round((occupied / total) * 100) : 0;
  state.overview.occupancy = { ...state.overview.occupancy, total, occupied, free, percent };
  updateKpis();
}
function updateKpis() {
  const o = state.overview; if (!o) return;
  const occ = o.occupancy || {};
  setText("occVal", `${occ.percent ?? 0}%`);
  setBar("occBar", occ.percent ?? 0);
  setText("freeVal", occ.free ?? 0);
  setText("freeMeta", `${occ.total ?? 0} total`);
  setText("occupiedVal", occ.occupied ?? 0);
  setText("activeVal", o.sessions?.active ?? 0);
  setText("usersVal", o.users?.total ?? 0);
  setText("usersMeta", `${o.users?.admins ?? 0} admin${(o.users?.admins ?? 0) === 1 ? "" : "s"}`);
  setText("reportsVal", o.reports?.open ?? 0);
  const sum = document.getElementById("lotSummary");
  if (sum) sum.innerHTML = lotSummaryHTML();
}
function updateLiveDot() {
  const el = document.getElementById("liveDot");
  if (!el) return;
  el.className = `live-dot ${state.socketConnected ? "" : "off"}`;
  el.innerHTML = `<span class="dot"></span>${state.socketConnected ? "Live" : "Offline"}`;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
const freeCount = () => state.spots.filter((s) => s.status === "free").length;
const occupiedCount = () => state.spots.filter((s) => s.status === "occupied").length;
function setText(id, v) { const el = document.getElementById(id); if (el) el.textContent = v; }
function setBar(id, pct) { const el = document.getElementById(id); if (el) el.querySelector("i").style.width = `${pct}%`; }
function initials(name) {
  return name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("") || "A";
}
function nowTime() { return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }); }
function cssId(id) { return String(id).replace(/[^a-zA-Z0-9_-]/g, "_"); }
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function escapeAttr(s) { return escapeHtml(s).replace(/`/g, "&#96;"); }

// expose handlers used in inline attributes
window.navigate = navigate;
window.logout = logout;
window.selectManageLot = selectManageLot;
window.openSpotModal = openSpotModal;
window.openLotModal = openLotModal;
window.closeModal = closeModal;
window.spotAction = spotAction;
window.deleteSpot = deleteSpot;
window.addSpotPrompt = addSpotPrompt;
window.submitLot = submitLot;
window.confirmDeleteLot = confirmDeleteLot;
window.deleteLot = deleteLot;

// Periodic overview refresh (sessions/users/reports don't arrive over the socket).
setInterval(() => { if (state.view !== "login") loadOverview(); }, 30000);

boot();
