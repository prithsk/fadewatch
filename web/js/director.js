// Camera director: shots are poses (lat, lon, target radius, range, heading, tilt);
// moves between them are eased, and long hops arc outward like a satellite pass.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { D2R, clamp, easeInOutCubic, lerp, toXYZ } from "./util.js";

const v3 = (a) => new THREE.Vector3(a[0], a[1], a[2]);

export function poseToCamera(p) {
  // Target point T at radius p.r above (lat, lon); camera sits `range` away from T,
  // tilted `tilt` deg from the local vertical, looking along `heading`.
  const up = v3(toXYZ(p.lat, p.lon, 1)).normalize();
  const north = v3(toXYZ(p.lat + 0.01, p.lon, 1)).normalize().sub(up).normalize();
  const east = new THREE.Vector3().crossVectors(north, up).normalize();
  const h = p.heading * D2R, tl = p.tilt * D2R;
  const fwd = north.clone().multiplyScalar(Math.cos(h)).add(east.clone().multiplyScalar(Math.sin(h)));
  const T = up.clone().multiplyScalar(p.r);
  const dir = up.clone().multiplyScalar(Math.cos(tl)).sub(fwd.clone().multiplyScalar(Math.sin(tl)));
  const pos = T.clone().add(dir.multiplyScalar(p.range));
  const camUp = fwd.clone().multiplyScalar(Math.cos(tl)).add(up.clone().multiplyScalar(Math.sin(tl))).normalize();
  return { pos, target: T, up: camUp };
}

function angDist(a, b) {
  const pa = v3(toXYZ(a.lat, a.lon)), pb = v3(toXYZ(b.lat, b.lon));
  return Math.acos(clamp(pa.dot(pb), -1, 1));
}
const wrap = (d) => ((d + 540) % 360) - 180;

export const SHOTS = {
  wide: { lat: 14, lon: -96, r: 0, range: 4.6, heading: 0, tilt: 0 },
  americas: { lat: 24, lon: -95, r: 0, range: 3.4, heading: 0, tilt: 0 },
  meo: { lat: 4, lon: -100, r: 0, range: 5.6, heading: 0, tilt: 0 },
};

export class Director {
  constructor(camera, dom) {
    this.camera = camera;
    this.pose = { ...SHOTS.wide };
    this.from = null; this.to = null; this.t = 0; this.dur = 1;
    this.drift = 0.9;                 // deg/s of heading drift on close shots, deg/s of longitude on wide ones
    this.controls = new OrbitControls(camera, dom);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.08;
    this.controls.enablePan = false;
    this.controls.minDistance = 1.02;
    this.controls.maxDistance = 14;
    this.controls.enabled = false;
    this.user = false;
    this.controls.addEventListener("start", () => { this.user = true; this.to = null; });
    this.apply();
  }

  // Fly to a pose over `dur` seconds of director time.
  fly(pose, dur = 2.6) {
    this.user = false;
    this.from = { ...this.pose };
    this.to = { ...pose };
    this.to.lon = this.from.lon + wrap(this.to.lon - this.from.lon);
    this.to.heading = this.from.heading + wrap(this.to.heading - this.from.heading);
    this.hop = angDist(this.from, this.to);
    this.t = 0; this.dur = dur;
  }
  cut(pose) { this.user = false; this.to = null; this.pose = { ...pose }; this.apply(); }

  // A close oblique shot on a station, looking toward a link's azimuth so the beam climbs into frame.
  stationShot(st, az = 180, close = 1) {
    return { lat: st.lat, lon: st.lon, r: 1, range: 0.085 * close, heading: az, tilt: 58 };
  }
  regionShot(lat, lon, range = 0.32, heading = 0, tilt = 38) { return { lat, lon, r: 1, range, heading, tilt }; }

  update(dt) {
    if (this.user) { this.controls.enabled = true; this.controls.update(); return; }
    this.controls.enabled = false;
    if (this.to) {
      this.t += dt;
      const u = easeInOutCubic(clamp(this.t / this.dur));
      const a = this.from, b = this.to, p = {};
      for (const k of ["lat", "lon", "heading", "tilt"]) p[k] = lerp(a[k], b[k], u);
      // the look-at point reaches the surface early when diving in, late when pulling out
      const ur = b.r > a.r ? 1 - Math.pow(1 - u, 3) : Math.pow(u, 3);
      p.r = lerp(a.r, b.r, ur);
      // range: log-interpolate, plus an outward arc for long hops
      const arc = Math.sin(Math.PI * u) * clamp(this.hop / 0.5) * 1.6;
      p.range = Math.exp(lerp(Math.log(a.range), Math.log(b.range), u)) + arc * Math.max(a.range, b.range, 0.6);
      // when the target point moves between the Earth's centre and its surface, keep the camera outside
      this.pose = p;
      if (this.t >= this.dur) { this.pose = { ...b }; this.to = null; }
    } else {
      // living micro-motion
      if (this.pose.r > 0.5) this.pose.heading += this.drift * 0.35 * dt;
      else this.pose.lon += this.drift * 0.6 * dt;
    }
    this.apply();
  }

  apply() {
    let c = poseToCamera(this.pose);
    for (let n = 0; n < 12 && c.pos.length() < 1.012; n++) {   // never inside the Earth
      this.pose.range *= 1.25;
      c = poseToCamera(this.pose);
    }
    this.camera.position.copy(c.pos);
    this.camera.up.copy(c.up);
    this.camera.lookAt(c.target);
    this.controls.target.copy(c.target);
    const d = c.pos.length();
    this.camera.near = Math.max(0.0004, (d - 1) * 0.15);
    this.camera.far = 400;
    this.camera.updateProjectionMatrix();
  }
}
