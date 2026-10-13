"""Backtest: Fadewatch vs the persistence baseline (snr-baseline-v1 replica).

Three questions, answered on a scenario recording:

1. Forecast skill at Constellation's own horizons (1, 3, 5 min): MAE, RMSE,
   80 % interval coverage and width, and pinball loss, overall and per regime
   (steady, low elevation, rain, equipment, interference).
2. Early warning: for every event that actually happened (loss of signal,
   SNR falling below closure, a pool falling below demand) how long before it
   did each system raise a warning that stayed up until the event?
3. Closed loop: replaying the same scenario with Fadewatch's plans executed
   (make-before-break handovers on spare antennas, rain-diversity
   acquisitions), how much link outage and unserved pool traffic is avoided?

The persistence baseline gets the most generous reading possible: it "warns"
whenever its p10 at any of its horizons is below closure, and for pools when
the pool capacity implied by its p10 is below demand.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

import numpy as np

from .engine import DemandModel, Fadewatch
from .forecast import STD_HORIZONS_S, PersistenceBaseline, PhysicsForecaster
from .linkbudget import CLOSURE_THRESHOLD_DB, capacity_mbps
from .orbit import iso
from .scenarios import Context
from .simulate import Action, Recording, ScenarioConfig, Simulator
from .weather import Nowcast

REGIMES = ("all", "steady", "low elevation", "rain", "equipment", "interference")


@dataclass
class TickLog:
    """What the engine said at each tick (kept for the dashboard and lead times)."""

    t: list[float] = field(default_factory=list)
    alerts: list[list[dict]] = field(default_factory=list)
    outputs: list[dict] = field(default_factory=list)


def _regime(rec: Recording, a: int, k: int, fault_kinds: dict[str, str]) -> str:
    if rec.fault[a, k] > 0.5:
        return fault_kinds.get(rec.antennas[a], "equipment")
    if rec.rain[a, k] > 1.0:
        return "rain"
    if rec.el[a, k] < 15.0:
        return "low elevation"
    return "steady"


def pool_capacity_truth(ctx: Context, rec: Recording) -> dict[str, np.ndarray]:
    out = {}
    for pool_id in sorted({s.pool_id for s in ctx.fleet.stations.values()}):
        sids = {s.id for s in ctx.fleet.stations_in_pool(pool_id)}
        cap = np.zeros(len(rec.t))
        for a, lid in enumerate(rec.antennas):
            ant = ctx.fleet.antennas[lid]
            if ant.station_id in sids:
                cap += np.nan_to_num(capacity_mbps(rec.snr[a], ant.bandwidth_mhz))
        out[pool_id] = cap
    return out


def truth_events(ctx: Context, rec: Recording, demand: DemandModel) -> list[dict]:
    """Events that happened after the replay start, with the time they happened."""
    t0 = rec.config.replay_start
    ev = []
    for e in rec.events:
        if e["type"] == "los" and e["t"] > t0:
            ev.append({"type": "los", "t": e["t"], "link_id": e["link_id"], "target": e["target"]})
    for a, lid in enumerate(rec.antennas):
        s = rec.snr[a]
        ok = np.where(np.isnan(s), np.nan, (s >= CLOSURE_THRESHOLD_DB).astype(float))
        for k in range(1, len(rec.t)):
            if rec.t[k] <= t0 or ok[k] != 0.0:
                continue
            prior = ok[max(0, k - 8):k]
            if np.nansum(prior == 1.0) >= 8 and rec.target[a, k] == rec.target[a, k - 1]:
                ev.append({"type": "closure", "t": float(rec.t[k]), "link_id": lid, "target": rec.target[a, k]})
    for pool_id, cap in pool_capacity_truth(ctx, rec).items():
        _, p99 = demand.demand(pool_id)
        short = cap < p99
        for k in range(1, len(rec.t)):
            if rec.t[k] > t0 and short[k] and not short[max(0, k - 8):k].any():
                ev.append({"type": "pool", "t": float(rec.t[k]), "pool_id": pool_id})
    return sorted(ev, key=lambda e: e["t"])


def _alert_matches(alert: dict, e: dict) -> bool:
    if e["type"] == "los":
        return alert["kind"] == "LOS" and alert["link_id"] == e["link_id"]
    if e["type"] == "closure":
        return alert["link_id"] == e["link_id"] and alert["kind"] in ("FADE", "DEGRADATION", "INTERFERENCE")
    return alert["kind"] == "CAPACITY" and alert["pool_id"] == e["pool_id"]


def fadewatch_lead(log: TickLog, e: dict) -> float | None:
    """Seconds between the first tick of the alert episode covering `e` and `e`."""
    start = None
    for t, alerts in zip(log.t, log.alerts):
        if t >= e["t"]:
            break  # the warning must be up *before* the event, at the last tick preceding it
        if any(_alert_matches(a, e) for a in alerts):
            start = t if start is None else start
        else:
            start = None
    if start is None:
        return None
    return e["t"] - start


def persistence_lead(rec: Recording, ctx: Context, e: dict, demand: DemandModel,
                     base: PersistenceBaseline) -> float | None:
    t0 = rec.config.replay_start
    ticks = rec.t[(rec.t >= t0) & (rec.t < e["t"])]
    start = None
    for t in ticks:
        warn = False
        if e["type"] in ("los", "closure"):
            f = base.forecast(e["link_id"], rec.history(e["link_id"], t, 300), t)
            warn = f.error is None and bool(np.nanmin(f.p10) < CLOSURE_THRESHOLD_DB)
        else:
            sids = {s.id for s in ctx.fleet.stations_in_pool(e["pool_id"])}
            cap = 0.0
            for lid in rec.antennas:
                if ctx.fleet.antennas[lid].station_id not in sids:
                    continue
                f = base.forecast(lid, rec.history(lid, t, 300), t)
                if f.error is None:
                    cap += float(capacity_mbps(float(np.nanmin(f.p10)), ctx.fleet.antennas[lid].bandwidth_mhz))
            warn = cap < demand.demand(e["pool_id"])[1]
        if warn:
            start = t if start is None else start
        else:
            start = None
    return None if start is None else e["t"] - float(start)


def forecast_skill(ctx: Context, rec: Recording, log: TickLog, base: PersistenceBaseline) -> dict:
    """Score both models at 1/3/5 min wherever the link is still on the same satellite."""
    fault_kinds = {f.antenna_id: ("interference" if f.kind == "interference" else "equipment")
                   for f in rec.config.faults}
    acc: dict[tuple[str, str, int], list[tuple[float, float, float, float]]] = {}
    for t, out in zip(log.t, log.outputs):
        k0 = rec.k_at(t)
        for lid, fw in out["forecasts"].items():
            if fw.error or fw.p50 is None:
                continue
            a = rec.index(lid)
            pb = base.forecast(lid, rec.history(lid, t, 300), t)
            for h in STD_HORIZONS_S:
                k1 = rec.k_at(t + h)
                if abs(rec.t[k1] - (t + h)) > 1 or rec.mode[a, k1] != "track" or rec.target[a, k1] != rec.target[a, k0]:
                    continue
                y = float(rec.snr[a, k1])
                reg = _regime(rec, a, k1, fault_kinds)
                i = int(np.argmin(np.abs(fw.horizons_s - h)))
                for model, (lo, md, hi) in (("fadewatch", (fw.p10[i], fw.p50[i], fw.p90[i])),
                                            ("persistence", pb.at(h) if pb.error is None else (np.nan,) * 3)):
                    if np.isnan(md):
                        continue
                    for r in ("all", reg):
                        acc.setdefault((model, r, int(h // 60)), []).append((y, lo, md, hi))
    out = {}
    for (model, reg, hm), rows in acc.items():
        y, lo, md, hi = (np.array(c) for c in zip(*rows))
        err = md - y

        def pin(q, pred):
            d = y - pred
            return np.mean(np.maximum(q * d, (q - 1) * d))
        out.setdefault(model, {}).setdefault(reg, {})[f"{hm}min"] = {
            "n": int(len(y)), "mae_db": float(np.mean(np.abs(err))), "rmse_db": float(np.sqrt(np.mean(err**2))),
            "coverage_80": float(np.mean((y >= lo) & (y <= hi))), "width_80_db": float(np.mean(hi - lo)),
            "pinball_db": float((pin(0.1, lo) + pin(0.5, md) + pin(0.9, hi)) / 3),
        }
    return out


def run_engine(ctx: Context, cfg: ScenarioConfig, rec: Recording, every_s: float = 15.0) -> tuple[Fadewatch, TickLog]:
    fc = PhysicsForecaster(ctx.fleet, ctx.prop, ctx.cal, Nowcast(cfg.rain) if cfg.rain.cells else None)
    eng = Fadewatch(ctx.fleet, fc)
    log = TickLog()
    for t in rec.t:
        if t < cfg.replay_start or (t - cfg.replay_start) % every_s:
            continue
        out = eng.step(float(t), {l: rec.history(l, t, 900) for l in rec.antennas})
        log.t.append(float(t))
        log.alerts.append([a.to_dict() for a in out["alerts"]])
        log.outputs.append(out)
    return eng, log


class FadewatchController:
    """Executes the engine's plans inside the simulator (closed loop)."""

    def __init__(self, ctx: Context, cfg: ScenarioConfig):
        fc = PhysicsForecaster(ctx.fleet, ctx.prop, ctx.cal, Nowcast(cfg.rain) if cfg.rain.cells else None)
        self.engine = Fadewatch(ctx.fleet, fc)
        self.cfg = cfg
        self.issued: set = set()
        self.commands: list[dict] = []

    def __call__(self, t: float, rec: Recording) -> list[Action]:
        if t < self.cfg.replay_start:
            return []
        out = self.engine.step(t, {l: rec.history(l, t - 1e-6, 900) for l in rec.antennas})
        acts = []
        for lid, plan in out["plans"].items():
            if plan.mode != "make-before-break" or plan.command_t is None or plan.command_t > t + 7.5:
                continue
            key = ("mbb", plan.antenna, plan.successor)
            if key in self.issued:
                continue
            self.issued.add(key)
            acts += [Action(t, plan.antenna, plan.successor, f"make-before-break for {lid}"),
                     Action(t, lid, kind="release_at_los", reason="successor already up")]
            self.commands.append({"t": t, "type": "make-before-break", "antenna": plan.antenna,
                                  "target": plan.successor, "for": lid})
        for d in out["diversity"]:
            key = ("div", d.antenna, d.successor)
            if key in self.issued:
                continue
            self.issued.add(key)
            acts.append(Action(t, d.antenna, d.successor, "rain diversity"))
            self.commands.append({"t": t, "type": "rain diversity", "antenna": d.antenna, "target": d.successor})
        return acts


