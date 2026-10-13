import { Replay } from "./data.js";
import { Globe } from "./globe.js";
import { Director, SHOTS } from "./director.js";
import { Hud } from "./hud.js";
import { Score } from "./score.js";
import { clamp, easeOutCubic } from "./util.js";

const QS = new URLSearchParams(location.search);
const CAPTURE = QS.has("capture");
const $ = (id) => document.getElementById(id);
const SEVN = { critical: 0, warning: 1, info: 2 };

const S = { t: 0, speed: 40, playing: true, sel: null, closedLoop: false, hudT: 0, i: -1, alerts: [], alertSev: {} };
let R, G, Dir, H, SC;

async function boot() {
  const D = await (await fetch("data/demo.json")).json();
  R = new Replay(D);
  if (CAPTURE) document.body.classList.add("capture");
  S.t = R.t0;
  G = new Globe($("scene"), R, { capture: CAPTURE });
  await G.load((url) => { $("boot-msg").textContent = url.split("/").pop(); });
  Dir = new Director(G.camera, $("scene"));
  H = new Hud(R, G, select);
  SC = new Score(R);
  resize();
  window.addEventListener("resize", resize);
  wireControls();
  $("boot").classList.add("gone");
  window.fadewatch = { S, R, G, Dir, H, SC, select, setMode, setPlaying, seek, caption, advance, cue, cues: [], ready: false };
  if (QS.has("tour")) await import("./tour.js").then((m) => m.run(window.fadewatch));
  else Dir.cut(SHOTS.wide);
  window.fadewatch.ready = true;
  if (!CAPTURE) requestAnimationFrame(frame);
}

function resize() {
  const w = window.innerWidth, h = window.innerHeight;
  G.resize(w, h);
}

function wireControls() {
  $("play").addEventListener("click", () => setPlaying(!S.playing));
  $("speed").addEventListener("click", () => {
    const opts = [10, 40, 60, 120]; S.speed = opts[(opts.indexOf(S.speed) + 1) % opts.length];
    $("speed").textContent = `${S.speed}×`;
  });
  document.querySelectorAll("[data-mode]").forEach((b) => b.addEventListener("click", () => setMode(+b.dataset.mode)));
  $("scorebtn").addEventListener("click", () => SC.toggle());
  $("cl").addEventListener("click", () => { S.closedLoop = !S.closedLoop; $("cl").setAttribute("aria-pressed", S.closedLoop); });
  $("home").addEventListener("click", () => { S.sel = null; Dir.fly(SHOTS.wide, 2.4); });
  const tl = $("timeline");
  const seekX = (e) => { const r = tl.getBoundingClientRect(); seek(((e.clientX - r.left) / r.width) * (R.tN - R.t0) / 60); };
  tl.addEventListener("pointerdown", (e) => { seekX(e); tl.setPointerCapture(e.pointerId); });
  tl.addEventListener("pointermove", (e) => { if (e.buttons) seekX(e); });
  window.addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT") return;
    if (e.code === "Space") { e.preventDefault(); setPlaying(!S.playing); }
    if (e.key >= "1" && e.key <= "3") setMode(+e.key - 1);
    if (e.key === "s") SC.toggle();
    if (e.key === "Escape") { if (SC.open) SC.toggle(false); else { S.sel = null; Dir.fly(SHOTS.wide, 2.4); } }
    if (e.code === "ArrowRight") seek((S.t - R.t0) / 60 + 1);
    if (e.code === "ArrowLeft") seek((S.t - R.t0) / 60 - 1);
  });
  $("scene").addEventListener("click", (e) => {
    // pick the nearest station marker on screen
    let best = null, bd = 22;
    for (const s of R.D.stations) {
      const p = G.project(G.stationPoint(s.id));
      const d = Math.hypot(p.x - e.clientX, p.y - e.clientY);
      if (p.front && d < bd) { bd = d; best = s; }
    }
    if (!best) return;
    const live = best.antennas.find((a) => R.link(a, S.t).live);
    select(live ? { link: live } : { station: best.id }, true);
  });
}

