// IDSS leaderboard page: reads the board the game reads (lb/boards.json + boards.txt) from the repo and the runs' ghost
// files (.idg) for the course map. No server: everything is fetched from raw.githubusercontent.com.
'use strict';
const REPO = 'cassnadra/initialdpsponpc';
const RAW = `https://raw.githubusercontent.com/${REPO}/main/lb/`;
// A button that asks the board to update needs a small service holding the token (a static page can't keep one secret).
// Set its URL here to show the button; it should answer {ok, wait_s} and keep the shared cool-down itself.
const REFRESH_URL = '';

const CARS = ['TRUENO GT-APEX (AE86)', 'LEVIN GT-APEX (AE86)', 'LEVIN SR (AE85)', 'MR2 G-Limited (SW20)', 'MR-S S EDITION (ZZW30)',
  'ALTEZZA RS-200 (SXE10)', 'CELICA GT-FOUR (ST205)', 'SKYLINE GT-R V-spec II (BNR32)', 'SKYLINE GT-R V-spec II (BNR34)',
  "SILVIA K's (S13)", "SILVIA Q's (S14)", "SILVIA K's AERO (S14)", 'SILVIA spec-R (S15)', '180SX TYPE X (RPS13)', 'SILEIGHTY',
  'CIVIC TYPE R (EK9)', 'CIVIC SiR II (EG6)', 'INTEGRA TYPE R (DC2)', 'S2000 (AP1)', 'LANCER GSR EVOLUTION III (CE9A)',
  'LANCER RS EVOLUTION IV (CN9A)', 'LANCER EVOLUTION VII GSR (CT9A)', 'RX-7 Type R (FD3S)', 'RX-7 SPIRIT R TypeA (FD3S)',
  'RX-7 Infini III (FC3S)', 'ROADSTER S Special (NA6CE)', 'ROADSTER RS (NB8C)', 'IMPREZA WRX STi Version VI (GC8)',
  'IMPREZA WRX STi (GDB)', 'IMPREZA WRX type R STi Version V (Bunta)', 'CAPPUCCINO (EA11R)', 'SKYLINE 25GT TURBO (ER34)',
  'LANCER RS EVOLUTION V (CP9A)', 'LANCER GSR EVOLUTION VI TM EDITION (CP9A)', 'RX-8 Type S (SE3P)'];
const COURSE_NAMES = { akina: 'Akina', akagi: 'Akagi', myougi: 'Myogi', usui: 'Usui' };
const COURSE_KANJI = { akina: '秋名', akagi: '赤城', myougi: '妙義', usui: '碓氷' };
const COLORS = ['#ffcc00', '#1d6bff', '#e3141f', '#b04dff', '#ff7a00', '#00b37a'];   // (readable on the green minimap)
const MAX_ON_MAP = 6;

const $ = id => document.getElementById(id);
const state = { boards: {}, profiles: {}, course: null, dir: 0, wet: 0, search: '', sort: 'time', car: '', steamOnly: false, picked: [] };

// ---------------- formatting ----------------
function fmtTime(ms) {
  const m = Math.floor(ms / 60000), s = Math.floor(ms / 1000) % 60, r = Math.round(ms % 1000);
  return `${m}'${String(s).padStart(2, '0')}"${String(r).padStart(3, '0')}`;   // (as the game shows it: 2'57"466)
}
const carName = c => (c >= 0 && c < CARS.length ? CARS[c] : 'Unknown car');
const courseName = c => COURSE_NAMES[c] || c.charAt(0).toUpperCase() + c.slice(1);
function boardId(course, dir, wet) { return `${course}-${dir}${wet ? '-wet' : ''}`; }
// what a board's direction is called: downhill / uphill, or normal / reverse on circuits
function dirName(course, dir) {
  for (const w of [0, 1]) {
    const b = state.boards[boardId(course, dir, w)];
    if (!b) continue;
    if (/downhill/i.test(b.label)) return 'Downhill';
    if (/uphill/i.test(b.label)) return 'Uphill';
    return /reverse/i.test(b.label) ? 'Reverse' : 'Normal';
  }
  const other = dir ? 0 : 1;   // only the other direction has times: name this one from it
  for (const w of [0, 1]) {
    const b = state.boards[boardId(course, other, w)];
    if (!b) continue;
    if (/downhill/i.test(b.label)) return 'Uphill';
    if (/uphill/i.test(b.label)) return 'Downhill';
    return dir ? 'Reverse' : 'Normal';
  }
  return dir ? 'Uphill' : 'Downhill';
}