def verify_alerts(ctx: Context, rec: Recording, log: TickLog, demand: DemandModel) -> dict:
    """Did each warning's predicted condition actually happen?

    An alert episode is one (id, raised_t). It is *confirmed* if the truth shows
    its condition between raising and 30 min after it was last shown,
    *unverifiable* if it predicted something past the end of the recording,
    and otherwise *false*. Info-level handover notices are not warnings.
    """
    episodes: dict[str, dict] = {}
    last_seen: dict[str, float] = {}
    for t, alerts in zip(log.t, log.alerts):
        for a in alerts:
            key = f"{a['id']}@{a['raised_t']}"
            episodes.setdefault(key, a)
            last_seen[key] = t
    caps = pool_capacity_truth(ctx, rec)
    interference = {f.antenna_id for f in rec.config.faults if f.kind == "interference"}
    out = {"confirmed": [], "false": [], "unverifiable": []}
    for key, a in episodes.items():
        if a["severity"] == "info":
            continue
        t1, t2 = a["raised_t"], last_seen[key] + 1800
        win = (rec.t >= t1 - 60) & (rec.t <= t2)
        if a.get("event_t") and a["event_t"] > rec.t[-1]:
            out["unverifiable"].append(a["id"])
            continue
        ok = False
        if a["link_id"]:
            i = rec.index(a["link_id"])
            snr, rain, fault = rec.snr[i][win], rec.rain[i][win], rec.fault[i][win]
            if a["kind"] == "LOS":
                ok = any(e["type"] == "los" and e["link_id"] == a["link_id"] and t1 - 60 <= e["t"] <= t2
                         for e in rec.events)
            elif a["kind"] in ("FADE", "NO_CLOSURE"):
                ok = bool(np.nanmin(np.append(snr, np.inf)) < CLOSURE_THRESHOLD_DB
                          or np.nanmax(np.append(rain, 0)) >= 3.0 or np.nanmax(np.append(fault, 0)) >= 3.0)
            elif a["kind"] == "DEGRADATION":
                ok = bool(np.nanmax(np.append(fault, 0)) > 0.5) and a["link_id"] not in interference
            elif a["kind"] == "INTERFERENCE":
                ok = a["link_id"] in interference and bool(np.nanmax(np.append(fault, 0)) > 3.0)
        elif a["kind"] == "CAPACITY":
            ok = bool((caps[a["pool_id"]][win] < demand.demand(a["pool_id"])[1]).any())
        (out["confirmed"] if ok else out["false"]).append(a["id"])
    n = len(out["confirmed"]) + len(out["false"])
    out["precision"] = len(out["confirmed"]) / n if n else None
    return out


