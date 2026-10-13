// The 3D scene: a sun-lit Earth in the Earth-fixed frame, true-scale orbits, link beams,
// the storm, and a post stack (bloom + sensor looks). R = 1 scene unit = 6371 km.
import * as THREE from "three";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/addons/postprocessing/UnrealBloomPass.js";
import { ShaderPass } from "three/addons/postprocessing/ShaderPass.js";
import { OutputPass } from "three/addons/postprocessing/OutputPass.js";
import { Line2 } from "three/addons/lines/Line2.js";
import { LineGeometry } from "three/addons/lines/LineGeometry.js";
import { LineMaterial } from "three/addons/lines/LineMaterial.js";
import { D2R, PALETTE, hash, marginColor, subsolar, toXYZ } from "./util.js";

const RE = 6371;
function vnoise(x, y) {
  const xi = Math.floor(x), yi = Math.floor(y), xf = x - xi, yf = y - yi;
  const h = (a, b) => hash(a * 57.31 + b * 113.7);
  const u = xf * xf * (3 - 2 * xf), v = yf * yf * (3 - 2 * yf);
  return (h(xi, yi) * (1 - u) + h(xi + 1, yi) * u) * (1 - v) + (h(xi, yi + 1) * (1 - u) + h(xi + 1, yi + 1) * u) * v;
}
function fbm(x, y) { let s = 0, a = 0.5; for (let o = 0; o < 4; o++) { s += a * vnoise(x, y); x *= 2.03; y *= 2.03; a *= 0.5; } return s / 0.9375; }
const RAIN_VS = /* glsl */ `
  attribute float aT; attribute float aPhase; attribute float aOn; varying float vT; varying float vP; varying float vOn;
  void main() { vT = aT; vP = aPhase; vOn = aOn; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`;
const RAIN_FS = /* glsl */ `
  uniform float time; varying float vT; varying float vP; varying float vOn;
  void main() { float d = fract(vT * 2.5 - time * 1.6 + vP); float a = smoothstep(0.0, 0.25, d) * (1.0 - smoothstep(0.35, 0.55, d));
    gl_FragColor = vec4(vec3(0.55, 0.85, 1.0) * a * vOn * 0.55, a * vOn * 0.55); }`;
const clampN = (x) => Math.max(0, Math.min(1, x));
const v3 = (a) => new THREE.Vector3(a[0], a[1], a[2]);
const geo = (lat, lon, r = 1) => v3(toXYZ(lat, lon, r));

const EARTH_VS = /* glsl */ `
  varying vec2 vUv; varying vec3 vN; varying vec3 vW;
  void main() {
    vUv = uv; vN = normalize(position);
    vec4 w = modelMatrix * vec4(position, 1.0); vW = w.xyz;
    gl_Position = projectionMatrix * viewMatrix * w;
  }`;