// ---------------- data ----------------
async function load() {
  const [js, txt] = await Promise.all([
    fetch(RAW + 'boards.json', { cache: 'no-cache' }).then(r => r.json()),
    fetch(RAW + 'boards.txt', { cache: 'no-cache' }).then(r => r.text()).catch(() => ''),
  ]);
  state.boards = js;
  for (const line of txt.split('\n')) {   // P pid pic car cards play_s name bio car_img discord steam steam_ok
    const f = line.split('\t');
    if (f[0] === 'P') state.profiles[f[1]] = { name: f[6], bio: f[7], carImg: f[8], discord: f[9], steam: f[10] };
  }
  loadUpdated();
}
async function loadUpdated() {
  try {
    const r = await fetch(`https://api.github.com/repos/${REPO}/commits?path=lb/boards.json&per_page=1`);
    const c = (await r.json())[0];
    const when = new Date(c.commit.committer.date);
    $('updated').textContent = `Board updated ${ago(when)}`;
    $('updated').title = when.toLocaleString();
  } catch { $('updated').textContent = ''; }
}
function ago(d) {
  const s = (Date.now() - d) / 1000;
  if (s < 90) return 'just now';
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

// ghost files: header 28 + 32 bytes, then 40-byte samples (t in 1/60 s since GO, x y z, right, forward)
const ghostCache = new Map();
function loadGhost(path) {
  if (!ghostCache.has(path)) ghostCache.set(path, fetch(RAW + path).then(r => {
    if (!r.ok) throw new Error(r.status);
    return r.arrayBuffer();
  }).then(buf => {
    const v = new DataView(buf), n = v.getInt32(24, true), base = 60;
    if (n < 2 || base + n * 40 > buf.byteLength) throw new Error('bad ghost');
    const t = new Float32Array(n), x = new Float32Array(n), z = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      const o = base + i * 40;
      t[i] = v.getFloat32(o, true) / 60; x[i] = v.getFloat32(o + 4, true); z[i] = v.getFloat32(o + 12, true);
    }
    return { t, x, z, n };
  }));
  return ghostCache.get(path);
}

// ---------------- courses ----------------
function courses() {
  const m = new Map();
  for (const [id, b] of Object.entries(state.boards)) {
    if (!m.has(b.course)) m.set(b.course, { course: b.course, boards: [], entries: 0, best: null });
    const c = m.get(b.course);
    c.boards.push(id); c.entries += b.entries.length;
  }
  return [...m.values()].sort((a, b) => b.entries - a.entries || a.course.localeCompare(b.course));
}
function playerOn(course, q) {   // the searched player has a time on this course
  if (!q) return false;
  return Object.values(state.boards).some(b => b.course === course && b.entries.some(e => e.shown.toLowerCase().includes(q)));
}
function drawCourses() {
  const ul = $('courseList'); ul.textContent = '';
  const q = state.search.toLowerCase();
  for (const c of courses()) {
    const li = document.createElement('li'), b = document.createElement('button');
    b.className = c.course === state.course ? 'on' : '';
    const arrow = document.createElement('span'); arrow.className = 'arrow'; arrow.textContent = '▶';
    const n = document.createElement('span'); n.className = 'cname'; n.textContent = courseName(c.course);
    const kj = document.createElement('span'); kj.className = 'kanji'; kj.lang = 'ja'; kj.textContent = COURSE_KANJI[c.course] || '';
    const meta = document.createElement('span'); meta.className = 'cmeta';
    meta.textContent = `${c.entries} time${c.entries === 1 ? '' : 's'} · ${c.boards.length} board${c.boards.length === 1 ? '' : 's'}`;
    if (playerOn(c.course, q)) { const h = document.createElement('span'); h.className = 'hit'; h.textContent = ' · found'; meta.append(h); }
    b.append(arrow, n, kj, meta);
    b.onclick = () => { state.course = c.course; pickDefaults(); render(); document.body.classList.remove('drawer'); };
    li.append(b); ul.append(li);
  }
}
// on a course change: a direction / road that has times
function pickDefaults() {
  if (!state.boards[boardId(state.course, state.dir, state.wet)]) {
    const opts = [[state.dir, 0], [state.dir ? 0 : 1, state.wet], [0, 0], [1, 0], [0, 1], [1, 1]];
    for (const [d, w] of opts) if (state.boards[boardId(state.course, d, w)]) { state.dir = d; state.wet = w; break; }
  }
  state.picked = null;   // (the board's top 3, once it loads)
}