def handover_gaps(rec: Recording, ctx: Context) -> list[dict]:
    """Seconds each station spent short of a link at every loss of signal.

    Gap = time from LOS until the next acquisition at that station, or zero
    if a successor was acquired in the 5 minutes before the LOS
    (make-before-break). Capped at 5 minutes.
    """
    out = []
    t0 = rec.config.replay_start
    for e in rec.events:
        if e["type"] != "los" or e["t"] <= t0:
            continue
        sid = ctx.fleet.antennas[e["link_id"]].station_id
        aos = [x["t"] for x in rec.events if x["type"] == "aos" and
               ctx.fleet.antennas[x["link_id"]].station_id == sid]
        mbb = any(x["type"] == "command" and e["link_id"] in x.get("reason", "") and e["t"] - 600 <= x["t"] <= e["t"]
                  for x in rec.events)
        after = [t for t in aos if t >= e["t"]]
        gap = 0.0 if mbb else min((after[0] - e["t"]) if after else 300.0, 300.0)
        out.append({"t": e["t"], "link_id": e["link_id"], "gap_s": gap, "make_before_break": mbb})
    return out


def outage_stats(ctx: Context, rec: Recording, demand: DemandModel) -> dict:
    t0 = rec.config.replay_start
    win = rec.t >= t0
    dt = rec.config.step_s
    hg = handover_gaps(rec, ctx)
    gaps = {"handovers": len(hg), "handover_gap_s": float(sum(g["gap_s"] for g in hg)),
            "make_before_break": sum(1 for g in hg if g["make_before_break"]),
            "link_hours_closed": 0.0}
    for a, lid in enumerate(rec.antennas):
        m, s = rec.mode[a][win], rec.snr[a][win]
        gaps["link_hours_closed"] += float(np.sum((m == "track") & (s >= CLOSURE_THRESHOLD_DB))) * dt / 3600
    pools = {}
    for pool_id, cap in pool_capacity_truth(ctx, rec).items():
        p50, p99 = demand.demand(pool_id)
        c = cap[win]
        pools[pool_id] = {
            "unserved_gbit": float(np.sum(np.maximum(p50 - c, 0)) * dt / 1000.0),
            "minutes_below_p50": float(np.sum(c < p50)) * dt / 60.0,
            "minutes_below_p99": float(np.sum(c < p99)) * dt / 60.0,
        }
    return {"links": gaps, "pools": pools}


