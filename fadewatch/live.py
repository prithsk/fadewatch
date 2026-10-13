"""Live mode: run Fadewatch on a real Constellation account.

    load_demo_fleet (MCP)  ->  GET /topology  ->  GET /topology/series
          -> infer each sample's satellite from geometry
          -> physics forecasts, alerts, plans
          -> POST /telemetry: `attenuation` per link, `telemetry` forecasts,
             `satellite` TLEs (so the console can propagate the orbits)

`/topology/series` returns numeric channels only, so a link's `target` tag is
not in the history. Fadewatch recovers it from the pointing: at each sample
it picks the satellite whose SGP4 look angles match the antenna's reported
azimuth/elevation (handovers show up as a change of best match).
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone

import numpy as np

from .align import align_fleet
from .constellation import Constellation, ConstellationMCP
from .engine import Fadewatch
from .fleet import DEMO_TLE, Fleet, LinkSample
from .forecast import STD_HORIZONS_S, PersistenceBaseline, PhysicsForecaster
from .linkbudget import calibrate
from .orbit import Propagator, iso, load_tle_file, to_unix


def infer_targets(prop: Propagator, fleet: Fleet, link_id: str, t: np.ndarray, az: np.ndarray, el: np.ndarray,
                  tol_deg: float = 1.5) -> list[str]:
    st = fleet.stations[fleet.antennas[link_id].station_id]
    fam = fleet.station_family(st.id)
    cands = [s for s, sat in fleet.satellites.items() if s in prop and (sat.family == fam or fam == "mixed")]
    best = np.full(len(t), np.inf)
    who = np.full(len(t), "", dtype=object)
    for s in cands:
        lk = prop.look(s, st.lat_deg, st.lon_deg, t)
        d = np.hypot(((lk.az_deg - az + 180) % 360 - 180) * np.cos(np.radians(el)), lk.el_deg - el)
        d = np.nan_to_num(d, nan=np.inf)
        better = d < best
        best[better], who[better] = d[better], s
    return [w if b <= tol_deg else "" for w, b in zip(who, best)]


def fetch_histories(api: Constellation, fleet: Fleet, prop: Propagator, start: float, end: float) -> dict[str, list[LinkSample]]:
    ids = sorted(fleet.antennas)
    out: dict[str, list[LinkSample]] = {}
    fields = ["snr_db", "elevation_deg", "azimuth_deg", "slant_range_km"]
    for i in range(0, len(ids), 16):
        chunk = ids[i:i + 16]
        span = end - start
        page = api.series(chunk, iso(start), iso(end), "link", fields, interval=None if span <= 3600 else "15s",
                          agg=None if span <= 3600 else "last")
        for s in page["series"]:
            lid = s["entity_id"]
            ch = s["channels"]
            t = np.array([to_unix(x) for x in s["times"]], dtype=float)
            snr, el, az, rg = (np.array([np.nan if v is None else v for v in ch.get(k, [None] * len(t))], dtype=float)
                               for k in fields)
            ok = ~(np.isnan(snr) | np.isnan(el) | np.isnan(az))
            t, snr, el, az, rg = t[ok], snr[ok], el[ok], az[ok], rg[ok]
            targets = infer_targets(prop, fleet, lid, t, az, el) if len(t) else []
            fallback = fleet.latest[lid].target if lid in fleet.latest else ""
            out[lid] = [LinkSample(lid, tg or fallback, float(a), float(b), float(c), float(d), float(e))
                        for a, b, c, d, e, tg in zip(t, az, el, rg, snr, targets)]
    return out


def history_at(hist: dict[str, list[LinkSample]], t: float, window_s: float = 900) -> dict[str, list[LinkSample]]:
    return {k: [s for s in v if t - window_s <= s.t <= t] for k, v in hist.items()}


def skill_on_recorded(fleet: Fleet, prop: Propagator, cal, hist: dict[str, list[LinkSample]],
                      every_s: float = 60.0) -> dict:
    """1/3/5-min skill of both models on real recorded telemetry (no quota spent)."""
    fc = PhysicsForecaster(fleet, prop, cal)
    base = PersistenceBaseline()
    t_all = sorted({s.t for v in hist.values() for s in v})
    if not t_all:
        return {}
    acc: dict[tuple[str, int], list[tuple[float, float, float, float]]] = {}
    for t in np.arange(t_all[0] + 300, t_all[-1] - 300, every_s):
        for lid, samples in hist.items():
            past = [s for s in samples if t - 900 <= s.t <= t]
            if not past or t - past[-1].t > 45:
                continue
            fw = fc.forecast(lid, past, t, STD_HORIZONS_S)
            pb = base.forecast(lid, past, t, STD_HORIZONS_S)
            for j, h in enumerate(STD_HORIZONS_S):
                fut = [s for s in samples if abs(s.t - (t + h)) <= 7.5 and s.target == past[-1].target]
                if not fut:
                    continue
                y = fut[0].snr_db
                for m, f in (("fadewatch", fw), ("persistence", pb)):
                    if f.error is None and not math.isnan(f.p50[j]):
                        acc.setdefault((m, int(h // 60)), []).append((y, f.p10[j], f.p50[j], f.p90[j]))
    out: dict = {}
    for (m, hm), rows in acc.items():
        y, lo, md, hi = (np.array(c) for c in zip(*rows))
        out.setdefault(m, {})[f"{hm}min"] = {"n": len(y), "mae_db": float(np.mean(np.abs(md - y))),
                                             "coverage_80": float(np.mean((y >= lo) & (y <= hi)))}
    return out


def compare_with_served_baseline(api: Constellation, fleet: Fleet, hist: dict[str, list[LinkSample]],
                                 link_ids: list[str]) -> dict:
    """Spend ONE prediction call to check the replica against snr-baseline-v1 itself."""
    resp = api.predictions(link_ids, "snr")
    base = PersistenceBaseline()
    rows = []
    for item in resp.get("items", []):
        lid, h = item["link_id"], float(item["horizon_minutes"]) * 60
        t = to_unix(item["predicted_at"])
        past = [s for s in hist.get(lid, []) if s.t <= t]
        rep = base.forecast(lid, past, t, np.array([h]))
        v = item["value"]
        rows.append({"link_id": lid, "horizon_min": item["horizon_minutes"], "served_p50": v.get("snr_db_p50"),
                     "replica_p50": None if rep.error else round(float(rep.p50[0]), 3),
                     "served_p10": v.get("snr_db_p10"), "replica_p10": None if rep.error else round(float(rep.p10[0]), 3),
                     "model_version": item.get("model_version")})
    return {"items": rows, "entity_errors": resp.get("entity_errors", []), "model_release_id": resp.get("model_release_id")}


def writeback_records(fleet: Fleet, prop: Propagator, out: dict, now: float) -> list[dict]:
    """Fadewatch's view as Constellation telemetry (canonical measurement families)."""
    ts = iso(now)
    recs: list[dict] = []
    for lid, f in out["forecasts"].items():
        if f.error or f.p50 is None:
            continue
        fields = {"source": "fadewatch", "snr_expected_db": round(float(f.clear[0]) - float(f.rain[0]), 3),
                  "rain_db": round(float(f.rain[0]), 3), "bias_db": round(f.bias_db, 3),
                  "trend_db_per_min": round(f.trend_db_min, 4)}
        for hm in (1, 3, 5, 15, 30):
            p10, p50, p90 = f.at(hm * 60)
            if not math.isnan(p50):
                fields[f"snr_p50_{hm}m"] = round(p50, 3)
                fields[f"snr_p10_{hm}m"] = round(p10, 3)
        if f.los_t is not None:
            fields["los_in_s"] = round(f.los_t - now, 1)
        recs.append({"measurement": "attenuation", "time": ts, "tags": {"entity_id": lid, "producer": "fadewatch"},
                     "fields": {"rain_db": fields["rain_db"], "source": "fadewatch"}})
        recs.append({"measurement": "telemetry", "time": ts,
                     "tags": {"entity_id": f"fadewatch-{lid}", "link_id": lid, "producer": "fadewatch"}, "fields": fields})
    for a in out["alerts"]:
        recs.append({"measurement": "telemetry", "time": ts,
                     "tags": {"entity_id": f"fadewatch-alert-{a.kind.lower()}-{(a.link_id or a.pool_id or '').replace(':', '-')}",
                              "producer": "fadewatch", "kind": a.kind, "severity": a.severity},
                     "fields": {"title": a.title, "action": a.action[:500],
                                "event_in_s": round(a.event_t - now, 1) if a.event_t else -1.0,
                                "lead_s": round(now - a.raised_t, 1)}})
    for sid, sat in fleet.satellites.items():
        if sid in prop and sid in prop.lines:
            l1, l2 = prop.lines[sid]
            recs.append({"measurement": "satellite", "time": ts,
                         "tags": {"entity_id": sid, "entity_type": "satellite", "observed_by": "fadewatch"},
                         "fields": {"name": sat.name, "norad_id": sid.split("-")[1], "tle_line1": l1, "tle_line2": l2}})
    return recs