// ---------------- table ----------------
function currentBoard() { return state.boards[boardId(state.course, state.dir, state.wet)]; }
function visibleEntries(b) {
  if (!b) return [];
  const q = state.search.toLowerCase();
  let es = b.entries.filter(e => (!q || e.shown.toLowerCase().includes(q)) && (state.car === '' || e.car === +state.car) && (!state.steamOnly || e.steam_verified));
  const by = {
    time: (a, c) => a.time_ms - c.time_ms,
    date: (a, c) => c.date.localeCompare(a.date) || a.time_ms - c.time_ms,
    name: (a, c) => a.shown.localeCompare(c.shown, undefined, { sensitivity: 'base' }),
    car: (a, c) => carName(a.car).localeCompare(carName(c.car)) || a.time_ms - c.time_ms,
  }[state.sort];
  return es.sort(by);
}
function drawBoard() {
  const b = currentBoard();
  $('boardTitle').textContent = `${courseName(state.course)} · ${dirName(state.course, state.dir)}${state.wet ? ' · Wet' : ''}`;
  const es = visibleEntries(b), total = b ? b.entries.length : 0;
  $('boardCount').textContent = b ? (es.length === total ? `${total} driver${total === 1 ? '' : 's'}` : `${es.length} of ${total} shown`) : '';
  const tb = $('rows'); tb.textContent = '';
  $('empty').hidden = es.length > 0;
  $('empty').textContent = !b ? 'No times on this board yet.' : 'No times match the filters.';
  const lead = b && b.entries.length ? b.entries[0].time_ms : 0;
  for (const e of es) {
    const tr = document.createElement('tr');
    const picked = state.picked || [];
    tr.className = (e.rank <= 3 ? `r${e.rank}` : '') + (picked.includes(e.pid) ? ' sel' : '');
    const cell = (cls, content) => { const td = document.createElement('td'); if (cls) td.className = cls; if (content instanceof Node) td.append(content); else td.textContent = content; tr.append(td); return td; };
    const cb = document.createElement('input'); cb.type = 'checkbox'; cb.checked = picked.includes(e.pid);
    cb.title = 'Show this run on the course map';
    cb.onchange = () => togglePick(e.pid, cb.checked);
    const mapCell = cell('c-map', cb);
    const pi = picked.indexOf(e.pid);
    if (pi >= 0) { const sw = document.createElement('span'); sw.className = 'swatch'; sw.style.background = COLORS[pi % COLORS.length]; mapCell.prepend(sw); }
    const rk = document.createElement('span'); rk.textContent = String(e.rank);
    const ord = document.createElement('span'); ord.className = 'ord';
    ord.textContent = e.rank % 100 >= 11 && e.rank % 100 <= 13 ? 'th' : ({ 1: 'st', 2: 'nd', 3: 'rd' }[e.rank % 10] || 'th');
    const rc = cell('c-rank', rk); rc.append(ord);
    const who = document.createElement('div'); who.className = 'who';
    const prof = state.profiles[e.pid];
    if (prof && prof.carImg) who.append(carPic(`${RAW}cars/${prof.carImg}.png`));
    else who.append(Object.assign(document.createElement('span'), { className: 'ph' }));
    const nm = document.createElement('span'); nm.textContent = e.shown.replace(/ \(Steam\)$/, '');
    who.append(nm);
    if (e.steam_verified) { const s = document.createElement('span'); s.className = 'badge'; s.textContent = 'Steam'; s.title = 'Steam account verified'; who.append(s); }
    if (prof && prof.bio) who.title = prof.bio;
    cell('', who);
    cell('c-time', fmtTime(e.time_ms));
    cell('c-gap', e.time_ms === lead ? '' : `+${((e.time_ms - lead) / 1000).toFixed(3)}`);
    cell('c-car', carName(e.car));
    cell('c-date', e.date);
    tr.onclick = ev => { if (ev.target !== cb) { cb.checked = !cb.checked; togglePick(e.pid, cb.checked); } };
    if (picked.includes(e.pid)) tr.querySelector('.c-rank').style.boxShadow = `inset 4px 0 0 ${COLORS[pi % COLORS.length]}`;
    tb.append(tr);
  }
}
// their car picture: a turntable sheet of 12 angles (4 x 3); one angle, turning while the mouse is over it
function carPic(url) {
  const d = document.createElement('span'); d.className = 'pic'; d.style.backgroundImage = `url("${url}")`;
  let f = 1, timer = 0;
  const show = () => { d.style.backgroundPosition = `${(f % 4) * 100 / 3}% ${Math.floor(f / 4) * 50}%`; };
  show();
  d.onmouseenter = () => { timer = setInterval(() => { f = (f + 1) % 12; show(); }, 110); };
  d.onmouseleave = () => { clearInterval(timer); f = 1; show(); };
  return d;
}
function togglePick(pid, on) {
  state.picked = (state.picked || []).filter(p => p !== pid);
  if (on) state.picked.push(pid);
  if (state.picked.length > MAX_ON_MAP) state.picked.shift();
  drawBoard(); loadMap();
}