const EARTH_FS = /* glsl */ `
  uniform sampler2D dayTex; uniform sampler2D nightTex; uniform sampler2D cloudTex; uniform sampler2D detailTex;
  uniform vec4 detailBox; uniform vec3 sunDir; uniform float cloudShift; uniform vec3 camPos; uniform float near;
  varying vec2 vUv; varying vec3 vN; varying vec3 vW;
  float h2(vec2 p) { vec3 p3 = fract(vec3(p.xyx) * 0.1031); p3 += dot(p3, p3.yzx + 33.33); return fract((p3.x + p3.y) * p3.z); }
  float vn(vec2 p) { vec2 i = floor(p), f = fract(p); f = f * f * (3.0 - 2.0 * f);
    return mix(mix(h2(i), h2(i + vec2(1, 0)), f.x), mix(h2(i + vec2(0, 1)), h2(i + vec2(1, 1)), f.x), f.y); }
  void main() {
    vec3 n = normalize(vN);
    float lat = degrees(asin(clamp(n.y, -1.0, 1.0)));
    float lon = degrees(atan(-n.z, n.x));
    vec3 day = texture2D(dayTex, vUv).rgb;
    vec3 dayG = day;   // coarse global imagery: oceans are unambiguous here
    vec2 du = vec2((lon - detailBox.x) / (detailBox.y - detailBox.x), (lat - detailBox.z) / (detailBox.w - detailBox.z));
    if (du.x > 0.0 && du.x < 1.0 && du.y > 0.0 && du.y < 1.0) {
      float edge = smoothstep(0.0, 0.05, min(min(du.x, 1.0 - du.x), min(du.y, 1.0 - du.y)));
      day = mix(day, texture2D(detailTex, du).rgb, edge);
    }
    // procedural micro-detail where the imagery runs out of pixels (close range only)
    vec2 q = vec2(lon, lat) * 260.0;
    float det = vn(q) * 0.5 + vn(q * 2.7) * 0.3 + vn(q * 7.1) * 0.2;
    day *= 1.0 + near * 0.16 * (det - 0.5);
    float ndl = dot(n, sunDir);
    float dayMix = smoothstep(-0.10, 0.16, ndl);
    vec3 night = texture2D(nightTex, vUv).rgb;
    night = pow(night, vec3(1.35)) * vec3(1.55, 1.15, 0.75) * 2.4 * (1.0 - 0.8 * near);
    float c = texture2D(cloudTex, vUv + vec2(cloudShift, 0.0)).r;
    c = smoothstep(0.18, 0.95, c) * (1.0 - 0.85 * near);
    vec3 V = normalize(camPos - vW);
    float water = smoothstep(0.03, 0.12, dayG.b - max(dayG.r, dayG.g)) * (1.0 - smoothstep(0.22, 0.38, dot(dayG, vec3(0.333))));
    water *= 1.0 - 0.9 * near;
    vec3 H = normalize(sunDir + V);
    float spec = pow(max(dot(n, H), 0.0), 70.0) * water;
    float lamb = max(ndl, 0.0);
    float amb = 0.006 + 0.035 * smoothstep(-0.2, 0.05, ndl);
    vec3 lit = day * (amb + 1.25 * pow(lamb, 0.75)) + vec3(1.0, 0.9, 0.75) * spec * 0.85 * lamb;
    lit = mix(lit, vec3(0.92, 0.95, 1.0) * (amb + 0.95 * lamb), c * 0.7);
    // city lights show only once the sun is below the horizon (civil dusk onward)
    float lights = 1.0 - smoothstep(-0.13, -0.01, ndl);
    vec3 col = lit + night * lights * (1.0 - c * 0.85);
    col *= mix(1.0, 1.0, dayMix);
    float tw = exp(-pow((ndl + 0.02) / 0.06, 2.0));
    col += vec3(0.38, 0.13, 0.03) * tw * 0.035 * (1.0 - c);
    float fr = pow(1.0 - max(dot(n, V), 0.0), 3.2);
    col += vec3(0.22, 0.5, 1.0) * fr * 0.6 * (0.2 + 0.8 * smoothstep(-0.25, 0.45, ndl));
    gl_FragColor = vec4(col, 1.0);
  }`;
const ATMO_VS = /* glsl */ `
  varying vec3 vNV; varying vec3 vNW;
  void main() { vNV = normalize(normalMatrix * normal); vNW = normalize(position);
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`;
const ATMO_FS = /* glsl */ `
  uniform vec3 sunDir; varying vec3 vNV; varying vec3 vNW;
  void main() {
    float i = pow(max(0.72 - dot(vNV, vec3(0.0, 0.0, 1.0)), 0.0), 3.4);
    float sun = 0.18 + 0.82 * smoothstep(-0.35, 0.5, dot(vNW, sunDir));
    gl_FragColor = vec4(vec3(0.32, 0.62, 1.0) * i * sun * 1.4, 1.0);
  }`;
