// Fadewatch dashboard: plays a compiled replay bundle (data/demo.json).
const D2R = Math.PI / 180, R2D = 180 / Math.PI;
const SEV = { critical: 0, warning: 1, info: 2 };
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const C = {
  ink: css("--ink"), ink2: css("--ink-2"), ink3: css("--ink-3"), rule: css("--rule"), panel: css("--panel"),
  ok: css("--ok"), thin: css("--thin"), fail: css("--fail"), info: css("--info"), fw: css("--fw"), pb: css("--pb"),
};
const RADAR = [[0.5, "#04e9e7"], [1.5, "#019ff4"], [3, "#02fd02"], [6, "#01c501"], [10, "#008e00"], [15, "#fdf802"],
  [22, "#e5bc00"], [30, "#fd9500"], [38, "#fd0000"], [48, "#d40000"], [60, "#f800fd"]];
const MODCODS = [["QPSK 1/4", -2.35, 0.49], ["QPSK 1/3", -1.24, 0.656], ["QPSK 1/2", 1.0, 0.989], ["QPSK 2/3", 3.1, 1.319],
  ["QPSK 3/4", 4.03, 1.484], ["QPSK 5/6", 5.18, 1.65], ["8PSK 2/3", 6.62, 1.98], ["8PSK 3/4", 7.91, 2.228],
  ["8PSK 5/6", 9.35, 2.479], ["16APSK 3/4", 10.21, 2.967], ["16APSK 5/6", 11.61, 3.3], ["16APSK 9/10", 12.89, 3.567],
  ["32APSK 3/4", 12.73, 3.703], ["32APSK 5/6", 14.28, 4.124], ["32APSK 9/10", 16.05, 4.453]];

const QS = new URLSearchParams(location.search), CAPTURE = QS.has("capture");
const S = {
  t: 0, i: -1, playing: true, speed: 30, selected: null, manualUntil: 0, followAt: 0,
  prevAlertIds: new Set(), closedLoop: false, view: "board", lastSat: 0,
};
let D, G, tick0, tickN, P;

// ---------------------------------------------------------------- utilities
const $ = (id) => document.getElementById(id);
const pad = (n) => String(Math.floor(n)).padStart(2, "0");
const hms = (t) => { const d = new Date(Math.round(t) * 1000); return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`; };
const hm = (t) => hms(t).slice(0, 5);
function mmss(s) {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  return h ? `${h}:${pad(m)}:${pad(x)}` : `${pad(m)}:${pad(x)}`;
}
const fmt = (x, d = 1) => (x === null || x === undefined || Number.isNaN(x) ? "–" : x.toFixed(d).replace("-", "−"));
const fmtInt = (x) => Math.round(x).toLocaleString("en-US");
function modcod(snr) {
  let best = ["none", 0];
  for (const [n, req, eff] of MODCODS) if (snr >= req + 1 && eff > best[1]) best = [n, eff];
  return best;
}
const capacity = (snr, bw) => (Number.isFinite(snr) ? (bw / 1.05) * modcod(snr)[1] : 0);
function marginColor(snr) {
  if (!Number.isFinite(snr)) return C.ink3;
  const m = snr - D.meta.closure_db;
  return m > 8 ? C.ok : m > 3 ? C.thin : C.fail;
}
function radarColor(r) {
  let c = null;
  for (const [th, col] of RADAR) if (r >= th) c = col;
  if (r < 1) return null;
  return c;
}
function hexA(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`;
}
function setupCanvas(cv) {
  const r = cv.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  if (cv.width !== Math.round(r.width * dpr) || cv.height !== Math.round(r.height * dpr)) {
    cv.width = Math.round(r.width * dpr); cv.height = Math.round(r.height * dpr);
  }
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.font = `12px ${css("--font")}`;
  return [ctx, r.width, r.height];
}

// ---------------------------------------------------------------- data access
const tickIndex = (t) => Math.max(0, Math.min(D.t.length - 1, Math.floor((t - tick0) / 15 + 1e-6)));
const sIdx = (i) => i + P;                      // series index (series include the pre-roll)
const antById = {}, stById = {}, calByName = {};
function alertsAt(i) {
  const A = D.alerts, f = A.fields;
  return A.ticks[i].map((row) => {
    const a = {};
    f.forEach((k, j) => { a[k] = j < f.length - 2 ? (row[j] < 0 ? null : A.strings[row[j]]) : row[j]; });
    return a;
  });
}
function satPos(id, t) {
  const tr = D.tracks.pos[id];
  if (!tr) return null;
  const x = (t - D.tracks.t0) / D.tracks.dt, k = Math.max(0, Math.min(tr.length - 2, Math.floor(x))), f = Math.min(1, Math.max(0, x - k));
  const a = tr[k], b = tr[k + 1];
  let dl = b[1] - a[1];
  if (dl > 180) dl -= 360; if (dl < -180) dl += 360;
  return [a[0] + (b[0] - a[0]) * f, a[1] + dl * f, a[2] + (b[2] - a[2]) * f];
}
const dispAlt = (km) => { const a = km / 6371; return a <= 1.6 ? a : 1.6 + 0.6 * Math.log10(a / 1.6); };
function xyz(lat, lon, alt) { const r = 1 + alt; return [r * Math.cos(lat * D2R) * Math.cos(lon * D2R), r * Math.cos(lat * D2R) * Math.sin(lon * D2R), r * Math.sin(lat * D2R)]; }
function geo([x, y, z]) { const r = Math.hypot(x, y, z); return [Math.asin(z / r) * R2D, Math.atan2(y, x) * R2D, r - 1]; }