def run_live(api_key: str | None = None, load_demo: bool = True, spend_prediction: bool = False,
             write_back: bool = True, log=print) -> dict:
    api = Constellation(api_key)
    report: dict = {"started": iso(time.time())}
    if load_demo:
        mcp = ConstellationMCP(api.api_key)
        report["mcp_tools"] = mcp.tools()
        log(f"MCP tools: {report['mcp_tools']}")
        report["load_demo_fleet"] = mcp.call("load_demo_fleet")
        log("load_demo_fleet: done")
        report["usage"] = mcp.call("get_usage")
    topo = api.topology(freshness_seconds=900)
    fleet = Fleet.from_topology(topo, source="constellation-api")
    if not fleet.stations:
        raise RuntimeError("Topology is empty: load the demo fleet first (MCP load_demo_fleet).")
    prop = Propagator(load_tle_file(DEMO_TLE))
    report["alignment"] = [a.__dict__ for a in align_fleet(prop, fleet)]
    cal = calibrate(fleet)
    now = time.time()
    hist = fetch_histories(api, fleet, prop, now - 2 * 3600, now)
    report["samples"] = {k: len(v) for k, v in hist.items()}
    log(f"series: {sum(report['samples'].values())} samples over {len(hist)} links")
    report["skill_recorded"] = skill_on_recorded(fleet, prop, cal, hist)
    eng = Fadewatch(fleet, PhysicsForecaster(fleet, prop, cal))
    out = eng.step(now, history_at(hist, now))
    report["alerts"] = [a.to_dict() for a in out["alerts"]]
    if spend_prediction:
        live_ids = [lid for lid, v in hist.items() if v and now - v[-1].t < 120][:20]
        report["served_baseline"] = compare_with_served_baseline(api, fleet, hist, live_ids)
    if write_back:
        recs = writeback_records(fleet, prop, out, now)
        report["writeback"] = api.send_telemetry(recs)
        log(f"wrote {len(recs)} records back to Constellation")
    report["fleet"] = {"stations": len(fleet.stations), "links": len(fleet.antennas), "satellites": len(fleet.satellites)}
    return report


def stream_scenario(rec, fleet: Fleet, api_key: str | None = None, speed: float = 1.0, prefix: str = "fwsim-",
                    log=print) -> None:
    """Replay a scenario recording into Constellation in real time (time-shifted to now)
    so Constellation's own forecasts can be compared live against Fadewatch."""
    api = Constellation(api_key)
    shift = time.time() - rec.config.replay_start
    t_sim = rec.config.replay_start - 300   # 5 minutes of history first, so the baseline has a window
    recs = rec.telemetry_records(fleet, t_sim, rec.config.replay_start, shift, prefix)
    api.send_telemetry(recs)
    t_sim = rec.config.replay_start
    while t_sim < rec.config.end:
        nxt = t_sim + rec.config.step_s
        recs = rec.telemetry_records(fleet, t_sim, nxt, shift + (t_sim - rec.config.replay_start) * (1 / speed - 1), prefix)
        if recs:
            api.send_telemetry(recs)
        log(f"{iso(t_sim)} sim -> {datetime.now(timezone.utc):%H:%M:%S} wall: {len(recs)} records")
        t_sim = nxt
        time.sleep(rec.config.step_s / speed)