const SENSOR_FS = /* glsl */ `
  uniform sampler2D tDiffuse; uniform float time; uniform float mode; uniform vec2 res; uniform float grain;
  varying vec2 vUv;
  float hash(vec2 p) { vec3 p3 = fract(vec3(p.xyx) * 0.1031); p3 += dot(p3, p3.yzx + 33.33); return fract((p3.x + p3.y) * p3.z); }
  vec3 ironbow(float t) {
    t = clamp(t, 0.0, 1.0); float s = t * 6.0;
    vec3 c0 = vec3(0.0), c1 = vec3(0.13, 0.0, 0.3), c2 = vec3(0.49, 0.0, 0.45), c3 = vec3(0.86, 0.1, 0.18),
         c4 = vec3(1.0, 0.55, 0.0), c5 = vec3(1.0, 0.91, 0.32), c6 = vec3(1.0);
    if (s < 1.0) return mix(c0, c1, s); if (s < 2.0) return mix(c1, c2, s - 1.0);
    if (s < 3.0) return mix(c2, c3, s - 2.0); if (s < 4.0) return mix(c3, c4, s - 3.0);
    if (s < 5.0) return mix(c4, c5, s - 4.0); return mix(c5, c6, s - 5.0);
  }
  void main() {
    vec2 c = vUv - 0.5; float r2 = dot(c, c);
    float ca = 0.0035 * r2;
    vec3 col = vec3(texture2D(tDiffuse, vUv + c * ca).r, texture2D(tDiffuse, vUv).g, texture2D(tDiffuse, vUv - c * ca).b);
    float lum = dot(col, vec3(0.2126, 0.7152, 0.0722));
    vec2 px = vUv * res;
    if (mode > 1.5) {
      float n = hash(floor(px * 0.5) + floor(time * 30.0)) * 0.12;
      col = vec3(0.16, 1.0, 0.42) * pow(clamp(lum * 2.2 + n, 0.0, 1.0), 0.8);
      col *= 0.86 + 0.14 * step(0.5, fract(px.y * 0.5));
    } else if (mode > 0.5) {
      col = ironbow(pow(clamp(lum * 1.7, 0.0, 1.0), 0.75));
    }
    col += (hash(px + fract(time) * 97.0) - 0.5) * 0.016 * grain;
    col *= 0.94 + 0.06 * sin(px.y * 3.14159);
    col *= 1.0 - 0.6 * pow(r2 * 2.0, 1.5);
    gl_FragColor = vec4(col, 1.0);
  }`;
const POINT_VS = /* glsl */ `
  attribute float aSize; attribute vec3 aColor; varying vec3 vColor;
  void main() { vColor = aColor; vec4 mv = modelViewMatrix * vec4(position, 1.0);
    gl_PointSize = aSize; gl_Position = projectionMatrix * mv; }`;
const POINT_FS = /* glsl */ `
  varying vec3 vColor;
  void main() { vec2 d = gl_PointCoord - 0.5; float r = length(d);
    float a = smoothstep(0.5, 0.0, r); float core = smoothstep(0.18, 0.0, r);
    gl_FragColor = vec4(vColor * (a * 0.55 + core * 1.6), a); }`;
const TRAIL_VS = /* glsl */ `
  attribute float aAlpha; varying float vA;
  void main() { vA = aAlpha; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`;
const TRAIL_FS = /* glsl */ `
  uniform vec3 color; varying float vA;
  void main() { gl_FragColor = vec4(color * vA, vA); }`;