// ---------------------------------------------------------------- boot
async function boot() {
  D = await (await fetch("data/demo.json")).json();
  tick0 = D.t[0]; tickN = D.t[D.t.length - 1]; P = D.meta.preroll || 0;
  D.antennas.forEach((a) => (antById[a.id] = a));
  D.stations.forEach((s) => (stById[s.id] = s));
  D.findings.calibration.forEach((c) => (calByName[c.station] = c));
  S.t = tick0;
  const sel = $("link-select");
  for (const st of D.stations) {
    const g = document.createElement("optgroup"); g.label = st.name;
    for (const a of st.antennas) { const o = document.createElement("option"); o.value = a; o.textContent = `${st.name} ${a.split("__")[1]}`; g.appendChild(o); }
    sel.appendChild(g);
  }
  sel.addEventListener("change", () => select(sel.value, true));
  $("play").addEventListener("click", () => setPlaying(!S.playing));
  $("speed").addEventListener("change", (e) => (S.speed = +e.target.value));
  $("closed-loop").addEventListener("change", (e) => { S.closedLoop = e.target.checked; renderPools(S.i); });
  document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));
  const tl = $("timeline");
  const seek = (e) => { const r = tl.getBoundingClientRect(); S.t = tick0 + ((e.clientX - r.left) / r.width) * (tickN - tick0); S.i = -1; };
  tl.addEventListener("pointerdown", (e) => { seek(e); tl.setPointerCapture(e.pointerId); });
  tl.addEventListener("pointermove", (e) => { if (e.buttons) seek(e); });
  window.addEventListener("keydown", (e) => {
    if (e.target.tagName === "SELECT" || e.target.tagName === "INPUT") return;
    if (e.code === "Space") { e.preventDefault(); setPlaying(!S.playing); }
    if (e.code === "ArrowRight") { S.t = Math.min(tickN, S.t + 60); S.i = -1; }
    if (e.code === "ArrowLeft") { S.t = Math.max(tick0, S.t - 60); S.i = -1; }
  });
  window.addEventListener("resize", () => { S.i = -1; if (G) G.width($("globe").clientWidth).height($("globe").clientHeight); });
  buildDocs();
  initGlobe();
  select("SITE-0da7faea0f45__ant0", false);
  $("loading").remove();
  window.fadewatch = { S, D, select, setPlaying, seekTo, show, caption, advance, tourTick: null, ready: false };
  if (QS.has("tour")) await import("./tour.js").then((m) => m.run(window.fadewatch));
  window.fadewatch.ready = true;
  if (!CAPTURE) requestAnimationFrame(frame);
}

function setPlaying(p) { S.playing = p; $("play").innerHTML = `<span aria-hidden="true">${p ? "❚❚" : "▶"}</span>`; $("play").setAttribute("aria-label", p ? "Pause replay" : "Play replay"); }
function seekTo(minutes) { S.t = tick0 + minutes * 60; S.i = -1; }
function show(view) {
  S.view = view;
  document.querySelectorAll(".tab").forEach((b) => (b.dataset.view === view ? b.setAttribute("aria-current", "page") : b.removeAttribute("aria-current")));
  for (const v of ["board", "backtest", "findings"]) $(v).hidden = v !== view;
  S.i = -1;
}
function caption(html) { const c = $("caption"); if (!html) { c.hidden = true; return; } c.innerHTML = html; c.hidden = false; }
function select(lid, manual) {
  S.selected = lid; $("link-select").value = lid;
  if (manual) S.manualUntil = Math.max(S.manualUntil, performance.now() + 30000);
  S.i = -1;
}

// Live: driven by requestAnimationFrame. Capture (?capture): the recorder calls advance(dt) per video frame.
let lastFrame = performance.now();
function frame(now) {
  const dt = Math.min(0.25, (now - lastFrame) / 1000); lastFrame = now;
  advance(dt, now);
  requestAnimationFrame(frame);
}
function advance(dt, now = performance.now()) {
  if (window.fadewatch?.tourTick) window.fadewatch.tourTick(dt);
  if (S.playing) { S.t += dt * S.speed; if (S.t >= tickN) { S.t = tickN; setPlaying(false); } }
  const i = tickIndex(S.t);
  if (i !== S.i) { S.i = i; renderTick(i); }
  if (now - S.lastSat > 100 && S.view === "board") { S.lastSat = now; updateSatellites(); }
  $("utc").textContent = hms(S.t);
  $("since").textContent = `T+${mmss(S.t - tick0)}`;
  drawTimeline();
}

function renderTick(i) {
  if (S.view !== "board") return;
  const alerts = alertsAt(i);
  follow(alerts);
  renderAlerts(alerts, i);
  updateLinks(i, alerts);
  renderStorm(i, alerts);
  renderLink(i);
  renderPools(i);
}

// Follow the most urgent new link alert unless the viewer picked a link recently.
function follow(alerts) {
  if (QS.has("tour")) return;  // the tour chooses what to show
  const now = performance.now();
  const fresh = alerts.filter((a) => !S.prevAlertIds.has(a.id) && a.link_id && a.severity !== "info");
  if (fresh.length && now > S.manualUntil && now > S.followAt && S.prevAlertIds.size) {
    fresh.sort((a, b) => SEV[a.severity] - SEV[b.severity]);
    S.selected = fresh[0].link_id; $("link-select").value = S.selected;
    S.followAt = now + 6000;
  }
}

