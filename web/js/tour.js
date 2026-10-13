// Guided replay (index.html?tour): camera shots, kinetic captions and sound cues keyed to
// tour seconds, so the same tour plays live or frame-by-frame for capture (?tour&capture).
const PHX = "SITE-0da7faea0f45__ant0", TX1 = "SITE-7576272aee77__ant1", WMP7 = "SITE-5c44ebc4a84e__ant7";
const WIDE = { lat: 18, lon: -96, r: 0, range: 3.9, heading: 0, tilt: 0 };
const MEO = { lat: 6, lon: -99, r: 0, range: 5.3, heading: 0, tilt: 0 };

export function run(f) {
  const speed = (v) => { if (v) { f.S.speed = v; document.getElementById("speed").textContent = `${v}×`; } f.setPlaying(!!v); };
  const plans = (on) => { if (f.S.closedLoop !== on) document.getElementById("cl").click(); };
  const steps = [
    [0.0, () => { f.seek(0); speed(20); f.Dir.cut({ ...WIDE, range: 5.6, lon: -110 }); f.Dir.fly(WIDE, 7.5); },
      ["Fadewatch", "sees satellite links fail before they do"]],
    [4.6, null, ["Constellation's demo fleet", "6 gateways · 21 antennas · 39 satellites, on real orbits"]],
    [8.6, () => f.Dir.fly(MEO, 3.2), ["Their SNR model repeats the last value", "snr-baseline-v1 is persistence: it cannot see a fade coming"]],
    [13.0, () => { f.seek(0); speed(0); f.select({ link: PHX }, true, 3.0); }, ["Phoenix loses its satellite at 23:16:36", "SGP4 knows when O3B-FM18 sets. Persistence doesn't."]],
    [17.0, () => speed(60), null],
    [26.7, () => f.cue("drop"), ["Dropped, on schedule", "one antenna: 75 s gap, pool TMPEAZX1 warned 10 min ahead"]],
    [31.0, () => { speed(0); f.seek(13); f.select({ link: TX1, close: 0.62 }, true, 3.2); }, ["A storm is closing on Texas Gateway", "19 GHz. Fadewatch integrates the radar nowcast along each slant path"]],
    [35.5, () => speed(40), null],
    [40.5, null, ["Fadewatch sees the fade coming", "persistence only follows it down"]],
    [47.5, () => { f.setMode(1); f.cue("mode"); }, null],
    [50.5, () => { f.setMode(0); f.cue("mode"); }, ["DLLSTXX1 short of capacity at 23:32", "called at 23:22"]],
    [56.5, () => { plans(true); f.cue("plans"); }, ["Plans executed: −69% unserved", "spare antenna on the clearest path, make-before-break handovers"]],
    [62.5, () => { plans(false); speed(0); f.seek(33.5); f.select({ link: PHX }, true, 2.6); }, ["Phoenix's receive chain starts to fail", null]],
    [65.5, () => speed(40), null],
    [70.0, null, ["Not geometry. Not weather.", "equipment drift: loss of lock called 31 min ahead"]],
    [78.0, () => { speed(0); f.seek(50.5); f.select({ link: WMP7 }, true, 2.6); }, ["Interference has no precursor", "flagged within a minute, never extrapolated"]],
    [81.0, () => speed(30), null],
    [87.5, () => { speed(0); f.caption(null); f.SC.toggle(true); f.cue("score"); }, null],
    [98.5, () => { f.SC.toggle(false); f.S.sel = null; speed(20); f.setMode(0); f.Dir.fly({ ...WIDE, range: 4.4 }, 4); },
      ["Fadewatch", "Constellation API + MCP client · MCP server · Python"]],
    [106.5, () => f.caption(null), null],
  ];
  f.S.tour = true;
  let t = 0, k = 0;
  f.tourTick = (dt) => {
    t += dt;
    while (k < steps.length && steps[k][0] <= t) {
      const [, fn, cap] = steps[k++];
      if (fn) fn();
      if (cap) f.caption(cap[0], cap[1] || "");
    }
    return k >= steps.length;
  };
  f.tourLength = 108;
}