// ---------------- filters ----------------
function drawFilters() {
  for (const btn of $('dirSeg').children) {
    const d = +btn.dataset.dir;
    btn.textContent = dirName(state.course, d);
    btn.className = (d === state.dir ? 'on' : '') + (Object.keys(state.boards).some(id => id.startsWith(`${state.course}-${d}`)) ? '' : ' none');
  }
  for (const btn of $('wetSeg').children) {
    const w = +btn.dataset.wet;
    btn.className = (w === state.wet ? 'on' : '') + (state.boards[boardId(state.course, state.dir, w)] ? '' : ' none');
  }
  const b = currentBoard(), sel = $('carFilter'), keep = state.car;
  sel.textContent = '';
  sel.append(new Option('Any car', ''));
  const cars = [...new Set((b ? b.entries : []).map(e => e.car))].sort((a, c) => carName(a).localeCompare(carName(c)));
  for (const c of cars) sel.append(new Option(carName(c), String(c)));
  if (keep !== '' && !cars.includes(+keep)) state.car = '';
  sel.value = state.car;
}
function wire() {
  $('search').oninput = e => { state.search = e.target.value.trim(); drawCourses(); drawBoard(); saveHash(); };
  for (const btn of $('dirSeg').children) btn.onclick = () => { state.dir = +btn.dataset.dir; state.picked = null; render(); };
  for (const btn of $('wetSeg').children) btn.onclick = () => { state.wet = +btn.dataset.wet; state.picked = null; render(); };
  $('sort').onchange = e => { state.sort = e.target.value; drawBoard(); saveHash(); };
  $('carFilter').onchange = e => { state.car = e.target.value; drawBoard(); saveHash(); };
  $('steamOnly').onchange = e => { state.steamOnly = e.target.checked; drawBoard(); };
  $('play').onclick = () => { replay.playing ? pause() : play(); };
  $('scrub').oninput = e => { replay.t = (e.target.value / 1000) * replay.len; pause(); drawMap(); };
  $('speed').onchange = e => { replay.speed = +e.target.value; };
  window.addEventListener('resize', () => drawMap());
  // phones: Options folds out the courses + filters; Map preview shows the map as a small window (remembered)
  $('courseBtn').onclick = () => document.body.classList.add('drawer');
  $('scrim').onclick = () => document.body.classList.remove('drawer');
  document.addEventListener('keydown', ev => { if (ev.key === 'Escape') document.body.classList.remove('drawer'); });
  $('optBar').onclick = () => { const on = document.body.classList.toggle('opts'); $('optBar').setAttribute('aria-expanded', on); };
  let mapOn = false; try { mapOn = localStorage.getItem('lb_map') === '1'; } catch {}
  $('mapOn').checked = mapOn; document.body.classList.toggle('mapon', mapOn);
  $('mapOn').onchange = e => {
    document.body.classList.toggle('mapon', e.target.checked);
    try { localStorage.setItem('lb_map', e.target.checked ? '1' : '0'); } catch {}
    drawMap();
  };
  $('map').onclick = () => { if (!$('play').disabled) replay.playing ? pause() : play(); };
  if (REFRESH_URL) {
    const r = $('refresh'); r.hidden = false;
    r.onclick = async () => {
      r.disabled = true;
      try {
        const res = await (await fetch(REFRESH_URL, { method: 'POST' })).json();
        r.textContent = res.ok ? 'Update asked for' : `Try again in ${Math.ceil((res.wait_s || 60) / 60)} min`;
      } catch { r.textContent = 'Could not ask'; }
      setTimeout(() => { r.disabled = false; r.textContent = 'Update board'; }, 60000);
    };
  }
}
function saveHash() {
  const p = new URLSearchParams({ c: state.course, d: state.dir, w: state.wet });
  if (state.search) p.set('q', state.search);
  if (state.sort !== 'time') p.set('s', state.sort);
  if (state.car !== '') p.set('car', state.car);
  history.replaceState(null, '', '#' + p);
}
function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  if (p.get('c')) state.course = p.get('c');
  state.dir = +(p.get('d') || 0); state.wet = +(p.get('w') || 0);
  state.t0 = +(p.get('t') || 0); state.search = p.get('q') || ''; state.sort = p.get('s') || 'time'; state.car = p.get('car') || '';
  $('search').value = state.search; $('sort').value = state.sort;
}