// ---------------------------------------------------------------- timeline
function drawTimeline() {
  const [ctx, w, h] = setupCanvas($("timeline"));
  ctx.clearRect(0, 0, w, h);
  const x = (t) => ((t - tick0) / (tickN - tick0)) * w;
  ctx.fillStyle = C.rule; ctx.fillRect(0, h / 2 - 1, w, 2);
  ctx.fillStyle = hexA(C.ink3, 0.6);
  for (let m = 0; m <= (tickN - tick0) / 60; m += 5) {
    const xx = x(tick0 + m * 60);
    ctx.fillRect(xx, h / 2 + 4, 1, m % 15 ? 4 : 8);
    if (m % 15 === 0) { ctx.fillStyle = C.ink3; ctx.fillText(`+${m}m`, Math.min(xx + 3, w - 28), h - 3); ctx.fillStyle = hexA(C.ink3, 0.6); }
  }
  const col = { los: C.info, closure: C.fail, pool: C.thin };
  for (const e of D.scorecard.events) {
    ctx.fillStyle = col[e.type] || C.ink2;
    ctx.fillRect(x(e.t) - 1, h / 2 - 9, 2, 8);
  }
  ctx.fillStyle = hexA(C.ok, 0.35); ctx.fillRect(0, h / 2 - 1, x(S.t), 2);
  ctx.fillStyle = C.ink; ctx.beginPath(); ctx.arc(x(S.t), h / 2, 5, 0, 7); ctx.fill();
}

// ---------------------------------------------------------------- globe
function initGlobe() {
  const el = $("globe");
  G = Globe({ animateIn: false })(el)
    .width(el.clientWidth).height(el.clientHeight)
    .backgroundColor("rgba(0,0,0,0)")
    .showAtmosphere(true).atmosphereColor("#7f9cc9").atmosphereAltitude(0.13)
    .pathPoints("pts").pathPointLat((p) => p[0]).pathPointLng((p) => p[1]).pathPointAlt((p) => p[2])
    .pathColor((d) => d.color).pathStroke((d) => (d.sel ? 2.4 : 1.1)).pathTransitionDuration(0)
    .htmlElementsData([]).htmlLat("lat").htmlLng("lng").htmlAltitude("alt").htmlTransitionDuration(0)
    .htmlElement((d) => d.el);
  const mat = G.globeMaterial();
  mat.color.set("#22355a"); mat.emissive && mat.emissive.set("#101c33");
  G.pointOfView({ lat: 22, lng: -93, altitude: 1.62 }, 0);
  G.controls().enableZoom = true;
  G.controls().autoRotate = false;
  fetch("vendor/countries-110m.json").then((r) => r.json()).then((topo) => {
    // h3 fails to tile one 110m shape (408); Antarctica is clutter at this view.
    const skip = new Set(["010", "408"]);
    const feats = topojson.feature(topo, topo.objects.countries).features.filter((f) => !skip.has(String(f.id)));
    G.hexPolygonsData(feats).hexPolygonResolution(3).hexPolygonMargin(0.42).hexPolygonColor(() => "#4f6a93").hexPolygonAltitude(0.002);
  });
  // Station labels placed per site so the clustered US gateways stay legible.
  const place = { "Phoenix Gateway": ["l", 0], "Texas Gateway": ["r", -12], "CLFN-1": ["l", 8],
    "The Blitz Gateway": ["r", 8], "RANDOLPH RIDGE": ["r", -9], "O3B1.WMP": ["r", 7] };
  S.stationEls = D.stations.map((s) => {
    const el = document.createElement("div"); el.className = "st-dot";
    const [side, dy] = place[s.name] || ["r", 0];
    const lab = document.createElement("span"); lab.className = `st-label ${side}`; lab.textContent = s.name;
    lab.style.marginTop = `${dy}px`;
    el.appendChild(lab);
    return { id: s.id, el, lat: s.lat, lng: s.lon, alt: 0.004 };
  });
  S.satEls = D.satellites.map((s) => {
    const el = document.createElement("div"); el.className = "sat-dot";
    const lab = document.createElement("span"); lab.className = "sat-label"; el.appendChild(lab);
    el.title = s.name;
    return { id: s.id, name: s.name, el, lab, lat: 0, lng: 0, alt: 1 };
  });
}

function updateSatellites() {
  if (!G) return;
  const i = S.i, tracked = new Set();
  for (const a of D.antennas) { const tg = D.ant[a.id].tgt[sIdx(i)]; if (tg >= 0) tracked.add(tg); }
  for (const [k, s] of S.satEls.entries()) {
    const p = satPos(s.id, S.t);
    if (!p) continue;
    s.lat = p[0]; s.lng = p[1]; s.alt = dispAlt(p[2]);
    const on = tracked.has(k);
    s.el.classList.toggle("tracked", on);
    s.lab.textContent = on ? s.name : "";
  }
  G.htmlElementsData(S.stationEls.concat(S.satEls.filter((s) => Number.isFinite(s.lat))));
  updateLinks(S.i, null, true);
}

let lastAlertSev = {};
function updateLinks(i, alerts, geometryOnly) {
  if (!G) return;
  if (alerts) {
    lastAlertSev = {};
    for (const a of alerts) if (a.link_id && a.severity !== "info") {
      const cur = lastAlertSev[a.link_id];
      if (cur === undefined || SEV[a.severity] < cur) lastAlertSev[a.link_id] = SEV[a.severity];
    }
  }
  const paths = [];
  let live = 0;
  for (const a of D.antennas) {
    const s = D.ant[a.id], k = sIdx(i);
    if (s.mode[k] !== "t" || s.tgt[k] < 0) continue;
    live++;
    const sat = D.satellites[s.tgt[k]], p = satPos(sat.id, S.t);
    if (!p) continue;
    const st = stById[a.station];
    const A = xyz(st.lat, st.lon, 0.003), B = xyz(p[0], p[1], dispAlt(p[2]));
    const pts = [];
    for (let j = 0; j <= 20; j++) { const f = j / 20; pts.push(geo([A[0] + (B[0] - A[0]) * f, A[1] + (B[1] - A[1]) * f, A[2] + (B[2] - A[2]) * f])); }
    let color = marginColor(s.snr[k]);
    const sev = lastAlertSev[a.id];
    if (sev === 0) color = C.fail; else if (sev === 1 && color === C.ok) color = C.thin;
    paths.push({ pts, color, sel: a.id === S.selected });
  }
  G.pathsData(paths);
  if (!geometryOnly) {
    const crit = (alerts || []).filter((a) => a.severity === "critical").length;
    $("globe-stats").innerHTML = `<div><b>${live}</b><span>of ${D.antennas.length} antennas tracking</span></div>
      <div><b>${D.stations.length}</b><span>ground stations</span></div>
      <div><b style="color:${crit ? C.fail : C.ink}">${crit}</b><span>critical warnings</span></div>`;
  }
}

