// The HUD: contacts, target card, lock brackets, pools, timeline, labels.
// Text is kept to codes, numbers and countdowns; sentences live in the README.
import { PALETTE, capacity, clamp, easeOutBack, easeOutCubic, fmt, fmtInt, hms, marginColor, mmss, modcod, rolled } from "./util.js";

const SEV = { critical: 0, warning: 1, info: 2 };
const SEV_COLOR = { critical: PALETTE.fail, warning: PALETTE.thin, info: PALETTE.info };
const $ = (id) => document.getElementById(id);

function setupCanvas(cv) {
  const r = cv.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  const W = Math.max(1, Math.round(r.width * dpr)), H = Math.max(1, Math.round(r.height * dpr));
  if (cv.width !== W || cv.height !== H) { cv.width = W; cv.height = H; }
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return [ctx, r.width, r.height];
}
const hexA = (hex, a) => { const n = parseInt(hex.slice(1), 16); return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`; };

export class Hud {
  constructor(replay, globe, onSelect) {
    this.R = replay; this.G = globe; this.onSelect = onSelect;
    this.frame = 0;
    this.lockT = -1;           // HUD time of the last lock-on (for the bracket snap)
    this.rows = new Map();
    this.prevIds = new Set();
    this.snrText = ""; this.snrAge = 99;
    this.buildPools();
    this.labels = new Map();
    for (const s of replay.D.stations) {
      const el = document.createElement("div"); el.className = "st-label"; el.textContent = replay.stationCode(s.id);
      $("labels").appendChild(el); this.labels.set(s.id, el);
    }
    this.satLabel = document.createElement("div"); this.satLabel.className = "sat-label"; $("labels").appendChild(this.satLabel);
  }

  // ------------------------------------------------------------ contacts
  contacts(alerts, now, selected, hudT) {
    const list = alerts.slice().sort((a, b) => SEV[a.severity] - SEV[b.severity] || (a.event_t ?? 9e12) - (b.event_t ?? 9e12))
      .filter((a) => a.severity !== "info" || (a.event_t && a.event_t - now < 900)).slice(0, 8);
    const ol = $("contacts");
    const ids = list.map((a) => a.id);
    // remove stale rows
    for (const [id, el] of this.rows) if (!ids.includes(id)) { el.remove(); this.rows.delete(id); }
    list.forEach((a, n) => {
      let el = this.rows.get(a.id);
      if (!el) {
        el = document.createElement("li");
        el.innerHTML = `<button class="contact"><i class="bar"></i><span class="cd"></span><span class="code"></span><span class="tag"></span><span class="act"></span><i class="prog"></i></button>`;
        el.firstChild.addEventListener("click", () => this.onSelect(a.link_id ? { link: a.link_id } : { pool: a.pool_id, station: a.station_id }, true));
        el._born = hudT + (this.rows.size ? 0 : n * 0.05);
        this.rows.set(a.id, el);
      }
      el.dataset.event = a.event_t ?? "";
      el.className = `row ${a.severity}` + ((a.link_id && a.link_id === selected?.link) || (a.pool_id && a.pool_id === selected?.pool) ? " sel" : "")
        ;
      el.querySelector(".code").textContent = a.code;
      el.querySelector(".tag").textContent = a.tag;
      el.querySelector(".act").textContent = a.act;
      ol.appendChild(el);   // keeps sort order
    });
    this.list = list;
  }
  tickCountdowns(now, hudT) {
    for (const el of this.rows.values()) {
      const u = easeOutCubic(clamp((hudT - el._born) / 0.4));
      el.style.opacity = u; el.style.transform = `translateX(${(1 - u) * -16}px)`;
      const ev = el.dataset.event === "" ? null : +el.dataset.event;
      el.querySelector(".cd").textContent = ev === null ? "LIVE" : ev <= now ? "NOW" : `T−${mmss(ev - now)}`;
      const left = ev === null ? 0 : clamp((ev - now) / 1800);
      el.querySelector(".prog").style.transform = `scaleX(${1 - left})`;
    }
  }

  // ------------------------------------------------------------ target card
  card(sel, alerts, t, i) {
    const box = $("target");
    if (!sel) { box.classList.remove("on"); return; }
    box.classList.add("on");
    const R = this.R;
    if (sel.pool) return this.poolCard(sel.pool, alerts, t, i);
    const L = R.link(sel.link, t), a = R.ant[sel.link];
    $("t-l1").textContent = "margin"; $("t-l2").textContent = "elev"; $("t-l3").textContent = "modcod"; $("t-unit").textContent = "dB";
    const al = alerts.filter((x) => x.link_id === sel.link).sort((x, y) => SEV[x.severity] - SEV[y.severity])[0];
    const sat = L.tgt >= 0 ? R.D.satellites[L.tgt].name : "—";
    $("t-code").textContent = R.code(sel.link);
    $("t-sat").textContent = L.live ? sat : L.mode === "s" ? "slewing" : "idle";
    const tag = $("t-tag");
    tag.textContent = al ? al.tag : L.live ? "NOMINAL" : "";
    tag.style.color = al ? SEV_COLOR[al.severity] : PALETTE.ok;
    const snrStr = L.live && L.snr !== null ? fmt(L.snr, 1) : "—";
    if (sel.link !== this.snrFor) { this.snrFor = sel.link; this.snrAge = 0; }   // roll on lock-on only
    $("t-snr").textContent = rolled(snrStr, this.snrAge++, 5, i);
    $("t-snr").style.color = L.live ? marginColor(L.snr, R.closure) : PALETTE.ink3;
    const m = L.live ? L.snr - R.closure : NaN;
    $("t-margin").textContent = L.live ? `${m >= 0 ? "+" : ""}${fmt(m, 1)}` : "—";
    $("t-el").textContent = L.live ? `${fmt(L.el, 1)}°` : "—";
    const mc = L.live ? modcod(L.snr) : ["—", 0];
    $("t-mod").textContent = mc[0];
    $("t-cap").textContent = L.live ? `${fmtInt(capacity(L.snr, a.bw))} Mbps` : "";
    if (al && al.event_t) {
      const ahead = al.event_t - al.raised_t;
      $("t-lead-l").textContent = al.event_t > t ? "event in" : ahead > 0 ? "warned" : "detected in";
      $("t-lead").textContent = al.event_t > t ? mmss(al.event_t - t) : ahead > 0 ? `${mmss(ahead)} ahead` : mmss(-ahead);
    } else { $("t-lead-l").textContent = ""; $("t-lead").textContent = ""; }
    $("t-act").textContent = al ? al.act : "";
    this.chart(sel.link, t, i);
  }

  poolCard(pid, alerts, t, i) {
    const R = this.R, p = R.D.pools.find((x) => x.id === pid);
    const al = alerts.find((x) => x.pool_id === pid);
    $("t-code").textContent = pid;
    $("t-l1").textContent = "offered"; $("t-l2").textContent = "p99"; $("t-l3").textContent = ""; $("t-unit").textContent = "Mbps";
    $("t-sat").textContent = p.stations.map((s) => R.stationCode(s)).join(" + ");
    $("t-tag").textContent = al ? al.tag : "POOL"; $("t-tag").style.color = al ? SEV_COLOR[al.severity] : PALETTE.ok;
    const cap = p.cap_true[i];
    $("t-snr").textContent = fmtInt(cap); $("t-snr").style.color = cap < p.p50 ? PALETTE.fail : cap < p.p99 ? PALETTE.thin : PALETTE.ok;
    $("t-margin").textContent = fmtInt(p.p50); $("t-el").textContent = fmtInt(p.p99);
    $("t-mod").textContent = "Mbps"; $("t-cap").textContent = "";
    if (al && al.event_t && al.event_t > t) { $("t-lead-l").textContent = "short in"; $("t-lead").textContent = mmss(al.event_t - t); }
    else { $("t-lead-l").textContent = al ? "short" : ""; $("t-lead").textContent = al ? "now" : ""; }
    $("t-act").textContent = al ? al.act : "";
    const [ctx, w, h] = setupCanvas($("t-chart"));
    ctx.clearRect(0, 0, w, h);
    const t0 = t - 900, t1 = t + 1800, X = (x) => ((x - t0) / (t1 - t0)) * w;
    const top = p.p99 * 2.2, Y = (v) => h - 14 - (Math.min(v, top) / top) * (h - 22);
    ctx.strokeStyle = hexA(PALETTE.thin, 0.8); ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.moveTo(0, Y(p.p99)); ctx.lineTo(w, Y(p.p99)); ctx.stroke();
    ctx.strokeStyle = hexA(PALETTE.ink2, 0.6); ctx.beginPath(); ctx.moveTo(0, Y(p.p50)); ctx.lineTo(w, Y(p.p50)); ctx.stroke(); ctx.setLineDash([]);
    ctx.strokeStyle = PALETTE.ink; ctx.lineWidth = 2; ctx.beginPath();
    for (let j = Math.max(0, i - 60); j <= i; j++) ctx.lineTo(X(R.D.t[j]), Y(p.cap_true[j]));
    ctx.stroke();
    ctx.strokeStyle = PALETTE.fw; ctx.setLineDash([4, 3]); ctx.beginPath(); ctx.moveTo(X(t), Y(cap));
    R.D.meta.H.forEach((m, j) => ctx.lineTo(X(t + m * 60), Y(p.cap_fc[i][j])));
    ctx.stroke(); ctx.setLineDash([]);
    this.axes(ctx, w, h, t, X);
  }

  axes(ctx, w, h, t, X) {
    ctx.fillStyle = PALETTE.ink3; ctx.font = "10px 'IBM Plex Mono', monospace";
    for (const m of [-10, 0, 10, 20, 30]) {
      const x = X(t + m * 60);
      if (x < 0 || x > w) continue;
      ctx.fillText(m === 0 ? "now" : `${m > 0 ? "+" : "−"}${Math.abs(m)}m`, Math.min(x - 8, w - 26), h - 2);
    }
    ctx.strokeStyle = hexA(PALETTE.ink, 0.35); ctx.beginPath(); ctx.moveTo(X(t), 0); ctx.lineTo(X(t), h - 12); ctx.stroke();
  }

  chart(lid, t, i) {
    const R = this.R, s = R.D.ant[lid], f = R.D.fc[lid][i], k = R.k(i);
    const [ctx, w, h] = setupCanvas($("t-chart"));
    ctx.clearRect(0, 0, w, h);
    const t0 = t - 600, t1 = t + 1800, X = (x) => ((x - t0) / (t1 - t0)) * w;
    const vals = [R.closure];
    for (let j = Math.max(0, k - 40); j <= Math.min(s.snr.length - 1, k + 120); j++) if (s.snr[j] !== null) vals.push(s.snr[j]);
    if (f) for (const arr of [f.p10, f.p90]) for (const v of arr) if (v !== null) vals.push(v);
    let lo = Math.min(...vals) - 2, hi = Math.max(...vals) + 2;
    lo = Math.max(lo, -30); hi = Math.min(Math.max(hi, lo + 8), 50);
    const Y = (v) => 4 + ((hi - v) / (hi - lo)) * (h - 18);
    // closure floor
    ctx.fillStyle = hexA(PALETTE.fail, 0.08); ctx.fillRect(0, Y(R.closure), w, h - 14 - Y(R.closure));
    ctx.strokeStyle = hexA(PALETTE.fail, 0.8); ctx.setLineDash([4, 3]); ctx.beginPath(); ctx.moveTo(0, Y(R.closure)); ctx.lineTo(w, Y(R.closure)); ctx.stroke(); ctx.setLineDash([]);
    const poly = (j0, j1, only) => {
      let pen = false; ctx.beginPath();
      for (let j = j0; j <= j1; j++) {
        const v = s.snr[j], ok = v !== null && s.mode[j] === "t" && (only === null || s.tgt[j] === only);
        if (!ok) { pen = false; continue; }
        const x = X(R.timeOfK(j));
        if (!pen) { ctx.moveTo(x, Y(v)); pen = true; } else ctx.lineTo(x, Y(v));
      }
      ctx.stroke();
    };
    // what actually happened
    ctx.strokeStyle = hexA(PALETTE.ink, 0.4); ctx.setLineDash([1.5, 3]); ctx.lineWidth = 1.5;
    poly(k, Math.min(s.snr.length - 1, k + 120), s.tgt[k]); ctx.setLineDash([]);
    // Fadewatch fan
    if (f) {
      const H = R.D.meta.H.map((m) => t + m * 60);
      let seg = [];
      const flush = () => {
        if (seg.length > 1) {
          ctx.fillStyle = hexA(PALETTE.fw, 0.2);
          ctx.beginPath(); seg.forEach(([x, a]) => ctx.lineTo(X(x), Y(a[0]))); seg.slice().reverse().forEach(([x, a]) => ctx.lineTo(X(x), Y(a[1]))); ctx.closePath(); ctx.fill();
          ctx.strokeStyle = PALETTE.fw; ctx.lineWidth = 2; ctx.beginPath(); seg.forEach(([x, a]) => ctx.lineTo(X(x), Y(a[2]))); ctx.stroke();
        }
        seg = [];
      };
      if (s.snr[k] !== null && s.mode[k] === "t") seg.push([t, [s.snr[k], s.snr[k], s.snr[k]]]);
      H.forEach((x, j) => { if (f.p50[j] === null) flush(); else seg.push([x, [f.p10[j], f.p90[j], f.p50[j]]]); });
      flush();
      if (f.los && f.los < t1) {
        ctx.strokeStyle = PALETTE.info; ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.moveTo(X(f.los), 0); ctx.lineTo(X(f.los), h - 14); ctx.stroke(); ctx.setLineDash([]);
        ctx.fillStyle = PALETTE.info; ctx.font = "10px 'IBM Plex Mono', monospace"; ctx.fillText("LOS", X(f.los) + 4, 12);
      }
    }
    // persistence
    let lastJ = -1;
    for (let j = k; j >= Math.max(0, k - 16); j--) if (s.snr[j] !== null && s.mode[j] === "t") { lastJ = j; break; }
    if (lastJ >= 0) {
      const v = s.snr[lastJ];
      ctx.strokeStyle = PALETTE.pb; ctx.setLineDash([6, 4]); ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(X(t), Y(v)); ctx.lineTo(X(t + 300), Y(v)); ctx.stroke(); ctx.setLineDash([]);
    }
    ctx.strokeStyle = PALETTE.ink; ctx.lineWidth = 2;
    poly(Math.max(0, k - 40), k, null);
    this.axes(ctx, w, h, t, X);
  }

  // ------------------------------------------------------------ brackets, labels
  overlay(sel, t, hudT) {
    const G = this.G, R = this.R;
    const br = $("brackets"), lead = $("leader");
    const ov = $("overlay"); if (G.size) ov.setAttribute("viewBox", `0 0 ${G.size[0]} ${G.size[1]}`);
    let target = null, satTarget = null;
    if (sel?.link) {
      target = G.project(G.stationPoint(R.ant[sel.link].station));
      const L = R.link(sel.link, t);
      if (L.live) { const sp = G.satPoint(L.tgt, t); if (sp) satTarget = G.project(sp); }
    } else if (sel?.station) target = G.project(G.stationPoint(sel.station));
    if (target && target.front) {
      const age = hudT - this.lockT, u = clamp(age / 0.42);
      const s = 1.45 - 0.45 * easeOutBack(u, 1.2);
      br.style.opacity = u < 1 ? (0.4 + 0.6 * Math.round((age * 24) % 1)) : 1;
      br.style.transform = `translate(${target.x - 28}px, ${target.y - 28}px) scale(${s})`;
      br.style.display = "block";
      const card = $("target").getBoundingClientRect();
      lead.style.display = card.width ? "block" : "none";
      if (card.width) {
        const x2 = card.left - 6, y2 = card.top + 26;
        lead.setAttribute("d", `M${target.x + 30},${target.y - 30} L${target.x + 64},${target.y - 64} L${x2},${y2}`);
      }
    } else { br.style.display = "none"; lead.style.display = "none"; }
    const sb = $("satbracket");
    if (satTarget && satTarget.front) {
      sb.style.display = "block"; sb.style.transform = `translate(${satTarget.x - 9}px, ${satTarget.y - 9}px) rotate(45deg)`;
      this.satLabel.style.display = "block";
      this.satLabel.style.transform = `translate(${satTarget.x + 14}px, ${satTarget.y - 22}px)`;
      this.satLabel.textContent = R.D.satellites[R.link(sel.link, t).tgt].name;
    } else { sb.style.display = "none"; this.satLabel.style.display = "none"; }
    for (const [id, el] of this.labels) {
      const p = G.project(G.stationPoint(id));
      if (!p.front) { el.style.display = "none"; continue; }
      el.style.display = "block";
      el.style.transform = `translate(${p.x + 10}px, ${p.y - 8}px)`;
      el.classList.toggle("sel", sel?.station === id || (sel?.link && R.ant[sel.link].station === id));
    }
  }

  // ------------------------------------------------------------ pools, timeline, clock
  buildPools() {
    const box = $("pools");
    box.innerHTML = "";
    this.poolEls = this.R.D.pools.map((p) => {
      const el = document.createElement("button"); el.className = "pool";
      el.innerHTML = `<svg viewBox="0 0 64 64"><circle class="trk" cx="32" cy="32" r="26"/><circle class="val" cx="32" cy="32" r="26"/><line class="p99"/></svg>
        <span class="pid">${p.id}</span><span class="pv"></span>`;
      el.addEventListener("click", () => this.onSelect({ pool: p.id, station: p.stations[0] }, true));
      box.appendChild(el);
      return el;
    });
  }
  pools(i, t, closedLoop) {
    const R = this.R, C = 2 * Math.PI * 26;
    R.D.pools.forEach((p, n) => {
      const el = this.poolEls[n];
      const cap = closedLoop && R.D.closed_loop ? R.D.closed_loop.cap[p.id][i] : p.cap_true[i];
      const top = p.p99 * 2.5, frac = clamp(cap / top);
      const val = el.querySelector(".val");
      val.style.strokeDasharray = `${frac * C * 0.75} ${C}`;
      const color = cap < p.p50 ? PALETTE.fail : cap < p.p99 ? PALETTE.thin : PALETTE.ok;
      val.style.stroke = color;
      const a = (135 + 270 * (p.p99 / top)) * Math.PI / 180;
      const l = el.querySelector(".p99");
      l.setAttribute("x1", 32 + 21 * Math.cos(a)); l.setAttribute("y1", 32 + 21 * Math.sin(a));
      l.setAttribute("x2", 32 + 31 * Math.cos(a)); l.setAttribute("y2", 32 + 31 * Math.sin(a));
      let short = null;
      R.D.meta.H.forEach((m, j) => { if (short === null && p.cap_fc[i][j] < p.p99) short = m; });
      el.querySelector(".pv").innerHTML = cap < p.p50 ? `<b style="color:${PALETTE.fail}">SHORT</b>`
        : short !== null ? `<b style="color:${PALETTE.thin}">T−${mmss(short * 60)}</b>` : `${fmtInt(cap)}`;
      el.classList.toggle("bad", cap < p.p50);
    });
  }

  timeline(t) {
    const R = this.R;
    const [ctx, w, h] = setupCanvas($("timeline"));
    ctx.clearRect(0, 0, w, h);
    const X = (x) => ((x - R.t0) / (R.tN - R.t0)) * w, mid = h / 2 + 3;
    ctx.fillStyle = hexA(PALETTE.ink3, 0.5); ctx.fillRect(0, mid, w, 1);
    ctx.fillStyle = hexA(PALETTE.ok, 0.7); ctx.fillRect(0, mid, X(t), 1);
    const col = { los: PALETTE.info, closure: PALETTE.fail, pool: PALETTE.thin };
    for (const e of R.D.scorecard.events) {
      ctx.fillStyle = col[e.type]; const x = X(e.t);
      ctx.fillRect(x - 1, mid - (e.type === "closure" ? 9 : 6), 2, e.type === "closure" ? 9 : 6);
    }
    ctx.fillStyle = PALETTE.ink; ctx.beginPath(); ctx.arc(X(t), mid, 4, 0, 7); ctx.fill();
    ctx.font = "10px 'IBM Plex Mono', monospace"; ctx.fillStyle = PALETTE.ink3;
    for (let m = 0; m <= 75; m += 15) ctx.fillText(`+${m}`, Math.min(X(R.t0 + m * 60) + 2, w - 18), h - 1);
  }

  clock(t) {
    $("utc").textContent = hms(t);
    $("tplus").textContent = `T+${mmss(t - this.R.t0)}`;
  }
}