// ---------------- course map + ghost replay ----------------
const replay = { runs: [], len: 0, t: 0, playing: false, speed: 4, last: 0, view: null };
let mapToken = 0;
async function loadMap() {
  const b = currentBoard(), token = ++mapToken;
  pause(); replay.runs = []; replay.t = 0; replay.len = 0;
  $('legend').textContent = '';
  if (!b || !b.entries.length) { replay.track = null; replay.view = null; $('mapNote').textContent = 'no runs yet'; setPlayer(false); drawMap(); return; }
  if (!state.picked) state.picked = b.entries.slice(0, Math.min(3, b.entries.length)).map(e => e.pid);
  const picks = state.picked.map(pid => b.entries.find(e => e.pid === pid)).filter(Boolean);
  $('mapNote').textContent = 'loading runs...';
  // the course line: the leader's run
  const all = await Promise.all([b.entries[0], ...picks].map(e => loadGhost(e.ghost).catch(() => null)));
  if (token !== mapToken) return;
  replay.track = all[0];
  replay.runs = picks.map((e, i) => ({ e, g: all[i + 1], color: COLORS[i % COLORS.length] })).filter(r => r.g);
  replay.len = Math.max(0, ...replay.runs.map(r => r.g.t[r.g.n - 1]));
  replay.view = viewOf([replay.track, ...replay.runs.map(r => r.g)].filter(Boolean));
  $('mapNote').textContent = replay.runs.length ? `${replay.runs.length} run${replay.runs.length === 1 ? '' : 's'} on the map` : 'tick a run to show it';
  setPlayer(replay.runs.length > 0);
  for (const r of replay.runs) {
    const li = document.createElement('li');
    const l = document.createElement('span'); const sw = document.createElement('span'); sw.className = 'swatch'; sw.style.background = r.color;
    l.append(sw, `${r.e.rank}. ${r.e.shown.replace(/ \(Steam\)$/, '')}`);
    const g = document.createElement('span'); g.className = 'gapnow'; r.gapEl = g;
    li.append(l, g); $('legend').append(li);
  }
  if (state.t0) { replay.t = Math.min(state.t0, replay.len); state.t0 = 0; }   // (a link to a moment: #...&t=SECONDS)
  drawBoard(); drawMap();
}
function setPlayer(on) { $('play').disabled = !on; $('scrub').disabled = !on; $('play').textContent = 'Play'; }
function viewOf(gs) {
  let x0 = Infinity, x1 = -Infinity, z0 = Infinity, z1 = -Infinity;
  for (const g of gs) for (let i = 0; i < g.n; i++) { x0 = Math.min(x0, g.x[i]); x1 = Math.max(x1, g.x[i]); z0 = Math.min(z0, g.z[i]); z1 = Math.max(z1, g.z[i]); }
  return { x0, x1, z0, z1 };
}
function posAt(g, t) {   // where a run was t seconds after GO
  if (t <= g.t[0]) return [g.x[0], g.z[0], false];
  if (t >= g.t[g.n - 1]) return [g.x[g.n - 1], g.z[g.n - 1], true];
  let lo = 0, hi = g.n - 1;
  while (hi - lo > 1) { const m = (lo + hi) >> 1; if (g.t[m] <= t) lo = m; else hi = m; }
  const f = (t - g.t[lo]) / Math.max(1e-6, g.t[hi] - g.t[lo]);
  return [g.x[lo] + (g.x[hi] - g.x[lo]) * f, g.z[lo] + (g.z[hi] - g.z[lo]) * f, false];
}
function drawMap() {
  const cv = $('map'), dpr = window.devicePixelRatio || 1, w = cv.clientWidth, h = cv.clientHeight;
  cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
  const c = cv.getContext('2d'); c.setTransform(dpr, 0, 0, dpr, 0, 0); c.clearRect(0, 0, w, h);
  const v = replay.view;
  if (!v || !replay.track) {
    c.fillStyle = '#3b4a40'; c.font = 'italic 700 16px "Barlow Condensed", sans-serif'; c.textAlign = 'center';
    c.fillText(currentBoard() ? 'Loading the course...' : 'No runs on this board yet', w / 2, h / 2); return;
  }
  const pad = 18, sx = (w - pad * 2) / Math.max(1, v.x1 - v.x0), sz = (h - pad * 2) / Math.max(1, v.z1 - v.z0), s = Math.min(sx, sz);
  const ox = (w - (v.x1 - v.x0) * s) / 2, oz = (h - (v.z1 - v.z0) * s) / 2;
  const P = (x, z) => [ox + (x - v.x0) * s, oz + (z - v.z0) * s];
  const line = (g, style, width) => {
    c.beginPath();
    for (let i = 0; i < g.n; i += 2) { const [px, pz] = P(g.x[i], g.z[i]); i ? c.lineTo(px, pz) : c.moveTo(px, pz); }
    c.strokeStyle = style; c.lineWidth = width; c.lineJoin = c.lineCap = 'round'; c.stroke();
  };
  // the road as the in-race minimap draws it: a dark line on the pale green
  line(replay.track, 'rgba(255,255,255,0.55)', 7);
  line(replay.track, '#1c231f', 3.5);
  const tg = replay.track, [sxp, szp] = P(tg.x[0], tg.z[0]), [fx, fz] = P(tg.x[tg.n - 1], tg.z[tg.n - 1]);
  const flag = (x, y, col, txt) => {
    c.fillStyle = col; c.strokeStyle = '#000'; c.lineWidth = 1.5; c.fillRect(x - 5, y - 5, 10, 10); c.strokeRect(x - 5, y - 5, 10, 10);
    c.font = 'italic 800 13px "Barlow Condensed", sans-serif'; c.lineWidth = 3; c.strokeStyle = 'rgba(255,255,255,.85)';
    const t = txt.toUpperCase(), right = x + 9 + c.measureText(t).width < w - 4;   // (near the right edge: on the left)
    c.textAlign = right ? 'left' : 'right'; const tx = right ? x + 9 : x - 9;
    c.strokeText(t, tx, y + 4); c.fillStyle = '#111'; c.fillText(t, tx, y + 4);
  };
  if (Math.hypot(tg.x[0] - tg.x[tg.n - 1], tg.z[0] - tg.z[tg.n - 1]) < 150) flag(sxp, szp, '#22c55e', 'Start / Finish');   // (a circuit)
  else { flag(sxp, szp, '#22c55e', 'Start'); flag(fx, fz, '#e3141f', 'Finish'); }
  // the cars at the replay time, with the gap to the leader of the picked runs (time behind at the same distance is
  // hard to get without the route, so: how far behind in seconds = when they reach the leader's spot)
  for (const r of replay.runs) {
    const [x, z, done] = posAt(r.g, replay.t), [px, pz] = P(x, z);
    c.beginPath(); c.arc(px, pz, 7, 0, 7); c.fillStyle = r.color; c.fill();
    c.lineWidth = 2; c.strokeStyle = '#000'; c.stroke();
    if (r.gapEl) r.gapEl.textContent = done ? fmtTime(r.e.time_ms) : '';
  }
  $('clock').textContent = fmtTime(Math.max(0, replay.t) * 1000);
  $('scrub').value = replay.len ? Math.round((replay.t / replay.len) * 1000) : 0;
}
function play() {
  if (!replay.runs.length) return;
  if (replay.t >= replay.len) replay.t = 0;
  replay.playing = true; replay.last = performance.now(); $('play').textContent = 'Pause';
  requestAnimationFrame(step);
}
function pause() { replay.playing = false; if (!$('play').disabled) $('play').textContent = 'Play'; }
function step(now) {
  if (!replay.playing) return;
  replay.t += ((now - replay.last) / 1000) * replay.speed; replay.last = now;
  if (replay.t >= replay.len) { replay.t = replay.len; drawMap(); pause(); return; }
  drawMap(); requestAnimationFrame(step);
}

// ---------------- start ----------------
function render() { drawCourses(); drawFilters(); drawBoard(); saveHash(); loadMap(); }
(async () => {
  wire(); readHash();
  try { await load(); } catch (err) { $('updated').textContent = 'Could not load the board'; console.error(err); return; }
  const cs = courses();
  if (!cs.length) { $('updated').textContent = 'No times yet'; return; }
  if (!state.course || !cs.some(c => c.course === state.course)) state.course = cs[0].course;
  pickDefaults();
  render();
})();
