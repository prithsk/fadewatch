"""A replay session: one scenario, simulated once, queried at any instant.

Shared by the MCP server, the web server and the CLI. The engine runs over
the whole replay at start-up (about 25 s for the demo scenario), so every
query afterwards is a lookup at the tick nearest the requested time.
"""
from __future__ import annotations

import json
import math
import os

import numpy as np

from .atmosphere import gas_attenuation, scintillation_sigma_db
from .backtest import run_engine
from .fleet import DATA_DIR
from .forecast import PersistenceBaseline
from .linkbudget import BOLTZMANN_DBW, CLOSURE_THRESHOLD_DB, capacity_mbps, fspl_db, modcod_for
from .orbit import iso, to_unix
from .scenarios import SCENARIOS, load_context
from .simulate import Simulator

SCORECARD_PATH = os.path.join(DATA_DIR, "scorecard.json")


class ReplaySession:
    def __init__(self, scenario: str = "demo"):
        self.ctx = load_context()
        self.cfg = SCENARIOS[scenario](self.ctx.fleet)
        self.rec = Simulator(self.ctx.fleet, self.ctx.prop, self.ctx.cal, self.cfg).run()
        self.engine, self.log = run_engine(self.ctx, self.cfg, self.rec)
        self.t = self.cfg.replay_start
        self.base = PersistenceBaseline()

    # ------------------------------------------------------------ clock
    def set_time(self, when: str | float | None = None, minutes_after_start: float | None = None) -> float:
        if minutes_after_start is not None:
            self.t = self.cfg.replay_start + 60 * minutes_after_start
        elif when is not None:
            self.t = to_unix(when)
        self.t = float(np.clip(self.t, self.log.t[0], self.log.t[-1]))
        return self.t

    def tick(self, t: float | None = None) -> int:
        t = self.t if t is None else t
        return int(np.clip(np.searchsorted(self.log.t, t, side="right") - 1, 0, len(self.log.t) - 1))

    def output(self, t: float | None = None) -> dict:
        return self.log.outputs[self.tick(t)]

    def clock(self) -> dict:
        return {"now": iso(self.t), "replay_start": iso(self.cfg.replay_start), "end": iso(self.log.t[-1]),
                "minutes_elapsed": round((self.t - self.cfg.replay_start) / 60, 2), "scenario": self.cfg.name}

    # ------------------------------------------------------------ queries
    def overview(self) -> dict:
        f = self.ctx.fleet
        out = self.output()
        live = {lid: fc.last for lid, fc in out["forecasts"].items() if fc.last is not None}
        stations = []
        for s in f.stations.values():
            ants = []
            for a in f.antennas_at(s.id):
                smp = live.get(a.id)
                ants.append({"link_id": a.id, "state": "tracking" if smp else "idle",
                             "target": f.satellites[smp.target].name if smp and smp.target in f.satellites else None,
                             "snr_db": round(smp.snr_db, 2) if smp else None,
                             "elevation_deg": round(smp.el_deg, 1) if smp else None,
                             "modcod": modcod_for(smp.snr_db)[0] if smp else None})
            stations.append({"id": s.id, "name": s.name, "pool": s.pool_id, "lat": s.lat_deg, "lon": s.lon_deg,
                             "antennas": ants})
        pools = [{"pool_id": p.pool_id, "capacity_now_mbps": round(float(p.capacity_mbps[0]), 1),
                  "demand_p50_mbps": round(p.demand_p50_mbps, 1), "demand_p99_mbps": round(p.demand_p99_mbps, 1),
                  "min_capacity_next_30min_mbps": round(p.min_capacity_mbps, 1),
                  "shortfall_from": iso(p.shortfall_t) if p.shortfall_t else None}
                 for p in out["pools"].values()]
        return {"clock": self.clock(), "stations": stations, "pools": pools,
                "alert_counts": {s: sum(1 for a in self.log.alerts[self.tick()] if a["severity"] == s)
                                 for s in ("critical", "warning", "info")}}

    def alerts(self, window_minutes: float = 30, min_severity: str = "info") -> dict:
        rank = {"critical": 0, "warning": 1, "info": 2}
        rows = []
        # The per-tick snapshots: Alert objects keep evolving after their tick.
        for d in self.log.alerts[self.tick()]:
            if rank[d["severity"]] > rank[min_severity]:
                continue
            if d["event_t"] and d["event_t"] - self.t > window_minutes * 60:
                continue
            d = dict(d)
            d["event_in_min"] = round(max(d["event_t"] - self.t, 0) / 60, 1) if d["event_t"] else None
            d["warned_for_min"] = round((self.t - d["raised_t"]) / 60, 1)
            rows.append({k: d[k] for k in ("severity", "kind", "cause", "title", "detail", "action", "event_iso",
                                           "event_in_min", "warned_for_min", "link_id", "pool_id")})
        return {"clock": self.clock(), "alerts": rows}

    def link_budget(self, link_id: str) -> dict:
        f = self.ctx.fleet
        ant = f.antennas[link_id]
        cal = self.ctx.cal[ant.station_id]
        out = self.output()
        fc = out["forecasts"].get(link_id)
        if fc is None or fc.last is None:
            return {"link_id": link_id, "state": "idle", "clock": self.clock()}
        s = fc.last
        fspl = float(fspl_db(ant.frequency_ghz, s.range_km))
        gas = float(gas_attenuation(cal.zenith_gas_db, s.el_deg))
        rain = float(fc.rain[0]) if fc.rain is not None else 0.0
        bw = 10 * math.log10(ant.bandwidth_mhz * 1e6)
        expected = ant.eirp_dbw + ant.g_t_db_k - fspl - gas - rain - cal.loss_db - BOLTZMANN_DBW - bw
        return {
            "link_id": link_id, "target": f.satellites[s.target].name if s.target in f.satellites else s.target,
            "clock": self.clock(),
            "geometry": {"azimuth_deg": round(s.az_deg, 2), "elevation_deg": round(s.el_deg, 2),
                         "slant_range_km": round(s.range_km, 1)},
            "budget_db": [
                {"term": "Satellite EIRP", "db": round(ant.eirp_dbw, 2)},
                {"term": "Ground G/T", "db": round(ant.g_t_db_k, 2)},
                {"term": "Free-space loss", "db": round(-fspl, 2)},
                {"term": "Gaseous absorption", "db": round(-gas, 2)},
                {"term": "Rain (nowcast)", "db": round(-rain, 2)},
                {"term": "Station losses (calibrated)", "db": round(-cal.loss_db, 2)},
                {"term": "Boltzmann (−228.6 dBW/K/Hz)", "db": round(-BOLTZMANN_DBW, 2)},
                {"term": f"Bandwidth ({ant.bandwidth_mhz:g} MHz)", "db": round(-bw, 2)},
            ],
            "expected_snr_db": round(expected, 2), "observed_snr_db": round(s.snr_db, 2),
            "residual_db": round(s.snr_db - expected, 2),
            "scintillation_sigma_db": round(float(scintillation_sigma_db(ant.frequency_ghz, s.el_deg)), 3),
            "modcod": modcod_for(s.snr_db)[0], "capacity_mbps": round(capacity_mbps(s.snr_db, ant.bandwidth_mhz), 1),
            "closure_threshold_db": CLOSURE_THRESHOLD_DB,
            "calibration": {"station_loss_db": round(cal.loss_db, 3), "zenith_gas_db": round(cal.zenith_gas_db, 3),
                            "fit_rms_db": None if math.isnan(cal.rms_db) else round(cal.rms_db, 4), "method": cal.method},
        }

    def forecast(self, link_id: str, horizons_minutes: list[float] | None = None) -> dict:
        horizons_minutes = horizons_minutes or [1, 3, 5, 10, 15, 20, 30]
        out = self.output()
        fc = out["forecasts"].get(link_id)
        if fc is None or fc.error:
            return {"link_id": link_id, "error": fc.error if fc else "not tracking", "clock": self.clock()}
        pb = self.base.forecast(link_id, self.rec.history(link_id, self.log.t[self.tick()], 300), self.log.t[self.tick()],
                                np.array([60.0, 180.0, 300.0]))
        rows = []
        for hm in horizons_minutes:
            p10, p50, p90 = fc.at(hm * 60)
            row = {"horizon_min": hm, "fadewatch_p10": _r(p10), "fadewatch_p50": _r(p50), "fadewatch_p90": _r(p90)}
            if hm in (1, 3, 5) and pb.error is None:
                q10, q50, q90 = pb.at(hm * 60)
                row.update(persistence_p10=_r(q10), persistence_p50=_r(q50), persistence_p90=_r(q90))
            i = int(np.argmin(np.abs(fc.horizons_s - hm * 60)))
            row.update(elevation_deg=_r(fc.el[i]), rain_db=_r(fc.rain[i]), trend_db=_r(fc.trend[i]))
            rows.append(row)
        return {"link_id": link_id, "clock": self.clock(), "model": fc.model,
                "los_iso": iso(fc.los_t) if fc.los_t else None, "bias_db": _r(fc.bias_db),
                "trend_db_per_min": _r(fc.trend_db_min), "horizons": rows,
                "note": "Persistence is the snr-baseline-v1 replica: last value carried forward, 1/3/5 min only."}

    def handovers(self, station_id: str | None = None) -> dict:
        out = self.output()
        rows = []
        for lid, p in out["plans"].items():
            if station_id and p.station_id != station_id:
                continue
            d = p.to_dict()
            d["los_iso"] = iso(p.los_t)
            d["command_iso"] = iso(p.command_t) if p.command_t else None
            d["los_in_min"] = round((p.los_t - self.t) / 60, 1)
            rows.append(d)
        div = [d.to_dict() for d in out.get("diversity", [])]
        return {"clock": self.clock(), "handover_plans": sorted(rows, key=lambda r: r["los_t"]), "rain_diversity": div}

    def pools(self) -> dict:
        out = self.output()
        rows = []
        for p in out["pools"].values():
            rows.append({"pool_id": p.pool_id, "stations": [self.ctx.fleet.stations[s].name for s in p.stations],
                         "demand_p50_mbps": _r(p.demand_p50_mbps), "demand_p99_mbps": _r(p.demand_p99_mbps),
                         "capacity_mbps_by_minute": {f"+{int(h // 60)}m": _r(c) for h, c in
                                                     zip(p.horizons_s, p.capacity_mbps) if h % 300 == 0},
                         "shortfall_from": iso(p.shortfall_t) if p.shortfall_t else None,
                         "shortfall_until": iso(p.shortfall_end_t) if p.shortfall_end_t else None})
        return {"clock": self.clock(), "pools": rows}

    def findings(self) -> dict:
        """What Fadewatch learned about the demo fleet itself."""
        al = [{"link_id": a.link_id, "satellite": self.ctx.fleet.satellites[a.sat_id].name,
               "replay_offset_days": round(a.offset_s / 86400, 4), "az_err_deg": round(a.az_err_deg, 3),
               "el_err_deg": round(a.el_err_deg, 3), "range_err_km": round(a.range_err_km, 2)}
              for a in self.ctx.alignment]
        cal = [{"station": self.ctx.fleet.stations[c.station_id].name, "loss_db": round(c.loss_db, 3),
                "zenith_gas_db": round(c.zenith_gas_db, 3), "rms_db": None if math.isnan(c.rms_db) else round(c.rms_db, 4),
                "n": c.n, "method": c.method} for c in self.ctx.cal.values()]
        return {"alignment": al, "calibration": cal}


def load_scorecard() -> dict | None:
    if os.path.exists(SCORECARD_PATH):
        return json.load(open(SCORECARD_PATH, encoding="utf-8"))
    return None


def _r(x, nd: int = 2):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) else round(x, nd)