// ---------------------------------------------------------------- alerts
function renderAlerts(alerts, i) {
  const now = D.t[i];
  alerts.sort((a, b) => SEV[a.severity] - SEV[b.severity] || (a.event_t ?? 9e12) - (b.event_t ?? 9e12));
  const ol = $("alerts");
  ol.innerHTML = "";
  const ids = new Set();
  for (const a of alerts.slice(0, 14)) {
    ids.add(a.id);
    const li = document.createElement("li");
    li.className = `alert ${a.severity}` + (S.prevAlertIds.size && !S.prevAlertIds.has(a.id) ? " new" : "") +
      (a.link_id && a.link_id === S.selected ? " selected" : "");
    const ev = a.event_t;
    const count = ev === null || ev === undefined ? "ongoing" : ev <= now ? "now" : `T−${mmss(ev - now)}`;
    const when = ev ? `at ${hms(ev)}` : "";
    li.innerHTML = `<div class="count">${count}<small>${when}</small><small>seen since ${hm(a.raised_t)}</small></div>
      <div class="title">${a.title}</div><div class="detail">${a.detail}</div><div class="do">${a.action}</div>`;
    li.addEventListener("click", () => { if (a.link_id) select(a.link_id, true); });
    ol.appendChild(li);
  }
  if (!alerts.length) ol.innerHTML = `<li class="empty">Nothing predicted in the next 30 minutes.</li>`;
  S.prevAlertIds = new Set(alerts.map((a) => a.id));
}

// ---------------------------------------------------------------- storm map
function renderStorm(i) {
  const [ctx, w, h] = setupCanvas($("storm"));
  ctx.clearRect(0, 0, w, h);
  const st = stById[antById[S.selected]?.station] || stById["SITE-7576272aee77"];
  $("storm-title").textContent = `${st.name}: slant paths and rain`;
  const half = 30, sc = Math.min(w, h) / (2 * half), cx = w / 2, cy = h / 2;
  const kmLat = 111.32, kmLon = 111.32 * Math.cos(st.lat * D2R);
  const toPx = (lat, lon) => [cx + (lon - st.lon) * kmLon * sc, cy - (lat - st.lat) * kmLat * sc];

  // rain raster (truth)
  const nx = 120, ny = Math.max(20, Math.round(120 * h / w)), img = ctx.createImageData(nx, ny);
  let maxR = 0;
  const cells = D.rain.cells;
  const hw = w / sc / 2, hh = h / sc / 2;
  for (let y = 0; y < ny; y++) for (let x = 0; x < nx; x++) {
    const dx = (x / (nx - 1) - 0.5) * 2 * hw, dy = (0.5 - y / (ny - 1)) * 2 * hh;
    let r = 0;
    for (const c of cells) {
      const ex = (st.lon - c.lon[i]) * kmLon + dx, ey = (st.lat - c.lat[i]) * kmLat + dy;
      r += c.mmh[i] * Math.exp(-(ex * ex + ey * ey) / (c.radius_km * c.radius_km));
    }
    maxR = Math.max(maxR, r);
    const col = radarColor(r);
    if (!col) continue;
    const v = parseInt(col.slice(1), 16), o = (y * nx + x) * 4;
    img.data[o] = v >> 16; img.data[o + 1] = (v >> 8) & 255; img.data[o + 2] = v & 255;
    img.data[o + 3] = Math.round(70 + 110 * Math.min(1, r / 20));
  }
  const off = document.createElement("canvas"); off.width = nx; off.height = ny;
  off.getContext("2d").putImageData(img, 0, 0);
  ctx.imageSmoothingEnabled = true;
  ctx.drawImage(off, 0, 0, w, h);

  // range rings and scale
  ctx.strokeStyle = hexA(C.ink3, 0.35); ctx.lineWidth = 1;
  for (const r of [10, 20]) { ctx.beginPath(); ctx.arc(cx, cy, r * sc, 0, 7); ctx.stroke(); }
  ctx.fillStyle = C.ink2; ctx.fillText("10 km", cx + 10 * sc + 4, cy + 12); ctx.fillText("N", cx - 3, cy - 20 * sc - 6);

  // nowcast ghosts for convective cells
  ctx.setLineDash([4, 4]);
  for (const g of D.rain.nowcast[i]) {
    if (g[5] > 15 || g[4] < 1) continue;
    for (const [la, lo, lab] of [[g[0], g[1], "+10 min"], [g[2], g[3], "+20 min"]]) {
      const [x, y] = toPx(la, lo);
      ctx.strokeStyle = hexA(C.ink, 0.55); ctx.beginPath(); ctx.arc(x, y, g[5] * sc, 0, 7); ctx.stroke();
      ctx.fillStyle = C.ink2; ctx.fillText(lab, x + g[5] * sc * 0.72, y - g[5] * sc * 0.72);
    }
  }
  ctx.setLineDash([]);

  // slant paths of this station's antennas
  const hR = D.rain.freezing_km + 0.36;
  for (const aid of st.antennas) {
    const s = D.ant[aid], k = sIdx(i);
    if (s.mode[k] !== "t") continue;
    const L = hR / Math.tan(Math.max(s.el[k], 3) * D2R);
    const ex = cx + Math.sin(s.az[k] * D2R) * L * sc, ey = cy - Math.cos(s.az[k] * D2R) * L * sc;
    const f = D.fc[aid][i];
    let worst = s.snr[k];
    if (f) for (const v of f.p50) if (v !== null) worst = Math.min(worst, v);
    const sel = aid === S.selected;
    ctx.strokeStyle = marginColor(worst); ctx.lineWidth = sel ? 4 : 2.2; ctx.lineCap = "round";
    ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(ex, ey); ctx.stroke();
    const sat = D.satellites[s.tgt[k]]?.name || "";
    const rain = f && f.rain[0] > 0.3 ? `  rain −${f.rain[0].toFixed(1)} dB` : "";
    ctx.fillStyle = sel ? C.ink : C.ink2;
    const lx = ex + Math.sin(s.az[k] * D2R) * 8, ly = ey - Math.cos(s.az[k] * D2R) * 8;
    ctx.textAlign = Math.sin(s.az[k] * D2R) < 0 ? "right" : "left";
    ctx.fillText(`${aid.split("__")[1]} → ${sat}${rain}`, lx, ly + 4);
    ctx.textAlign = "left";
  }
  ctx.fillStyle = C.ink; ctx.beginPath(); ctx.arc(cx, cy, 4, 0, 7); ctx.fill();
  if (maxR < 0.5) { ctx.fillStyle = C.ink3; ctx.fillText("Clear sky over the station", 8, h - 8); }
  else { ctx.fillStyle = C.ink3; ctx.fillText(`Path length below the ${hR.toFixed(1)} km rain height`, 8, h - 8); }
}

