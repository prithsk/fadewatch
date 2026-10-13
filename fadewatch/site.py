"""Compile a scenario replay into one JSON bundle for the static dashboard."""
from __future__ import annotations

import json
import math
import os
import shutil
import time

import numpy as np

from .backtest import evaluate, pool_capacity_truth, summarize
from .linkbudget import CLOSURE_THRESHOLD_DB
from .orbit import iso
from .scenarios import SCENARIOS, load_context
from .simulate import Simulator
from .weather import Nowcast

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
H_MIN = [0.5, 1, 2, 3, 4, 5, 7.5, 10, 12.5, 15, 17.5, 20, 25, 30]


def _r(x, nd=2):
    if x is None:
        return None
    x = float(x)
    return None if (math.isnan(x) or math.isinf(x)) else round(x, nd)


def _arr(a, nd=2):
    return [_r(v, nd) for v in a]


def build_bundle(scenario: str = "demo", closed_loop: bool = True, log=print) -> dict:
    t_start = time.time()
    ctx = load_context()
    cfg = SCENARIOS[scenario](ctx.fleet)
    res = evaluate(ctx, cfg, closed_loop=closed_loop)
    rec, tlog, eng = res["_rec"], res["_log"], res["_engine"]
    fleet = ctx.fleet
    log(f"evaluated {scenario} in {time.time() - t_start:.0f}s")

    ticks = np.array(tlog.t)
    ks = [rec.k_at(t) for t in ticks]
    sat_ids = sorted(fleet.satellites)
    sat_idx = {s: i for i, s in enumerate(sat_ids)}
    ants = sorted(fleet.antennas)

    # Satellite tracks every 30 s across the replay (with replay offsets applied).
    ts = np.arange(ticks[0], ticks[-1] + 31, 30.0)
    tracks = {}
    for s in sat_ids:
        if s in ctx.prop:
            lat, lon, alt = ctx.prop.subpoint(s, ts)
            tracks[s] = [[_r(a, 3), _r(b, 3), _r(c, 0)] for a, b, c in zip(lat, lon, alt)]

    # Link series cover the whole recording, pre-roll included, so charts have history at the start.
    preroll = ks[0]
    kall = range(len(rec.t))
    ant_series = {}
    for lid in ants:
        a = rec.index(lid)
        ant_series[lid] = {
            "tgt": [sat_idx.get(rec.target[a, k], -1) if rec.mode[a, k] != "idle" else -1 for k in kall],
            "mode": "".join(rec.mode[a, k][0] for k in kall),
            "snr": _arr(rec.snr[a]), "el": _arr(rec.el[a], 2),
            "az": _arr(rec.az[a], 1), "rng": _arr(rec.rng[a], 0),
            "rain": _arr(rec.rain[a], 1), "clear": _arr(rec.clear[a]),
        }

    fc_out: dict[str, list] = {lid: [] for lid in ants}
    for out in tlog.outputs:
        for lid in ants:
            f = out["forecasts"].get(lid)
            if f is None or f.error or f.p50 is None:
                fc_out[lid].append(None)
                continue
            idx = [int(np.argmin(np.abs(f.horizons_s - h * 60))) for h in H_MIN]
            fc_out[lid].append({
                "p10": _arr(f.p10[idx], 1), "p50": _arr(f.p50[idx], 2), "p90": _arr(f.p90[idx], 1),
                "rain": _arr(f.rain[idx], 1), "el": _arr(f.el[idx], 1),
                "los": _r(f.los_t, 1), "bias": _r(f.bias_db), "trend": _r(f.trend_db_min, 3),
            })

    # Alerts as they stood at every tick, with repeated strings interned.
    strings: list[str] = []
    sidx: dict[str, int] = {}

    def intern(x):
        if x is None:
            return -1
        if x not in sidx:
            sidx[x] = len(strings)
            strings.append(x)
        return sidx[x]

    ALERT_FIELDS = ("id", "kind", "cause", "severity", "station_id", "link_id", "pool_id", "title", "detail", "action")
    alert_ticks = {"fields": list(ALERT_FIELDS) + ["raised_t", "event_t"], "strings": strings, "ticks": []}
    for alerts in tlog.alerts:
        alert_ticks["ticks"].append([[intern(a[k]) for k in ALERT_FIELDS] + [_r(a["raised_t"], 0), _r(a["event_t"], 0)]
                                     for a in alerts])

    caps_true = pool_capacity_truth(ctx, rec)
    pools = []
    for pid in sorted(caps_true):
        p50, p99 = eng.demand.demand(pid)
        pools.append({"id": pid, "p50": _r(p50, 1), "p99": _r(p99, 1),
                      "stations": [s.id for s in fleet.stations_in_pool(pid)],
                      "cap_true": _arr([caps_true[pid][k] for k in ks], 1),
                      "cap_fc": [_arr(o["pools"][pid].capacity_mbps[[int(np.argmin(np.abs(o["pools"][pid].horizons_s - h * 60))) for h in H_MIN]], 0)
                                 for o in tlog.outputs]})

    closed = None
    if closed_loop and "closed_loop" in res:
        from .backtest import FadewatchController
        ctl = FadewatchController(ctx, cfg)
        rec_cl = Simulator(ctx.fleet, ctx.prop, ctx.cal, cfg).run(controller=ctl)
        caps_cl = pool_capacity_truth(ctx, rec_cl)
        closed = {"stats": res["closed_loop"], "open_stats": res["open_loop"],
                  "commands": res["closed_loop_commands"],
                  "cap": {pid: _arr([caps_cl[pid][k] for k in ks], 1) for pid in caps_cl}}

    nowcast = Nowcast(cfg.rain)
    rain = {"freezing_km": cfg.rain.freezing_level_km, "cells": []}
    for i, c in enumerate(cfg.rain.cells):
        lat, lon = c.centre(ticks)
        rain["cells"].append({"name": c.name, "radius_km": c.radius_km,
                              "lat": _arr(lat, 4), "lon": _arr(lon, 4), "mmh": _arr(c.intensity(ticks), 1)})
    # What the nowcast extrapolates from each tick: positions at +10 and +20 min.
    rain["nowcast"] = []
    for t in ticks:
        row = []
        for c, amp in nowcast.field_at(float(t)):
            p10, p20 = c.centre(t + 600), c.centre(t + 1200)
            row.append([_r(p10[0], 4), _r(p10[1], 4), _r(p20[0], 4), _r(p20[1], 4), _r(amp, 1), c.radius_km])
        rain["nowcast"].append(row)

    scorecard = {"summary": summarize(res), "skill": res["skill"],
                 "alerts": {k: (v if k == "precision" else len(v)) for k, v in res["alerts"].items()},
                 "open_loop": res["open_loop"], "closed_loop": res.get("closed_loop"),
                 "events": [{k: e.get(k) for k in ("type", "iso", "t", "link_id", "pool_id", "fadewatch_lead_s",
                                                   "persistence_lead_s")} for e in res["events"]]}

    bundle = {
        "meta": {"scenario": cfg.name, "replay_start": iso(cfg.replay_start), "end": iso(ticks[-1]),
                 "step_s": 15, "generated": iso(time.time()), "closure_db": CLOSURE_THRESHOLD_DB,
                 "mask_deg": cfg.mask_deg, "H": H_MIN, "preroll": int(preroll),
                 "source": "Constellation demo fleet (CC BY 4.0)"},
        "t": [float(t) for t in ticks],
        "stations": [{"id": s.id, "name": s.name, "lat": s.lat_deg, "lon": s.lon_deg, "pool": s.pool_id,
                      "antennas": [a.id for a in fleet.antennas_at(s.id)]} for s in fleet.stations.values()],
        "satellites": [{"id": s, "name": fleet.satellites[s].name, "family": fleet.satellites[s].family}
                       for s in sat_ids],
        "antennas": [{"id": a.id, "station": a.station_id, "f": a.frequency_ghz, "bw": a.bandwidth_mhz,
                      "eirp": a.eirp_dbw, "gt": a.g_t_db_k} for a in (fleet.antennas[x] for x in ants)],
        "tracks": {"t0": float(ts[0]), "dt": 30.0, "pos": tracks},
        "ant": ant_series, "fc": fc_out, "alerts": alert_ticks, "pools": pools, "closed_loop": closed,
        "rain": rain, "scorecard": scorecard,
        "findings": {"alignment": [{"link_id": a.link_id, "sat": fleet.satellites[a.sat_id].name,
                                    "offset_days": round(a.offset_s / 86400, 4), "az_err": round(a.az_err_deg, 3),
                                    "el_err": round(a.el_err_deg, 3), "rng_err_km": round(a.range_err_km, 2)}
                                   for a in ctx.alignment],
                     "calibration": [{"station": fleet.stations[c.station_id].name, "loss_db": round(c.loss_db, 3),
                                      "zenith_gas_db": round(c.zenith_gas_db, 3),
                                      "rms_db": None if math.isnan(c.rms_db) else round(c.rms_db, 4), "n": c.n}
                                     for c in ctx.cal.values()]},
    }
    log(f"bundle built in {time.time() - t_start:.0f}s")
    return bundle, res


def export_site(out_dir: str, scenario: str = "demo", log=print) -> str:
    bundle, res = build_bundle(scenario, log=log)
    os.makedirs(os.path.join(out_dir, "data"), exist_ok=True)
    for name in os.listdir(WEB_DIR):
        src = os.path.join(WEB_DIR, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(out_dir, name))
        elif name != "data":
            shutil.copytree(src, os.path.join(out_dir, name), dirs_exist_ok=True)
    path = os.path.join(out_dir, "data", f"{scenario}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, separators=(",", ":"))
    log(f"wrote {path} ({os.path.getsize(path) / 1e6:.1f} MB)")
    return path
