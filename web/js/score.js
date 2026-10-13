// Scorecard overlay: big numbers that count up, bars that grow. Driven by `age` (seconds
// since opened) so it renders identically live and in frame-by-frame capture.
import { PALETTE, clamp, easeOutCubic, easeOutExpo } from "./util.js";

const $ = (id) => document.getElementById(id);
const TYPE = { los: "LOS", closure: "FADE", pool: "POOL" };

export class Score {
  constructor(replay) {
    this.R = replay;
    this.open = false; this.age = 0;
    const sc = replay.D.scorecard, s = sc.summary;
    const hero = [["los", "satellites setting"], ["closure", "fades below closure"], ["pool", "pools short of demand"]];
    const sk = sc.skill, regs = ["steady", "low elevation", "rain", "equipment", "interference"];
    const ol = sc.open_loop.pools.DLLSTXX1.unserved_gbit, cl = sc.closed_loop ? sc.closed_loop.pools.DLLSTXX1.unserved_gbit : ol;
    const F = replay.D.findings;
    const o3b = F.alignment.filter((a) => a.offset_days === 0);
    const pointErr = Math.max(...o3b.map((a) => Math.max(a.az_err, a.el_err)));
    const fit = Math.max(...F.calibration.filter((c) => c.n >= 2).map((c) => c.rms_db));
    const events = sc.events.slice().sort((a, b) => a.t - b.t);
    const maxLead = 32;
    $("score").innerHTML = `
      <div class="sc-wrap">
        <div class="sc-head"><span class="sc-k">Backtest</span><span class="sc-v">Fadewatch <i style="background:${PALETTE.fw}"></i> vs persistence <i style="background:${PALETTE.pb}"></i></span>
          <button class="sc-close" aria-label="Close">✕</button></div>
        <div class="sc-hero">${hero.map(([k, lab], n) => `
          <div class="hero" style="--d:${n * 0.12}s"><b data-count="${s[k].fadewatch_warned_60s}" data-of="${s[k].events}">0</b>
            <span>${lab} warned ahead</span><em>median ${s[k].fadewatch_median_lead_min.toFixed(0)} min · persistence ${s[k].persistence_warned_60s}/${s[k].events}</em></div>`).join("")}
          <div class="hero small" style="--d:0.36s"><b data-count="${sc.alerts.confirmed}" data-of="${sc.alerts.confirmed + sc.alerts.false}">0</b><span>warnings confirmed</span><em>no false alarms</em></div>
        </div>
        <div class="sc-grid">
          <section><h3>Lead time per event</h3><div class="leads">${events.map((e, n) => {
            const code = e.link_id ? replay.code(e.link_id) : e.pool_id;
            const fw = (e.fadewatch_lead_s || 0) / 60, pb = (e.persistence_lead_s || 0) / 60;
            return `<div class="lead" style="--d:${0.5 + n * 0.035}s"><span class="lt">${TYPE[e.type]}</span><span class="lc">${code}</span>
              <span class="lb"><i class="fw" style="--w:${clamp(fw / maxLead) * 100}%"></i><i class="pb" style="--w:${clamp(pb / maxLead) * 100}%"></i></span>
              <span class="lv">${fw.toFixed(0)}′</span></div>`;
          }).join("")}</div></section>
          <section><h3>Error at 5 min, dB</h3><div class="mae">${regs.map((r, n) => {
            const a = sk.fadewatch[r]["5min"].mae_db, b = sk.persistence[r]["5min"].mae_db, top = 9;
            return `<div class="mrow" style="--d:${0.7 + n * 0.08}s"><span class="mn">${r}</span>
              <span class="mb"><i class="fw" style="--w:${clamp(a / top) * 100}%"></i><i class="pb" style="--w:${clamp(b / top) * 100}%"></i></span>
              <span class="mv">${a.toFixed(1)} <s>${b.toFixed(1)}</s></span></div>`;
          }).join("")}</div>
            <h3>Plans executed: DLLSTXX1 unserved</h3>
            <div class="cl" style="--d:1.2s"><span class="mb big"><i class="pb" style="--w:100%"></i></span><span class="mv">${ol.toFixed(1)} Gbit</span>
              <span class="mb big"><i class="fw" style="--w:${(cl / ol) * 100}%"></i></span><span class="mv">${cl.toFixed(1)} Gbit <b>−${Math.round((1 - cl / ol) * 100)}%</b></span></div>
          </section>
          <section><h3>The demo fleet, checked</h3>
            <div class="facts">
              <div class="fact" style="--d:0.9s"><b>${pointErr.toFixed(1)}°</b><span>O3b pointing vs SGP4</span></div>
              <div class="fact" style="--d:1.0s"><b>−25.5 d</b><span>LEO links are a replay</span></div>
              <div class="fact" style="--d:1.1s"><b>${fit.toFixed(3)} dB</b><span>link-budget fit, RMS</span></div>
            </div></section>
        </div>
      </div>`;
    $("score").querySelector(".sc-close").addEventListener("click", () => this.toggle(false));
  }
  toggle(on = !this.open) {
    this.open = on; this.age = 0;
    $("score").classList.toggle("on", on);
  }
  update(dt) {
    if (!this.open) return;
    this.age += dt;
    const el = $("score");
    el.style.setProperty("--age", this.age.toFixed(3));
    el.querySelectorAll("[data-count]").forEach((b) => {
      const d = parseFloat(getComputedStyle(b.parentElement).getPropertyValue("--d")) || 0;
      const u = easeOutExpo(clamp((this.age - d - 0.15) / 1.1));
      b.textContent = `${Math.round(u * +b.dataset.count)}/${b.dataset.of}`;
    });
    el.querySelectorAll("[style*='--d']").forEach((x) => {
      const d = parseFloat(x.style.getPropertyValue("--d")) || 0;
      const u = easeOutCubic(clamp((this.age - d) / 0.6));
      x.style.opacity = u; x.style.transform = `translateY(${(1 - u) * 10}px)`;
      x.style.setProperty("--g", u.toFixed(3));
    });
  }
}