// ---------------------------------------------------------------- link chart
function renderLink(i) {
  const lid = S.selected, a = antById[lid], st = stById[a.station], s = D.ant[lid], k = sIdx(i), f = D.fc[lid][i];
  const now = D.t[i];
  const sat = s.tgt[k] >= 0 ? D.satellites[s.tgt[k]].name : null;
  $("link-title").textContent = `${st.name} ${lid.split("__")[1]}` + (sat ? ` → ${sat}` : " (idle)") + `, ${a.f.toFixed(1)} GHz`;
  const [ctx, w, h] = setupCanvas($("snr"));
  ctx.clearRect(0, 0, w, h);
  const t0 = now - 600, t1 = now + 1800, L = 34, R = 18, T = 8, B = 20;
  const X = (t) => L + ((t - t0) / (t1 - t0)) * (w - L - R);
  // y range
  const vals = [D.meta.closure_db];
  for (let j = Math.max(0, k - 40); j <= Math.min(s.snr.length - 1, k + 120); j++) if (s.snr[j] !== null) vals.push(s.snr[j]);
  if (f) for (const arr of [f.p10, f.p90]) for (const v of arr) if (v !== null) vals.push(v);
  let lo = Math.min(...vals) - 2, hi = Math.max(...vals) + 2;
  lo = Math.max(lo, -30); hi = Math.min(Math.max(hi, lo + 8), 50);
  const Y = (v) => T + ((hi - v) / (hi - lo)) * (h - T - B);
  // grid
  ctx.strokeStyle = hexA(C.rule, 0.9); ctx.fillStyle = C.ink3; ctx.lineWidth = 1;
  const step = hi - lo > 30 ? 10 : 5;
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) {
    ctx.beginPath(); ctx.moveTo(L, Y(v)); ctx.lineTo(w - R, Y(v)); ctx.stroke();
    ctx.fillText(`${v}`, 4, Y(v) + 4);
  }
  for (let m = -10; m <= 30; m += 5) {
    ctx.fillText(m === 0 ? "now" : `${m > 0 ? "+" : "−"}${Math.abs(m)}m`, X(now + m * 60) - 10, h - 5);
  }
  // below-closure zone
  ctx.fillStyle = hexA(C.fail, 0.07); ctx.fillRect(L, Y(D.meta.closure_db), w - L - R, h - B - Y(D.meta.closure_db));
  ctx.strokeStyle = C.fail; ctx.setLineDash([5, 4]);
  ctx.beginPath(); ctx.moveTo(L, Y(D.meta.closure_db)); ctx.lineTo(w - R, Y(D.meta.closure_db)); ctx.stroke(); ctx.setLineDash([]);
  ctx.fillStyle = C.fail; ctx.fillText(`closure ${fmt(D.meta.closure_db, 2)} dB`, w - R - 98, Y(D.meta.closure_db) - 4);
  // what actually happened (future truth)
  ctx.strokeStyle = hexA(C.ink, 0.45); ctx.setLineDash([1.5, 3]); ctx.lineWidth = 1.5;
  polyline(ctx, s, k, Math.min(s.snr.length - 1, k + 120), X, Y, s.tgt[k]);
  ctx.setLineDash([]);
  // Fadewatch fan
  if (f) {
    const H = D.meta.H.map((m) => now + m * 60);
    ctx.fillStyle = hexA(C.fw, 0.22);
    let seg = [];
    const flush = () => {
      if (seg.length > 1) {
        ctx.beginPath(); seg.forEach(([x, a]) => ctx.lineTo(X(x), Y(a[0]))); seg.slice().reverse().forEach(([x, a]) => ctx.lineTo(X(x), Y(a[1]))); ctx.closePath(); ctx.fill();
        ctx.strokeStyle = C.fw; ctx.lineWidth = 2; ctx.beginPath(); seg.forEach(([x, a]) => ctx.lineTo(X(x), Y(a[2]))); ctx.stroke();
      }
      seg = [];
    };
    const last = s.snr[k];
    if (last !== null) seg.push([now, [last, last, last]]);
    H.forEach((x, j) => { if (f.p50[j] === null) flush(); else seg.push([x, [f.p10[j], f.p90[j], f.p50[j]]]); });
    flush();
    if (f.los && f.los < t1) {
      ctx.strokeStyle = C.info; ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.moveTo(X(f.los), T); ctx.lineTo(X(f.los), h - B); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = C.info; ctx.fillText(`sets below 10° at ${hms(f.los)}`, Math.min(X(f.los) + 5, w - 170), T + 12);
    }
  }
  // persistence baseline: last value within 4 min, 1-5 min ahead
  let lastJ = -1;
  for (let j = k; j >= Math.max(0, k - 16); j--) if (s.snr[j] !== null && s.mode[j] === "t") { lastJ = j; break; }
  if (lastJ >= 0) {
    const v = s.snr[lastJ], sig = (m) => 1.2816 * 0.26038 * Math.sqrt(m);
    ctx.fillStyle = hexA(C.pb, 0.14);
    ctx.beginPath(); ctx.moveTo(X(now), Y(v));
    for (const m of [1, 2, 3, 4, 5]) ctx.lineTo(X(now + m * 60), Y(v + sig(m)));
    for (const m of [5, 4, 3, 2, 1]) ctx.lineTo(X(now + m * 60), Y(v - sig(m)));
    ctx.closePath(); ctx.fill();
    ctx.strokeStyle = C.pb; ctx.setLineDash([6, 4]); ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(X(now), Y(v)); ctx.lineTo(X(now + 300), Y(v)); ctx.stroke(); ctx.setLineDash([]);
  }
  // observed
  ctx.strokeStyle = C.ink; ctx.lineWidth = 2;
  polyline(ctx, s, Math.max(0, k - 40), k, X, Y, null);
  // now line
  ctx.strokeStyle = hexA(C.ink, 0.5); ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(X(now), T); ctx.lineTo(X(now), h - B); ctx.stroke();
  renderBudget(i);
}
function polyline(ctx, s, j0, j1, X, Y, onlyTarget) {
  let pen = false;
  ctx.beginPath();
  for (let j = j0; j <= j1; j++) {
    const v = s.snr[j], ok = v !== null && s.mode[j] === "t" && (onlyTarget === null || s.tgt[j] === onlyTarget);
    const t = tick0 + (j - P) * 15;
    if (!ok) { pen = false; continue; }
    if (!pen) { ctx.moveTo(X(t), Y(v)); pen = true; } else ctx.lineTo(X(t), Y(v));
  }
  ctx.stroke();
}