def evaluate(ctx: Context, cfg: ScenarioConfig, closed_loop: bool = True) -> dict:
    sim = Simulator(ctx.fleet, ctx.prop, ctx.cal, cfg)
    rec = sim.run()
    eng, log = run_engine(ctx, cfg, rec)
    demand = eng.demand
    base = PersistenceBaseline()
    events = truth_events(ctx, rec, demand)
    rows = []
    for e in events:
        rows.append({**e, "iso": iso(e["t"]), "fadewatch_lead_s": fadewatch_lead(log, e),
                     "persistence_lead_s": persistence_lead(rec, ctx, e, demand, base)})
    skill = forecast_skill(ctx, rec, log, base)
    verified = verify_alerts(ctx, rec, log, demand)
    result = {
        "scenario": cfg.name, "replay_start": iso(cfg.replay_start), "end": iso(cfg.end),
        "events": rows, "skill": skill, "alerts": verified,
        "open_loop": outage_stats(ctx, rec, demand),
    }
    if closed_loop:
        ctl = FadewatchController(ctx, cfg)
        rec_cl = Simulator(ctx.fleet, ctx.prop, ctx.cal, cfg).run(controller=ctl)
        result["closed_loop"] = outage_stats(ctx, rec_cl, demand)
        result["closed_loop_commands"] = [{**c, "iso": iso(c["t"])} for c in ctl.commands]
    result["_rec"], result["_log"], result["_engine"] = rec, log, eng
    return result


def summarize(res: dict) -> dict:
    """Headline numbers for the README and the dashboard."""
    ev = res["events"]
    out = {}
    for typ in ("los", "closure", "pool"):
        rows = [e for e in ev if e["type"] == typ]
        if not rows:
            continue
        fw = [e["fadewatch_lead_s"] for e in rows]
        pb = [e["persistence_lead_s"] for e in rows]
        out[typ] = {
            "events": len(rows),
            "fadewatch_warned_60s": sum(1 for x in fw if x is not None and x >= 60),
            "persistence_warned_60s": sum(1 for x in pb if x is not None and x >= 60),
            "fadewatch_median_lead_min": float(np.median([x or 0 for x in fw])) / 60,
            "persistence_median_lead_min": float(np.median([x or 0 for x in pb])) / 60,
        }
    return out


def to_json(res: dict) -> str:
    clean = {k: v for k, v in res.items() if not k.startswith("_")}
    return json.dumps(clean, indent=2, default=lambda o: None if (isinstance(o, float) and math.isnan(o)) else str(o))
