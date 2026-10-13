// Motion and formatting helpers. Everything here is a pure function of its inputs,
// so a frame can be rendered for any time t (needed for frame-by-frame capture).

export const D2R = Math.PI / 180, R2D = 180 / Math.PI;
export const clamp = (x, a = 0, b = 1) => Math.min(b, Math.max(a, x));
export const lerp = (a, b, t) => a + (b - a) * t;
export const easeInOutCubic = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
export const easeOutCubic = (t) => 1 - Math.pow(1 - t, 3);
export const easeOutBack = (t, s = 1.4) => 1 + (s + 1) * Math.pow(t - 1, 3) + s * Math.pow(t - 1, 2);
export const easeOutExpo = (t) => (t >= 1 ? 1 : 1 - Math.pow(2, -10 * t));

// Seeded hash noise (deterministic).
export function hash(n) {
  const s = Math.sin(n * 127.1 + 311.7) * 43758.5453123;
  return s - Math.floor(s);
}

const pad2 = (n) => String(Math.floor(n)).padStart(2, "0");
export function hms(t) {
  const d = new Date(Math.round(t) * 1000);
  return `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}:${pad2(d.getUTCSeconds())}`;
}
export function mmss(s) {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  return h ? `${h}:${pad2(m)}:${pad2(x)}` : `${pad2(m)}:${pad2(x)}`;
}
export const fmt = (x, d = 1) => (x === null || x === undefined || Number.isNaN(x) ? "–" : x.toFixed(d).replace("-", "−"));
export const fmtInt = (x) => Math.round(x).toLocaleString("en-US");

// Rolling digits: for `frames` after a value changes, show scrambled digits, then settle.
// `age` is how many render frames ago the value changed.
export function rolled(text, age, frames = 5, seed = 1) {
  if (age >= frames) return text;
  return text.replace(/\d/g, (_, i) => String(Math.floor(hash(seed * 31 + i * 7 + age * 13) * 10)));
}

// Geodesy in the scene frame: R = 1, y up, lon 0 on +x, east toward -z
// (matches three.js SphereGeometry texture mapping).
export function toXYZ(lat, lon, r = 1) {
  const la = lat * D2R, lo = lon * D2R;
  return [r * Math.cos(la) * Math.cos(lo), r * Math.sin(la), -r * Math.cos(la) * Math.sin(lo)];
}
export function fromXYZ(x, y, z) {
  const r = Math.hypot(x, y, z);
  return [Math.asin(y / r) * R2D, Math.atan2(-z, x) * R2D, r];
}

// Sub-solar point for a UTC time (accurate to ~0.5 deg, plenty for lighting).
export function subsolar(t) {
  const d = new Date(t * 1000);
  const start = Date.UTC(d.getUTCFullYear(), 0, 0);
  const n = (t * 1000 - start) / 86400000;
  const g = (2 * Math.PI / 365) * (n - 1);
  const decl = 0.006918 - 0.399912 * Math.cos(g) + 0.070257 * Math.sin(g) - 0.006758 * Math.cos(2 * g)
    + 0.000907 * Math.sin(2 * g) - 0.002697 * Math.cos(3 * g) + 0.00148 * Math.sin(3 * g);
  const eot = 229.18 * (0.000075 + 0.001868 * Math.cos(g) - 0.032077 * Math.sin(g) - 0.014615 * Math.cos(2 * g)
    - 0.040849 * Math.sin(2 * g));
  const utcMin = d.getUTCHours() * 60 + d.getUTCMinutes() + d.getUTCSeconds() / 60;
  const lon = -(utcMin + eot - 720) / 4;
  return [decl * R2D, ((lon + 540) % 360) - 180];
}

export const STATION_CODE = { "Phoenix Gateway": "PHX", "RANDOLPH RIDGE": "RDG", "O3B1.WMP": "WMP",
  "Texas Gateway": "TXG", "The Blitz Gateway": "BLZ", "CLFN-1": "CLF" };

export const MODCODS = [["QPSK 1/4", -2.35, 0.49], ["QPSK 1/3", -1.24, 0.656], ["QPSK 1/2", 1.0, 0.989],
  ["QPSK 2/3", 3.1, 1.319], ["QPSK 3/4", 4.03, 1.484], ["QPSK 5/6", 5.18, 1.65], ["8PSK 2/3", 6.62, 1.98],
  ["8PSK 3/4", 7.91, 2.228], ["8PSK 5/6", 9.35, 2.479], ["16APSK 3/4", 10.21, 2.967], ["16APSK 5/6", 11.61, 3.3],
  ["16APSK 9/10", 12.89, 3.567], ["32APSK 3/4", 12.73, 3.703], ["32APSK 5/6", 14.28, 4.124], ["32APSK 9/10", 16.05, 4.453]];
export function modcod(snr) {
  let best = ["—", 0];
  for (const [n, req, eff] of MODCODS) if (snr >= req + 1 && eff > best[1]) best = [n, eff];
  return best;
}
export const capacity = (snr, bw) => (Number.isFinite(snr) ? (bw / 1.05) * modcod(snr)[1] : 0);

export const PALETTE = {
  ok: "#7fe7f2", thin: "#ffb547", fail: "#ff4d5e", info: "#9db8ff", fw: "#7fe7f2", pb: "#c3a6ff",
  ink: "#e9f4ff", ink2: "#9fb6cc", ink3: "#5f7a93",
};
export function marginColor(snr, closure) {
  if (!Number.isFinite(snr)) return PALETTE.ink3;
  const m = snr - closure;
  return m > 8 ? PALETTE.ok : m > 3 ? PALETTE.thin : PALETTE.fail;
}