function renderBudget(i) {
  const lid = S.selected, a = antById[lid], st = stById[a.station], s = D.ant[lid], k = sIdx(i), f = D.fc[lid][i];
  const box = $("budget");
  if (s.mode[k] !== "t") { box.innerHTML = `<p class="bmeta">This antenna is ${s.mode[k] === "s" ? "slewing to a new satellite" : "idle, available as a spare"}.</p>`; return; }
  const cal = calByName[st.name] || { loss_db: 0, zenith_gas_db: 0.2 };
  const el = s.el[k], rng = s.rng[k], sn = Math.sin(el * D2R);
  const fspl = 92.45 + 20 * Math.log10(a.f) + 20 * Math.log10(rng);
  const gas = cal.zenith_gas_db * 2 / (Math.sqrt(sn * sn + 2.35e-4) + sn);
  const rain = f ? f.rain[0] || 0 : 0;
  const bw = 10 * Math.log10(a.bw * 1e6);
  const exp = a.eirp + a.gt - fspl - gas - rain - cal.loss_db + 228.6 - bw;
  const rows = [["Satellite EIRP", a.eirp], ["Ground G/T", a.gt], ["Free-space loss", -fspl], ["Gaseous absorption", -gas, gas > 0.5],
    ["Rain on the path (nowcast)", -rain, rain > 0.3], ["Station losses (fitted)", -cal.loss_db], ["Boltzmann constant", 228.6], [`Bandwidth, ${a.bw} MHz`, -bw]];
  const mc = modcod(s.snr[k]);
  box.innerHTML = `<table>${rows.map(([n, v, hot]) => `<tr class="${hot ? "var" : ""}"><td>${n}</td><td class="v">${fmt(v, 1)}</td></tr>`).join("")}
    <tr class="sum"><td>Expected SNR</td><td class="v">${fmt(exp, 2)}</td></tr>
    <tr><td>Observed SNR</td><td class="v">${fmt(s.snr[k], 2)}</td></tr></table>
    <div class="bmeta">Elevation <b>${fmt(el, 1)}°</b>, range <b>${fmtInt(rng)} km</b><br>
    ${mc[0] === "none" ? "<b>No MODCOD closes</b>" : `<b>${mc[0]}</b>, ${fmtInt(capacity(s.snr[k], a.bw))} Mbps`}<br>
    ${f ? `Residual bias <b>${fmt(f.bias, 2)} dB</b>, trend <b>${fmt(f.trend, 2)} dB/min</b>` : ""}</div>`;
}

