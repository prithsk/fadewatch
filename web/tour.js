// Guided replay with captions (index.html?tour). Steps are keyed to tour seconds,
// so the same tour plays live or frame-by-frame for video capture (?tour&capture).
const PHX = "SITE-0da7faea0f45__ant0", TX1 = "SITE-7576272aee77__ant1", WMP7 = "SITE-5c44ebc4a84e__ant7";

export function steps(f) {
  const speed = (v) => { if (v) { f.S.speed = v; document.getElementById("speed").value = String(v); } f.setPlaying(!!v); };
  const scroll = (id, from, to, dur) => (u) => { const el = document.getElementById(id); el.scrollTop = from + (to - from) * Math.min(1, u / dur); };
  return [
    [0, () => { f.show("board"); f.seekTo(0); f.select(PHX, true); speed(0); },
      "<b>Fadewatch</b> watches Constellation's demo fleet (6 ground stations, 21 antennas, 39 satellites) and warns before its links fail."],
    [6, () => f.show("findings"), "It starts by checking the telemetry against physics."],
    [9, null, "O3b pointing matches SGP4 to 0.1°. Every SNR matches a two-parameter link budget to 0.003 dB. And the LEO links turn out to be a recording from 25.5 days earlier.", scroll("findings", 0, 520, 7)],
    [17, () => { f.show("board"); f.seekTo(0); f.select(PHX, true); speed(0); },
      "Constellation's served SNR model carries the last value forward. Fadewatch forecasts from orbital geometry: Phoenix Gateway's only antenna loses O3B-FM18 at 23:16:35."],
    [22, () => speed(60), "The dashed purple line is persistence. It keeps predicting 26 dB until the samples stop."],
    [30, null, "Fadewatch has already planned the slew to the next satellite, and flagged that pool TMPEAZX1 has no other link through the 75-second gap."],
    [35, () => speed(0), null],
    [39, () => { f.seekTo(13); f.select(TX1, true); speed(0); },
      "Meanwhile a thunderstorm is heading for Texas Gateway. Fadewatch integrates the radar nowcast along each antenna's slant path through the rain."],
    [46, () => speed(40), "The fan is Fadewatch's forecast; the dotted line is what actually happened. Persistence only follows once the fade has begun."],
    [55, null, "At 19 GHz this is more than 30 dB of fade. Pool DLLSTXX1 runs short of capacity at 23:32, and Fadewatch said so at 23:22."],
    [63, () => { const c = document.getElementById("closed-loop"); if (!c.checked) c.click(); },
      "With Fadewatch's plans executed, a spare antenna comes up on the satellite whose path avoids the cell. DLLSTXX1's unserved traffic falls by two thirds."],
    [72, () => { const c = document.getElementById("closed-loop"); if (c.checked) c.click(); f.seekTo(33.5); f.select(PHX, true); speed(40); },
      "At 23:40 Phoenix Gateway's receive chain starts to degrade."],
    [80, null, "A minute later Fadewatch calls it: not geometry, not weather, an equipment drift, with loss of lock at 00:13, more than 30 minutes ahead. Persistence notices 48 seconds before."],
    [91, () => { f.seekTo(50.5); f.select(WMP7, true); speed(30); },
      "An interferer has no precursor, so nothing can forecast it. Fadewatch flags the step within a minute and stops extrapolating it."],
    [99, () => { speed(0); f.show("backtest"); }, "Across the whole replay:"],
    [101, null, "12 of 12 losses of signal, 2 of 2 fades and 5 of 5 pool shortfalls warned ahead, with no false warnings. Persistence warned for none of them."],
    [107, null, null, scroll("backtest", 0, 360, 5)],
    [112, null, "Fadewatch ships as a Python package with a Constellation API and MCP client, and its own MCP server: ask your agent which links drop next."],
    [120, () => f.caption(null), null],
  ];
}

export function run(f) {
  const list = steps(f);
  f.S.manualUntil = 1e15;            // the tour drives the selection
  let t = 0, k = 0, active = null;
  f.tourTick = (dt) => {
    t += dt;
    while (k < list.length && list[k][0] <= t) {
      const [at, fn, cap, tween] = list[k++];
      if (fn) fn();
      if (cap !== null && cap !== undefined) f.caption(cap);
      active = tween ? { at, tween } : null;
    }
    if (active) active.tween(t - active.at);
    return k >= list.length;
  };
}
