// Access to the compiled replay bundle (data/demo.json, built by `fadewatch site`).
import { STATION_CODE } from "./util.js";

export class Replay {
  constructor(D) {
    this.D = D;
    this.t0 = D.t[0];
    this.tN = D.t[D.t.length - 1];
    this.P = D.meta.preroll || 0;
    this.closure = D.meta.closure_db;
    this.ant = Object.fromEntries(D.antennas.map((a) => [a.id, a]));
    this.st = Object.fromEntries(D.stations.map((s) => [s.id, s]));
    this.satIndex = Object.fromEntries(D.satellites.map((s, i) => [s.id, i]));
    this.cal = Object.fromEntries(D.findings.calibration.map((c) => [c.station, c]));
    this._alertCache = new Map();
  }
  tick(t) { return Math.max(0, Math.min(this.D.t.length - 1, Math.floor((t - this.t0) / 15 + 1e-6))); }
  k(i) { return i + this.P; }              // series index for tick i (series include the pre-roll)
  timeOfK(k) { return this.t0 + (k - this.P) * 15; }

  alerts(i) {
    if (this._alertCache.has(i)) return this._alertCache.get(i);
    const A = this.D.alerts, f = A.fields, n = f.length - 2;
    const out = A.ticks[i].map((row) => {
      const a = {};
      f.forEach((key, j) => { a[key] = j < n ? (row[j] < 0 ? null : A.strings[row[j]]) : row[j]; });
      return a;
    });
    if (this._alertCache.size > 400) this._alertCache.clear();
    this._alertCache.set(i, out);
    return out;
  }

  satPos(satId, t) {
    const tr = this.D.tracks.pos[satId];
    if (!tr) return null;
    const x = (t - this.D.tracks.t0) / this.D.tracks.dt;
    const k = Math.max(0, Math.min(tr.length - 2, Math.floor(x))), f = Math.min(1, Math.max(0, x - k));
    const a = tr[k], b = tr[k + 1];
    let dl = b[1] - a[1];
    if (dl > 180) dl -= 360; if (dl < -180) dl += 360;
    return [a[0] + (b[0] - a[0]) * f, a[1] + dl * f, a[2] + (b[2] - a[2]) * f];
  }

  // Link state at a fractional time (interpolated between 15 s samples).
  link(id, t) {
    const s = this.D.ant[id], x = (t - this.t0) / 15 + this.P;
    const k = Math.max(0, Math.min(s.snr.length - 1, Math.floor(x)));
    const live = s.mode[k] === "t" && s.tgt[k] >= 0;
    return { k, live, mode: s.mode[k], tgt: s.tgt[k], snr: s.snr[k], el: s.el[k], az: s.az[k], rng: s.rng[k], rain: s.rain[k] };
  }

  code(linkId) {
    const a = this.ant[linkId], st = this.st[a.station];
    return `${STATION_CODE[st.name] || st.name.slice(0, 3)}·A${linkId.split("__ant")[1]}`;
  }
  stationCode(stId) { return STATION_CODE[this.st[stId].name]; }
}