// ---------------------------------------------------------------- pools
function renderPools(i) {
  if (i < 0) return;
  const box = $("pools");
  if (!box.children.length) {
    for (const p of D.pools) {
      const d = document.createElement("div"); d.className = "pool";
      const names = p.stations.map((s) => stById[s].name).join(", ");
      d.innerHTML = `<div class="pid">${p.id}</div><canvas></canvas><div class="pst">${names}</div><div class="pnow"></div>`;
      box.appendChild(d);
    }
  }
  const now = D.t[i];
  D.pools.forEach((p, n) => {
    const el = box.children[n], cv = el.querySelector("canvas");
    const [ctx, w, h] = setupCanvas(cv);
    ctx.clearRect(0, 0, w, h);
    const t0 = now - 900, t1 = now + 1800, X = (t) => ((t - t0) / (t1 - t0)) * w;
    const top = p.p99 * 2.4, Y = (v) => h - 2 - (Math.min(v, top) / top) * (h - 6);
    ctx.strokeStyle = hexA(C.ink3, 0.7); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, Y(p.p50)); ctx.lineTo(w, Y(p.p50)); ctx.stroke();
    ctx.strokeStyle = C.thin; ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(0, Y(p.p99)); ctx.lineTo(w, Y(p.p99)); ctx.stroke(); ctx.setLineDash([]);
    const line = (arr, color, dash) => {
      ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.setLineDash(dash || []);
      ctx.beginPath();
      for (let j = Math.max(0, i - 60); j <= i; j++) ctx.lineTo(X(D.t[j]), Y(arr[j]));
      ctx.stroke(); ctx.setLineDash([]);
    };
    line(p.cap_true, C.ink);
    if (S.closedLoop && D.closed_loop) line(D.closed_loop.cap[p.id], C.fw);
    const fc = p.cap_fc[i];
    ctx.strokeStyle = C.fw; ctx.setLineDash([4, 3]); ctx.lineWidth = 1.5; ctx.beginPath(); ctx.moveTo(X(now), Y(p.cap_true[i]));
    D.meta.H.forEach((m, j) => ctx.lineTo(X(now + m * 60), Y(fc[j])));
    ctx.stroke(); ctx.setLineDash([]);
    ctx.strokeStyle = hexA(C.ink, 0.4); ctx.beginPath(); ctx.moveTo(X(now), 0); ctx.lineTo(X(now), h); ctx.stroke();
    const cap = S.closedLoop && D.closed_loop ? D.closed_loop.cap[p.id][i] : p.cap_true[i];
    let short = null;
    D.meta.H.forEach((m, j) => { if (short === null && fc[j] < p.p99) short = m; });
    const status = cap < p.p50 ? `<span class="bad">short now</span>` : short !== null ? `<span class="bad">short in T−${mmss(short * 60)}</span>` : "";
    el.querySelector(".pnow").innerHTML = `<b>${fmtInt(cap)}</b> Mbps for ${fmtInt(p.p50)} offered ${status}`;
  });
}