export class Globe {
  constructor(canvas, replay, { capture = false } = {}) {
    this.R = replay;
    this.canvas = canvas;
    this.capture = capture;   // video capture: no per-frame film grain (it defeats the encoder)
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true, preserveDrawingBuffer: capture, powerPreference: "high-performance" });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(32, 1, 0.0005, 400);
    this.time = 0;
    this.mode = 0;
    this.beams = new Map();
    this.flow = 0;
  }

  async load(onProgress = () => {}) {
    const tl = new THREE.TextureLoader();
    const tex = (url, srgb = true) => new Promise((res, rej) => tl.load(url, (t) => {
      if (srgb) t.colorSpace = THREE.SRGBColorSpace;
      t.anisotropy = this.renderer.capabilities.getMaxAnisotropy();
      onProgress(url);
      res(t);
    }, undefined, rej));
    const [day, night, clouds, detail] = await Promise.all([
      tex("assets/earth-day.jpg"), tex("assets/earth-night.jpg"), tex("assets/clouds.jpg", false), tex("assets/detail-us-south.jpg"),
    ]);
    clouds.wrapS = THREE.RepeatWrapping;
    this.uniforms = {
      dayTex: { value: day }, nightTex: { value: night }, cloudTex: { value: clouds }, detailTex: { value: detail },
      detailBox: { value: new THREE.Vector4(-116, -74, 24, 42) }, sunDir: { value: new THREE.Vector3(1, 0, 0) },
      cloudShift: { value: 0 }, camPos: { value: new THREE.Vector3() }, near: { value: 0 },
    };
    this.earth = new THREE.Mesh(new THREE.SphereGeometry(1, 320, 160),
      new THREE.ShaderMaterial({ uniforms: this.uniforms, vertexShader: EARTH_VS, fragmentShader: EARTH_FS }));
    this.scene.add(this.earth);
    this.atmo = new THREE.Mesh(new THREE.SphereGeometry(1.028, 160, 80), new THREE.ShaderMaterial({
      uniforms: { sunDir: this.uniforms.sunDir }, vertexShader: ATMO_VS, fragmentShader: ATMO_FS,
      side: THREE.BackSide, blending: THREE.AdditiveBlending, transparent: true, depthWrite: false,
    }));
    this.scene.add(this.atmo);
    this._stars();
    await this._outlines();
    this._graticule();
    this._orbits();
    this._satellites();
    this._stations();
    this._storm();
    this._post();
  }

  // ------------------------------------------------------------------ static layers
  _stars() {
    const n = 5200, pos = new Float32Array(n * 3), size = new Float32Array(n), col = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) {
      const u = hash(i * 3.1) * 2 - 1, th = hash(i * 7.7) * Math.PI * 2, s = Math.sqrt(1 - u * u);
      pos.set([90 * s * Math.cos(th), 90 * u, 90 * s * Math.sin(th)], i * 3);
      const m = Math.pow(hash(i * 1.3), 6);
      size[i] = 1.2 + m * 3.2;
      const warm = hash(i * 5.9);
      col.set([0.75 + 0.25 * warm, 0.82, 1.0 - 0.25 * warm].map((c) => c * (0.35 + 0.9 * m)), i * 3);
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    g.setAttribute("aSize", new THREE.BufferAttribute(size, 1));
    g.setAttribute("aColor", new THREE.BufferAttribute(col, 3));
    this.scene.add(new THREE.Points(g, new THREE.ShaderMaterial({ vertexShader: POINT_VS, fragmentShader: POINT_FS,
      transparent: true, depthWrite: false, blending: THREE.AdditiveBlending })));
  }

  async _outlines() {
    const add = (coords, r, color, opacity) => {
      const pts = [];
      for (const line of coords) for (let i = 0; i + 1 < line.length; i++) {
        pts.push(...toXYZ(line[i][1], line[i][0], r), ...toXYZ(line[i + 1][1], line[i + 1][0], r));
      }
      const g = new THREE.BufferGeometry();
      g.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
      const m = new THREE.LineBasicMaterial({ color, transparent: true, opacity, depthWrite: false });
      const l = new THREE.LineSegments(g, m);
      this.scene.add(l);
      return l;
    };
    const [world, us] = await Promise.all([fetch("assets/countries-50m.json").then((r) => r.json()),
      fetch("assets/states-10m.json").then((r) => r.json())]);
    const mesh = window.topojson.mesh;
    this.coast = add(mesh(world, world.objects.countries).coordinates, 1.0004, 0x7fe7f2, 0.22);
    this.states = add(mesh(us, us.objects.states, (a, b) => a !== b).coordinates, 1.0004, 0x7fe7f2, 0.16);
  }

  _graticule() {
    const pts = [];
    for (let lat = -75; lat <= 75; lat += 15) for (let lon = -180; lon < 180; lon += 2)
      pts.push(...toXYZ(lat, lon, 1.0003), ...toXYZ(lat, lon + 2, 1.0003));
    for (let lon = -180; lon < 180; lon += 15) for (let lat = -80; lat < 80; lat += 2)
      pts.push(...toXYZ(lat, lon, 1.0003), ...toXYZ(lat + 2, lon, 1.0003));
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
    this.grid = new THREE.LineSegments(g, new THREE.LineBasicMaterial({ color: 0x7fe7f2, transparent: true, opacity: 0.05, depthWrite: false }));
    this.scene.add(this.grid);
  }

  _orbits() {
    // The O3b MEO shell: equatorial and circular, so it is a circle in the Earth-fixed frame too.
    const o3b = this.R.D.satellites.find((s) => s.family === "O3b");
    const p = o3b && this.R.satPos(o3b.id, this.R.t0);
    const r = 1 + (p ? p[2] : 8062) / RE;
    const pts = [];
    for (let i = 0; i <= 360; i++) pts.push(...toXYZ(0, i, r));
    const g = new LineGeometry(); g.setPositions(pts);
    this.ringMat = new LineMaterial({ color: 0x7fe7f2, linewidth: 1, transparent: true, opacity: 0.22, dashed: true,
      dashSize: 0.02, gapSize: 0.03, depthWrite: false });
    this.ring = new Line2(g, this.ringMat); this.ring.computeLineDistances();
    this.scene.add(this.ring);
  }

  _satellites() {
    const sats = this.R.D.satellites.filter((s) => this.R.D.tracks.pos[s.id]);
    this.satList = sats;
    const n = sats.length;
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(new Float32Array(n * 3), 3));
    g.setAttribute("aSize", new THREE.BufferAttribute(new Float32Array(n), 1));
    g.setAttribute("aColor", new THREE.BufferAttribute(new Float32Array(n * 3), 3));
    this.satPoints = new THREE.Points(g, new THREE.ShaderMaterial({ vertexShader: POINT_VS, fragmentShader: POINT_FS,
      transparent: true, depthWrite: false, blending: THREE.AdditiveBlending }));
    this.scene.add(this.satPoints);
    this.trails = sats.map(() => {
      const tg = new THREE.BufferGeometry(), N = 41;
      tg.setAttribute("position", new THREE.BufferAttribute(new Float32Array(N * 3), 3));
      const a = new Float32Array(N); for (let i = 0; i < N; i++) a[i] = Math.pow(i / (N - 1), 1.6) * 0.6;
      tg.setAttribute("aAlpha", new THREE.BufferAttribute(a, 1));
      const line = new THREE.Line(tg, new THREE.ShaderMaterial({ uniforms: { color: { value: new THREE.Color(0x9fdcff) } },
        vertexShader: TRAIL_VS, fragmentShader: TRAIL_FS, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending }));
      this.scene.add(line);
      return line;
    });
  }

  _stations() {
    this.stationObjs = this.R.D.stations.map((s) => {
      const p = geo(s.lat, s.lon, 1.0006);
      const grp = new THREE.Group(); grp.position.copy(p); grp.lookAt(p.clone().multiplyScalar(2));
      const dot = new THREE.Mesh(new THREE.CircleGeometry(1, 32), new THREE.MeshBasicMaterial({ color: 0xe9f4ff, transparent: true, depthWrite: false }));
      const rings = [0, 1].map(() => new THREE.Mesh(new THREE.RingGeometry(0.82, 1, 64),
        new THREE.MeshBasicMaterial({ color: 0x7fe7f2, transparent: true, depthWrite: false, side: THREE.DoubleSide })));
      grp.add(dot, ...rings);
      this.scene.add(grp);
      return { s, p, grp, dot, rings };
    });
  }

  _storm() {
    const c = this.R.st["SITE-7576272aee77"] || this.R.D.stations[0];
    const lat0 = c.lat - 1.15, lat1 = c.lat + 1.15, lon0 = c.lon - 1.4, lon1 = c.lon + 1.4;
    this.stormBox = { lat0, lat1, lon0, lon1 };
    this.stormCanvas = document.createElement("canvas");
    this.stormCanvas.width = this.stormCanvas.height = 200;
    this.stormTex = new THREE.CanvasTexture(this.stormCanvas);
    this.stormTex.colorSpace = THREE.SRGBColorSpace;
    this.stormLayers = [[1.0003, 0.95], [1.0009, 0.42], [1.0015, 0.22]].map(([r, op]) => {
      const g = new THREE.SphereGeometry(r, 64, 64, (lon0 + 180) * D2R, (lon1 - lon0) * D2R, (90 - lat1) * D2R, (lat1 - lat0) * D2R);
      const m = new THREE.Mesh(g, new THREE.MeshBasicMaterial({ map: this.stormTex, transparent: true, opacity: op,
        depthWrite: false, blending: THREE.NormalBlending }));
      this.scene.add(m);
      return m;
    });
    // rain shafts: streaks under the cells, switched on where the modelled rate is heavy
    const N = 900, sp = new Float32Array(N * 6), aT = new Float32Array(N * 2), aP = new Float32Array(N * 2), aOn = new Float32Array(N * 2);
    this.shafts = [];
    for (let j = 0; j < N; j++) {
      const r = Math.sqrt(hash(j * 1.7)) * 1.6, th = hash(j * 9.1) * Math.PI * 2;
      this.shafts.push([r * Math.cos(th), r * Math.sin(th), hash(j * 3.3) < 0.82 ? 0 : 1]);
      aT[j * 2] = 0; aT[j * 2 + 1] = 1; aP[j * 2] = aP[j * 2 + 1] = hash(j * 4.9);
    }
    const rg = new THREE.BufferGeometry();
    rg.setAttribute("position", new THREE.BufferAttribute(sp, 3));
    rg.setAttribute("aT", new THREE.BufferAttribute(aT, 1));
    rg.setAttribute("aPhase", new THREE.BufferAttribute(aP, 1));
    rg.setAttribute("aOn", new THREE.BufferAttribute(aOn, 1));
    this.rain = new THREE.LineSegments(rg, new THREE.ShaderMaterial({ uniforms: { time: { value: 0 } }, vertexShader: RAIN_VS,
      fragmentShader: RAIN_FS, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending }));
    this.scene.add(this.rain);
    this.ghosts = [0, 1].map(() => {
      const g = new THREE.BufferGeometry();
      g.setAttribute("position", new THREE.BufferAttribute(new Float32Array(65 * 3), 3));
      const l = new THREE.LineLoop(g, new THREE.LineDashedMaterial({ color: 0xe9f4ff, dashSize: 0.0012, gapSize: 0.0009,
        transparent: true, opacity: 0.7, depthWrite: false }));
      this.scene.add(l);
      return l;
    });
  }

  _post() {
    const r = this.renderer;
    this.composer = new EffectComposer(r);
    this.composer.addPass(new RenderPass(this.scene, this.camera));
    this.bloom = new UnrealBloomPass(new THREE.Vector2(512, 512), 0.75, 0.55, 0.55);
    this.composer.addPass(this.bloom);
    this.sensor = new ShaderPass({ uniforms: { tDiffuse: { value: null }, time: { value: 0 }, mode: { value: 0 },
      res: { value: new THREE.Vector2(1, 1) }, grain: { value: this.capture ? 0 : 1 } }, vertexShader: `varying vec2 vUv; void main(){ vUv = uv; gl_Position = projectionMatrix*modelViewMatrix*vec4(position,1.0); }`,
      fragmentShader: SENSOR_FS });
    this.composer.addPass(this.sensor);
    this.composer.addPass(new OutputPass());
  }

  resize(w, h) {
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h; this.camera.updateProjectionMatrix();
    this.composer.setSize(w, h);
    const pr = this.renderer.getPixelRatio();
    this.sensor.uniforms.res.value.set(w * pr, h * pr);
    for (const m of [this.ringMat, ...[...this.beams.values()].map((b) => b.mat)]) m.resolution.set(w, h);
    this.size = [w, h];
  }

  setMode(m) { this.mode = m; this.sensor.uniforms.mode.value = m; }

  // ------------------------------------------------------------------ dynamic state
  update(t, dt, { selected, alertSev, focusStation }) {
    const R = this.R, i = R.tick(t);
    this.time += dt;
    const [sl, sn] = subsolar(t);
    this.uniforms.sunDir.value.copy(geo(sl, sn)).normalize();
    this.uniforms.cloudShift.value = (t - R.t0) * 1.2e-6;
    this.uniforms.camPos.value.copy(this.camera.position);
    this.sensor.uniforms.time.value = this.time;
    const camD = this.camera.position.length();
    const near = THREE.MathUtils.clamp((2.2 - camD) / 1.1, 0, 1);       // 0 far .. 1 close
    this.uniforms.near.value = THREE.MathUtils.clamp((1.35 - camD) / 0.3, 0, 1);
    if (this.rain) this.rain.material.uniforms.time.value = this.time;
    this.coast.material.opacity = 0.14 + 0.2 * near;
    this.states.material.opacity = 0.05 + 0.22 * near;
    this.grid.material.opacity = 0.05 * (1 - near) + 0.015;

    // satellites
    const pos = this.satPoints.geometry.attributes.position, size = this.satPoints.geometry.attributes.aSize,
      col = this.satPoints.geometry.attributes.aColor;
    const tracked = new Map();
    for (const a of R.D.antennas) {
      const L = R.link(a.id, t);
      if (L.live) tracked.set(L.tgt, a.id);
    }
    const selSat = selected ? R.link(selected, t).tgt : -1;
    this.satScreen = [];
    this.satList.forEach((s, j) => {
      const p = R.satPos(s.id, t);
      const xyz = toXYZ(p[0], p[1], 1 + p[2] / RE);
      pos.setXYZ(j, ...xyz);
      const idx = R.satIndex[s.id], on = tracked.has(idx), sel = idx === selSat;
      size.setX(j, sel ? 15 : on ? 9 : 5);
      const c = sel ? [1, 1, 1] : on ? [0.62, 0.95, 1] : [0.55, 0.62, 0.72];
      col.setXYZ(j, ...c);
      // trail: the last 20 minutes
      const tp = this.trails[j].geometry.attributes.position, N = tp.count;
      for (let k = 0; k < N; k++) {
        const q = R.satPos(s.id, t - (N - 1 - k) * 30);
        tp.setXYZ(k, ...toXYZ(q[0], q[1], 1 + q[2] / RE));
      }
      tp.needsUpdate = true;
      this.trails[j].material.uniforms.color.value.set(sel ? 0xffffff : on ? 0x7fe7f2 : 0x6b7f99);
    });
    pos.needsUpdate = size.needsUpdate = col.needsUpdate = true;

    // beams: dashes flow from satellite to ground at a speed set by throughput
    this.flow += dt;
    const live = new Set();
    for (const a of R.D.antennas) {
      const L = R.link(a.id, t);
      if (!L.live) continue;
      live.add(a.id);
      let b = this.beams.get(a.id);
      if (!b) {
        const mat = new LineMaterial({ linewidth: 2, transparent: true, opacity: 0.95, dashed: true, dashSize: 0.022,
          gapSize: 0.014, depthWrite: false });
        if (this.size) mat.resolution.set(...this.size);
        const line = new Line2(new LineGeometry(), mat);
        this.scene.add(line);
        b = { line, mat, phase: hash(a.id.length * 13.7) };
        this.beams.set(a.id, b);
      }
      const st = R.st[a.station], sat = R.D.satellites[L.tgt], p = R.satPos(sat.id, t);
      const A = toXYZ(st.lat, st.lon, 1.0006), B = toXYZ(p[0], p[1], 1 + p[2] / RE);
      b.line.geometry.setPositions([...B, ...A]);
      b.line.computeLineDistances();
      let color = marginColor(L.snr, R.closure);
      const sev = alertSev[a.id];
      if (sev === 0) color = PALETTE.fail; else if (sev === 1 && color === PALETTE.ok) color = PALETTE.thin;
      b.mat.color.set(color);
      const sel = a.id === selected;
      b.mat.linewidth = sel ? 4 : 1.8;
      b.mat.opacity = sel ? 1 : selected ? 0.55 : 0.85;
      // dashes stay roughly screen-sized: scale them with the camera's distance to the station
      const near = Math.min(1, Math.max(0.03, this.camera.position.distanceTo(new THREE.Vector3(...A)) / 3.2));
      b.mat.dashSize = 0.03 * near; b.mat.gapSize = 0.018 * near;
      const speed = (0.1 + 0.45 * Math.min(1, Math.max(0, (L.snr - R.closure) / 30))) * near;
      b.phase += speed * dt;
      b.mat.dashOffset = -b.phase;
      b.line.visible = true;
    }
    for (const [id, b] of this.beams) if (!live.has(id)) b.line.visible = false;

    // stations: screen-constant markers with a slow pulse
    const k = Math.max(0.004, camD * 0.0042);
    this.stationObjs.forEach((o, j) => {
      const d = this.camera.position.distanceTo(o.p), sc = Math.max(0.0009, d * 0.0034);
      o.dot.scale.setScalar(sc * 0.55);
      o.rings.forEach((rg, n) => {
        const ph = ((this.time * 0.45 + n * 0.5 + j * 0.13) % 1);
        rg.scale.setScalar(sc * (1 + ph * 3.2));
        rg.material.opacity = 0.55 * (1 - ph) * (o.s.id === focusStation ? 1.6 : 1);
      });
    });
    void k;

    // storm volume and nowcast ghosts
    if (i !== this._stormTick) { this._stormTick = i; this._drawStorm(i); }
  }

  _drawStorm(i) {
    const R = this.R, cv = this.stormCanvas, ctx = cv.getContext("2d"), n = cv.width;
    const { lat0, lat1, lon0, lon1 } = this.stormBox;
    const img = ctx.createImageData(n, n), kmLon = 111.32 * Math.cos(((lat0 + lat1) / 2) * D2R);
    const cells = R.D.rain.cells;
    const ramp = [[1, [4, 233, 231]], [2, [1, 159, 244]], [4, [2, 253, 2]], [7, [1, 197, 1]], [11, [0, 142, 0]],
      [16, [253, 248, 2]], [22, [229, 188, 0]], [29, [253, 149, 0]], [36, [253, 0, 0]], [45, [212, 0, 0]], [55, [248, 0, 253]]];
    for (let y = 0; y < n; y++) for (let x = 0; x < n; x++) {
      const lat = lat1 - (y / (n - 1)) * (lat1 - lat0), lon = lon0 + (x / (n - 1)) * (lon1 - lon0);
      let r = 0;
      for (const c of cells) {
        const dy = (lat - c.lat[i]) * 111.32, dx = (lon - c.lon[i]) * kmLon;
        r += c.mmh[i] * Math.exp(-(dx * dx + dy * dy) / (c.radius_km * c.radius_km));
      }
      // display texture: fBm advected with the storm, +/-30 % around the modelled rate
      const ax = (lon - cells[0].lon[i]) * kmLon, ay = (lat - cells[0].lat[i]) * 111.32;
      r *= 0.7 + 0.6 * fbm(ax * 0.12, ay * 0.12);
      if (r < 1) continue;
      let col = ramp[0][1];
      for (const [th, cc] of ramp) if (r >= th) col = cc;
      const o = (y * n + x) * 4;
      img.data[o] = col[0]; img.data[o + 1] = col[1]; img.data[o + 2] = col[2];
      img.data[o + 3] = Math.round(Math.min(255, 70 + 170 * Math.min(1, r / 18)));
    }
    ctx.putImageData(img, 0, 0);
    this.stormTex.needsUpdate = true;
    // rain shafts follow their cell; heavier rain lights more of them
    const sp = this.rain.geometry.attributes.position, on = this.rain.geometry.attributes.aOn;
    this.shafts.forEach(([u, v, which], j) => {
      const c = cells[Math.min(which, cells.length - 1)], rad = c.radius_km * (which ? 0.7 : 1);
      const lat = c.lat[i] + (v * rad) / 111.32, lon = c.lon[i] + (u * rad) / kmLon;
      let rate = 0;
      for (const cc of cells) {
        const dy = (lat - cc.lat[i]) * 111.32, dx = (lon - cc.lon[i]) * kmLon;
        rate += cc.mmh[i] * Math.exp(-(dx * dx + dy * dy) / (cc.radius_km * cc.radius_km));
      }
      const top = 1 + (R.D.rain.freezing_km + 0.36) * 1.6 / RE;
      sp.setXYZ(j * 2, ...toXYZ(lat, lon, top)); sp.setXYZ(j * 2 + 1, ...toXYZ(lat, lon, 1.0002));
      const k = clampN((rate - 6) / 20);
      on.setX(j * 2, k); on.setX(j * 2 + 1, k);
    });
    sp.needsUpdate = on.needsUpdate = true;
    const gh = R.D.rain.nowcast[i].filter((g) => g[5] < 15 && g[4] >= 1);
    this.ghosts.forEach((l, j) => {
      const g = gh[0];
      l.visible = !!g;
      if (!g) return;
      const [la, lo] = j === 0 ? [g[0], g[1]] : [g[2], g[3]];
      const p = l.geometry.attributes.position, rad = g[5];
      for (let q = 0; q <= 64; q++) {
        const a = (q / 64) * Math.PI * 2;
        p.setXYZ(q, ...toXYZ(la + (rad * Math.sin(a)) / 111.32, lo + (rad * Math.cos(a)) / kmLon, 1.0018));
      }
      p.needsUpdate = true;
      l.computeLineDistances();
    });
  }

  render() { this.composer.render(); }

  // Screen position of a scene point and whether the Earth hides it.
  project(p) {
    const v = p.clone().project(this.camera);
    const [w, h] = this.size;
    const cam = this.camera.position;
    // occlusion: does the segment camera->p cross the unit sphere before p?
    const d = p.clone().sub(cam), len = d.length(); d.normalize();
    const b = cam.dot(d), c = cam.lengthSq() - 1, disc = b * b - c;
    let hidden = false;
    if (disc > 0) { const t1 = -b - Math.sqrt(disc); hidden = t1 > 1e-4 && t1 < len - 1e-3; }
    return { x: (v.x * 0.5 + 0.5) * w, y: (-v.y * 0.5 + 0.5) * h, front: v.z < 1 && !hidden };
  }
  stationPoint(id) { const s = this.R.st[id]; return geo(s.lat, s.lon, 1.0006); }
  satPoint(satIdx, t) {
    const s = this.R.D.satellites[satIdx], p = this.R.satPos(s.id, t);
    return p ? geo(p[0], p[1], 1 + p[2] / RE) : null;
  }
}