function setPlaying(p) { S.playing = p; $("play").dataset.state = p ? "playing" : "paused"; }
function setMode(m) {
  G.setMode(m);
  document.querySelectorAll("[data-mode]").forEach((b) => b.setAttribute("aria-pressed", +b.dataset.mode === m));
}
function seek(minutes) { S.t = clamp(R.t0 + minutes * 60, R.t0, R.tN); S.i = -1; }

function select(sel, fly = true, dur = 2.8) {
  S.sel = sel;
  H.lockT = S.hudT;
  cue("lock");
  if (!fly || !sel) return;
  if (sel.link) {
    const a = R.ant[sel.link], st = R.st[a.station], L = R.link(sel.link, S.t);
    const az = L.live ? L.az : 180;
    Dir.fly(Dir.stationShot(st, az, sel.close || 1), dur);
  } else {
    const st = R.st[sel.station || R.D.pools.find((p) => p.id === sel.pool).stations[0]];
    Dir.fly(Dir.regionShot(st.lat - 2, st.lon, 0.55, 0, 30), dur);
  }
}

// Kinetic caption: characters rise in with a stagger, driven by HUD time.
let cap = { el: null, t0: 0, out: null };
function caption(text, sub = "") {
  const box = $("kicker");
  if (cap.el) { cap.out = { el: cap.el, t0: S.hudT }; }
  if (!text) { cap.el = null; return; }
  const el = document.createElement("div"); el.className = "kick";
  const mk = (s, cls) => `<div class="${cls}">${[...s].map((c) => `<span>${c === " " ? "&nbsp;" : c}</span>`).join("")}</div>`;
  el.innerHTML = mk(text, "k1") + (sub ? mk(sub, "k2") : "");
  box.appendChild(el);
  cap = { el, t0: S.hudT, out: cap.out };
  cue("caption");
}
function animateCaption() {
  if (cap.el) cap.el.querySelectorAll("span").forEach((s, n) => {
    const u = easeOutCubic(clamp((S.hudT - cap.t0 - n * 0.016) / 0.35));
    s.style.opacity = u; s.style.transform = `translateY(${(1 - u) * 14}px)`;
  });
  if (cap.out) {
    const u = clamp((S.hudT - cap.out.t0) / 0.25);
    cap.out.el.style.opacity = 1 - u; cap.out.el.style.transform = `translateY(${-u * 10}px)`;
    if (u >= 1) { cap.out.el.remove(); cap.out = null; }
  }
}

// Audio cues for the soundtrack: recorded against tour time, synthesised after capture.
function cue(type, extra = {}) { window.fadewatch?.cues.push({ t: S.hudT, type, ...extra }); }

let last = performance.now();
function frame(now) {
  advance(Math.min(0.1, (now - last) / 1000));
  last = now;
  requestAnimationFrame(frame);
}

function advance(dt) {
  S.hudT += dt;
  window.fadewatch.tourTick?.(dt);
  if (S.playing) { S.t = Math.min(R.tN, S.t + dt * S.speed); if (S.t >= R.tN) setPlaying(false); }
  const i = R.tick(S.t);
  if (i !== S.i) {
    const prev = new Set(S.alerts.map((a) => a.id));
    S.i = i; S.alerts = R.alerts(i);
    S.alertSev = {};
    for (const a of S.alerts) if (a.link_id && a.severity !== "info") {
      const v = SEVN[a.severity]; if (S.alertSev[a.link_id] === undefined || v < S.alertSev[a.link_id]) S.alertSev[a.link_id] = v;
    }
    for (const a of S.alerts) if (!prev.has(a.id) && prev.size && a.severity !== "info") cue("alert", { sev: a.severity });
    H.contacts(S.alerts, S.t, S.sel, S.hudT);
    H.pools(i, S.t, S.closedLoop);
  }
  Dir.update(dt);
  const focus = S.sel?.link ? R.ant[S.sel.link].station : S.sel?.station;
  G.update(S.t, dt, { selected: S.sel?.link, alertSev: S.alertSev, focusStation: focus });
  H.tickCountdowns(S.t, S.hudT);
  H.card(S.sel, S.alerts, S.t, i);
  H.overlay(S.sel, S.t, S.hudT);
  H.timeline(S.t);
  H.clock(S.t);
  SC.update(dt);
  animateCaption();
  G.render();
}

boot().catch((e) => { $("boot-msg").textContent = `Could not start: ${e.message}`; console.error(e); });