// ---------------------------------------------------------------- documents
function buildDocs() {
  const sc = D.scorecard, s = sc.summary, sk = sc.skill;
  const lead = (x) => (x === null || x === undefined ? "missed" : `${(x / 60).toFixed(1)} min`);
  const ol = sc.open_loop, cl = sc.closed_loop;
  const hl = (k, label) => s[k] ? `<div><b>${s[k].fadewatch_warned_60s} of ${s[k].events}</b><span>${label} warned at least a minute ahead; median ${s[k].fadewatch_median_lead_min.toFixed(1)} min</span>
    <em>Persistence: ${s[k].persistence_warned_60s} of ${s[k].events}</em></div>` : "";
  const reg = ["all", "steady", "low elevation", "rain", "equipment", "interference"];
  const skillRows = reg.filter((r) => sk.fadewatch[r]).map((r) => {
    const fw = sk.fadewatch[r], pb = sk.persistence[r] || {};
    return `<tr><td>${r === "all" ? "All samples" : r[0].toUpperCase() + r.slice(1)}</td>${["1min", "3min", "5min"].map((h) => {
      const a = fw[h]?.mae_db, b = pb[h]?.mae_db;
      return `<td class="n ${a < b ? "win" : ""}">${fmt(a, 2)}</td><td class="n lose">${fmt(b, 2)}</td>`;
    }).join("")}<td class="n">${fmt((fw["5min"]?.coverage_80 ?? NaN) * 100, 0)}%</td><td class="n lose">${fmt((pb["5min"]?.coverage_80 ?? NaN) * 100, 0)}%</td></tr>`;
  }).join("");
  const poolRows = Object.keys(ol.pools).map((p) => `<tr><td>${p}</td><td class="n">${fmt(ol.pools[p].unserved_gbit, 1)}</td><td class="n ${cl && cl.pools[p].unserved_gbit < ol.pools[p].unserved_gbit - 0.1 ? "win" : ""}">${cl ? fmt(cl.pools[p].unserved_gbit, 1) : "–"}</td>
    <td class="n">${fmt(ol.pools[p].minutes_below_p50, 1)}</td><td class="n">${cl ? fmt(cl.pools[p].minutes_below_p50, 1) : "–"}</td></tr>`).join("");
  const evRows = sc.events.map((e) => `<tr><td>${e.iso.slice(11, 19)}</td><td>${{ los: "Loss of signal", closure: "Fade below closure", pool: "Pool short of demand" }[e.type]}</td>
    <td>${e.link_id ? `${stById[antById[e.link_id].station].name} ${e.link_id.split("__")[1]}` : e.pool_id}</td>
    <td class="n ${e.fadewatch_lead_s >= 60 ? "win" : ""}">${lead(e.fadewatch_lead_s)}</td><td class="n lose">${lead(e.persistence_lead_s)}</td></tr>`).join("");
  $("backtest").innerHTML = `<div class="col">
    <h1>Fadewatch against the persistence baseline</h1>
    <p>Constellation's served SNR model, <b>snr-baseline-v1</b>, is a persistence forecast: it carries the last observed SNR forward, so by design it cannot see a fade the link has not started showing. This backtest replays the demo fleet for 75 minutes from its snapshot with a Ka-band storm over Texas Gateway, a failing LNA at Phoenix Gateway and an interferer at O3B1.WMP, and scores Fadewatch against a replica of that baseline on the same telemetry.</p>
    <div class="headline">${hl("los", "satellites setting below the mask")}${hl("closure", "fades below closure")}${hl("pool", "pool capacity shortfalls")}</div>
    <p>Alert precision: <b>${sc.alerts.confirmed} of ${sc.alerts.confirmed + sc.alerts.false}</b> warnings were confirmed by what happened next (${sc.alerts.unverifiable} predicted events past the end of the replay could not be checked).</p>
    <h3>Forecast error at Constellation's horizons</h3>
    <p>Mean absolute error in dB at 1, 3 and 5 minutes, Fadewatch first, persistence second, and how often the truth landed inside each model's 80% interval at 5 minutes.</p>
    <table><thead><tr><th>Regime</th><th class="n" colspan="2">1 min</th><th class="n" colspan="2">3 min</th><th class="n" colspan="2">5 min</th><th class="n" colspan="2">In 80% band, 5 min</th></tr>
      <tr><th></th>${'<th class="n">Fadewatch</th><th class="n">Persistence</th>'.repeat(4)}</tr></thead><tbody>${skillRows}</tbody></table>
    <p>Interference is the one regime neither model can forecast: it is a step with no precursor. Fadewatch flags it within a minute and stops extrapolating its trend while it lasts.</p>
    <h3>Closed loop: the same replay with Fadewatch's plans executed</h3>
    <p>Make-before-break handovers on spare antennas (${cl ? cl.links.make_before_break : 0} of ${ol.links.handovers}), and a spare at Texas Gateway brought up on the satellite whose slant path the nowcast kept clearest of the storm. Handover gaps fell from <b>${fmt(ol.links.handover_gap_s / 60, 1)}</b> to <b>${cl ? fmt(cl.links.handover_gap_s / 60, 1) : "–"}</b> antenna-minutes.</p>
    <table><thead><tr><th>Pool</th><th class="n">Unserved, Gbit</th><th class="n">With plans</th><th class="n">Minutes below demand</th><th class="n">With plans</th></tr></thead><tbody>${poolRows}</tbody></table>
    <p>ATLAGAX1 is unserved before the replay starts: its only closing link, BIOMASS, had already set, and PROBA-3 near apogee does not close. Phoenix Gateway has one antenna, so its handover gap and its failing LNA cannot be planned away; Fadewatch can only warn early, and does.</p>
    <h3>Every event in the replay</h3>
    <table><thead><tr><th>Time</th><th>Event</th><th>Where</th><th class="n">Fadewatch warned</th><th class="n">Persistence warned</th></tr></thead><tbody>${evRows}</tbody></table>
    <p>Lead times count from the first tick of an unbroken warning to the event, and are capped by the replay start (23:07:00) and the 30-minute forecast horizon. The persistence baseline is credited whenever its 10th percentile at any horizon falls below closure.</p>
    <h3>What this is and is not</h3>
    <p>The orbits, ground stations, RF configuration and starting state are the real demo fleet. The truth is simulated: rain from a moving convective cell integrated along each slant path, ITU-R scintillation and estimator noise, and injected equipment faults. Fadewatch never sees that truth, only telemetry and a radar-style nowcast with motion and intensity errors that grow with lead time. Run the same engine against your recorded telemetry with <b>fadewatch live</b>.</p>
  </div>`;
  const F = D.findings;
  const alRows = F.alignment.map((a) => `<tr><td>${a.link_id.replace("__", " ")}</td><td>${a.sat}</td><td class="n">${a.offset_days === 0 ? "0" : fmt(a.offset_days, 2) + " d"}</td><td class="n">${fmt(a.az_err, 2)}°</td><td class="n">${fmt(a.el_err, 2)}°</td><td class="n">${fmt(a.rng_err_km, 1)} km</td></tr>`).join("");
  const calRows = F.calibration.map((c) => `<tr><td>${c.station}</td><td class="n">${fmt(c.loss_db, 2)} dB</td><td class="n">${fmt(c.zenith_gas_db, 3)} dB</td><td class="n">${c.rms_db === null || c.n < 2 ? "single link" : fmt(c.rms_db, 4) + " dB"}</td><td class="n">${c.n}</td></tr>`).join("");
  $("findings").innerHTML = `<div class="col">
    <h1>The demo fleet, checked against physics</h1>
    <p>Before forecasting anything, Fadewatch checks that the telemetry agrees with orbital mechanics and a link budget. On the demo fleet it does, closely, with one exception worth knowing about.</p>
    <h3>Pointing agrees with SGP4, once the replay offset is found</h3>
    <p>Propagating CelesTrak elements for each target, the 16 O3b links match their reported azimuth and elevation to about a tenth of a degree at the stamped time. The Globalstar and PROBA-3 links do not match at the stamped time at all: they match at <b>exactly −25.5 days</b>. The LEO/HEO part of the demo fleet is a recording from 2026-09-15 re-stamped to the present. A console that propagates elements at the playhead would draw those links to satellites below the horizon. Fadewatch finds the offset automatically and works in the recording's own geometry. BIOMASS, on a 3-day repeat orbit, aligns at the equivalent +1.5 days.</p>
    <table><thead><tr><th>Link</th><th>Satellite</th><th class="n">Offset</th><th class="n">Azimuth error</th><th class="n">Elevation error</th><th class="n">Range error</th></tr></thead><tbody>${alRows}</tbody></table>
    <h3>SNR agrees with a two-parameter link budget per station</h3>
    <p>Free-space loss from the reported range and frequency, EIRP and G/T from the telemetry, a fitted fixed loss per station and a fitted zenith gaseous attenuation on a cosecant law reproduce every observed SNR in the snapshot. The fitted zenith attenuation rises with frequency the way the atmosphere does: about 0.04 dB at C band, 0.17 dB at 18.5 GHz and 0.22 dB at 19 GHz, nearer the 22 GHz water-vapour line.</p>
    <table><thead><tr><th>Station</th><th class="n">Fixed loss</th><th class="n">Zenith gas</th><th class="n">Fit residual (RMS)</th><th class="n">Links</th></tr></thead><tbody>${calRows}</tbody></table>
    <p>The non-closing PROBA-3 link (−12.5 dB at 48,800 km) also falls exactly on the budget: it is a geometry problem, not a fault, and Fadewatch says so.</p>
  </div>`;
}

boot().catch((e) => { $("loading").textContent = `Could not load the replay: ${e.message}. Run "fadewatch site" to build data/demo.json.`; console.error(e); });
