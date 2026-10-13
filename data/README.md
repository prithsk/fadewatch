# Data

* `demo-fleet/`: the [Constellation demo fleet](https://huggingface.co/datasets/constellationspace/demo-fleet)
  snapshot of 2026-10-10T23:07:10Z (6 ground stations, 39 satellites, 21 links, 4 demand pools),
  unchanged. © Constellation Space, licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
* `tle/demo-fleet-2026-10-12.tle`: CelesTrak GP elements for the 39 satellites, fetched 2026-10-12
  (epochs day 285). Kept in the repo so results are reproducible; `Propagator` accepts any newer file.
* `scorecard.json`: output of `fadewatch backtest` on the demo scenario.
