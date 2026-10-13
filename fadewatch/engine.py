"""The watch engine: forecasts -> alerts -> plans, once per telemetry tick.

Each tick it:
  1. forecasts every live link 30 s .. 30 min ahead (physics),
  2. raises alerts for predicted loss of signal (geometry), predicted fades
     below closure or deep rain fades, equipment degradation and abrupt
     interference, and links that do not close,
  3. plans handovers: the best successor satellite and, where the station
     has an idle antenna, a make-before-break command time,
  4. projects pool capacity (ACM throughput of the pool's links) against
     the pool's demand and raises a capacity alert ahead of a shortfall.

Alerts keep a stable id across ticks, so `raised_t` is the moment the agent
first saw the event coming; the backtest measures lead time from it.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np

from .fleet import Fleet, LinkSample
from .forecast import DENSE_HORIZONS_S, AntennaMonitor, Forecast, PhysicsForecaster
from .linkbudget import CLOSURE_THRESHOLD_DB, capacity_mbps, modcod_for
from .orbit import iso

SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}


@dataclass
class Alert:
    id: str
    kind: str                 # LOS | FADE | DEGRADATION | INTERFERENCE | NO_CLOSURE | CAPACITY
    cause: str                # geometry | rain | equipment | interference | link budget | capacity
    severity: str
    station_id: str
    link_id: str | None
    pool_id: str | None
    raised_t: float
    updated_t: float
    event_t: float | None
    title: str
    detail: str
    action: str
    evidence: dict = field(default_factory=dict)
    resolved_t: float | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["event_iso"] = iso(self.event_t) if self.event_t else None
        d["raised_iso"] = iso(self.raised_t)
        return d


@dataclass
class HandoverPlan:
    link_id: str
    station_id: str
    from_sat: str
    los_t: float
    successor: str | None
    successor_name: str | None
    successor_up_until: float | None
    successor_max_el: float | None
    antenna: str | None          # who acquires the successor
    command_t: float | None
    mode: str                    # make-before-break | break-before-make | none
    gap_s: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DiversityPlan:
    antenna: str
    station_id: str
    successor: str
    successor_name: str
    command_t: float
    min_snr_db: float
    min_capacity_mbps: float
    mean_az_deg: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PoolOutlook:
    pool_id: str
    stations: list[str]
    horizons_s: np.ndarray
    capacity_mbps: np.ndarray
    demand_p50_mbps: float
    demand_p99_mbps: float
    shortfall_t: float | None
    shortfall_end_t: float | None
    min_capacity_mbps: float


class DemandModel:
    """Per-pool offered load. Defaults to the snapshot's latest interval with a
    p99 headroom; swap in Constellation `model_family=demand` forecasts live."""

    def __init__(self, fleet: Fleet, p99_ratio: float = 1.3):
        self.p50 = {p.id: p.offered_mbps for p in fleet.pools.values()}
        self.p99_ratio = p99_ratio
        self.source = {p: "snapshot" for p in self.p50}

    def set_forecast(self, pool_id: str, p50_mbps: float, p99_mbps: float, source: str) -> None:
        self.p50[pool_id] = p50_mbps
        self.p99_ratio_pool = getattr(self, "p99_ratio_pool", {})
        self.p99_ratio_pool[pool_id] = p99_mbps / max(p50_mbps, 1e-9)
        self.source[pool_id] = source

    def demand(self, pool_id: str) -> tuple[float, float]:
        p50 = self.p50.get(pool_id, 0.0)
        ratio = getattr(self, "p99_ratio_pool", {}).get(pool_id, self.p99_ratio)
        return p50, p50 * ratio


class Fadewatch:
    def __init__(self, fleet: Fleet, forecaster: PhysicsForecaster, demand: DemandModel | None = None,
                 mask_deg: float = 10.0, slew_s: float = 75.0, horizon_s: float = 1800.0,
                 fade_alert_db: float = 3.0, spare_after_s: float = 600.0):
        self.fleet, self.fc = fleet, forecaster
        self.prop = forecaster.prop
        self.demand = demand or DemandModel(fleet)
        self.mask, self.slew_s, self.horizon_s = mask_deg, slew_s, horizon_s
        self.fade_alert_db = fade_alert_db
        self.spare_after_s = spare_after_s
        self.monitors: dict[str, AntennaMonitor] = {}
        self.active: dict[str, Alert] = {}
        self.resolved: list[Alert] = []
        self._vis_cache: dict = {}

    # ---------------------------------------------------------------- planner
    def _visibility(self, station_id: str, t0: float) -> dict[str, tuple[np.ndarray, np.ndarray]]:
        """Elevation of every family satellite over the next 3 h (60 s grid), cached per 5 min."""
        key = (station_id, int(t0 // 300))
        if key in self._vis_cache:
            return self._vis_cache[key]
        st = self.fleet.stations[station_id]
        fam = self.fleet.station_family(station_id)
        tg = np.arange(int(t0 // 300) * 300, int(t0 // 300) * 300 + 3 * 3600, 60.0)
        out = {}
        for sid, sat in self.fleet.satellites.items():
            if sid in self.prop and (sat.family == fam or fam == "mixed"):
                out[sid] = (tg, self.prop.look(sid, st.lat_deg, st.lon_deg, tg).el_deg)
        if len(self._vis_cache) > 500:
            self._vis_cache.clear()
        self._vis_cache[key] = out
        return out

    def plan_handover(self, link_id: str, from_sat: str, los_t: float, busy: set[str], spares: list[str],
                      now: float) -> HandoverPlan:
        sid = self.fleet.antennas[link_id].station_id
        vis = self._visibility(sid, now)
        best, best_end, best_max = None, -math.inf, None
        for sat, (tg, el) in vis.items():
            if sat in busy or sat == from_sat:
                continue
            # usable from the moment it is needed (LOS, or a spare's acquisition)
            k = int(np.searchsorted(tg, los_t))
            if k >= len(tg) or np.nan_to_num(el[k], nan=-90) < self.mask + 0.5:
                continue
            below = np.nonzero(np.nan_to_num(el[k:], nan=-90) < self.mask)[0]
            end = tg[k + below[0]] if below.size else tg[-1]
            if end - los_t >= 600 and end > best_end:
                best, best_end, best_max = sat, float(end), float(np.nanmax(el[k:k + (below[0] if below.size else len(el) - k)]))
        name = self.fleet.satellites[best].name if best else None
        if best is None:
            return HandoverPlan(link_id, sid, from_sat, los_t, None, None, None, None, None, None, "none", math.inf)
        if spares:
            # Command the spare early enough to be locked before the old link drops,
            # but no earlier than the successor clears the mask.
            tg, el = vis[best]
            up = tg[np.nonzero(np.nan_to_num(el, nan=-90) >= self.mask + 0.5)[0]]
            first_up = float(up[up >= now - 60][0]) if (up >= now - 60).any() else los_t
            cmd = max(los_t - self.slew_s - 90.0, first_up, now)
            gap = max(0.0, cmd + self.slew_s - los_t)
            return HandoverPlan(link_id, sid, from_sat, los_t, best, name, best_end, best_max, spares[0], cmd,
                                "make-before-break", gap)
        return HandoverPlan(link_id, sid, from_sat, los_t, best, name, best_end, best_max, link_id, los_t,
                            "break-before-make", self.slew_s)

    def plan_diversity(self, station_id: str, spare: str, busy: set[str], now: float) -> DiversityPlan | None:
        """Pick the satellite whose slant path the nowcast keeps clearest for the
        next 30 min, for a spare antenna to add capacity through a storm."""
        vis = self._visibility(station_id, now)
        tf = now + self.slew_s + np.arange(0.0, 1800.0, 120.0)
        best = None
        for sat, (tg, el) in vis.items():
            if sat in busy:
                continue
            k = int(np.searchsorted(tg, tf[0]))
            if k >= len(tg) or np.nan_to_num(el[k], nan=-90) < self.mask + 3:
                continue
            ex = self.fc.expected(spare, sat, tf, now)
            if (ex["el"] < self.mask).any():
                continue  # must stay up through the window
            snr = ex["clear"] - ex["rain"]
            cap = capacity_mbps(snr, self.fleet.antennas[spare].bandwidth_mhz)
            score = float(np.min(cap)) + 0.01 * float(np.mean(cap))
            if best is None or score > best[0]:
                best = (score, sat, float(np.min(snr)), float(np.min(cap)), float(np.mean(ex["az"])))
        if best is None or best[3] <= 0:
            return None
        _, sat, min_snr, min_cap, az = best
        return DiversityPlan(spare, station_id, sat, self.fleet.satellites[sat].name, now, min_snr, min_cap, az)

    # ------------------------------------------------------------- alerting
    def _raise(self, now: float, seen: set[str], **kw) -> Alert:
        aid = kw["id"]
        seen.add(aid)
        if aid in self.active:
            a = self.active[aid]
            sev = a.severity
            for k, v in kw.items():
                if k not in ("id", "raised_t"):
                    setattr(a, k, v)
            a.updated_t = now
            # Severity only escalates within one alert's life.
            if SEVERITY_RANK[sev] < SEVERITY_RANK[a.severity]:
                a.severity = sev
            return a
        a = Alert(raised_t=now, updated_t=now, **kw)
        self.active[aid] = a
        return a

    @staticmethod
    def _attribute(f: Forecast, i: int) -> tuple[str, dict]:
        """Which physical term drives the drop between now and horizon index i?"""
        d_geo = float(f.clear[i] - f.clear[0])
        d_rain = -float(f.rain[i])                     # total rain loss vs clear sky
        d_eq = float(f.trend[i]) + min(f.bias_db, 0.0)
        parts = {"geometry": d_geo, "rain": d_rain, "equipment": d_eq}
        return min(parts, key=parts.get), {k: round(v, 2) for k, v in parts.items()}

    def step(self, now: float, histories: dict[str, list[LinkSample]]) -> dict:
        seen: set[str] = set()
        forecasts: dict[str, Forecast] = {}
        live: dict[str, LinkSample] = {}
        for lid, hist in histories.items():
            if hist and now - hist[-1].t <= 45:   # three missed 15 s samples = not live
                live[lid] = hist[-1]
        spares_by_station: dict[str, list[str]] = {}
        busy_by_station: dict[str, set[str]] = {}
        for lid, ant in sorted(self.fleet.antennas.items()):
            hist = histories.get(lid) or []
            if lid in live:
                busy_by_station.setdefault(ant.station_id, set()).add(live[lid].target)
            elif not hist or now - hist[-1].t > self.spare_after_s:
                # Quiet for 10 min: idle, not mid-handover. Free to command.
                spares_by_station.setdefault(ant.station_id, []).append(lid)

        plans: dict[str, HandoverPlan] = {}
        los_alerts: list[tuple[Alert, HandoverPlan]] = []
        claimed_spares: set[str] = set()
        for lid, last in sorted(live.items()):
            mon = self.monitors.setdefault(lid, AntennaMonitor(lid))
            f = self.fc.forecast(lid, histories[lid], now, DENSE_HORIZONS_S, allow_trend=mon.trend_allowed(now),
                                drift_confirmed=mon.alarm_t is not None and mon.step_t is None)
            forecasts[lid] = f
            if f.error:
                continue
            ant = self.fleet.antennas[lid]
            sid = ant.station_id
            st = self.fleet.stations[sid]
            sat = self.fleet.satellites.get(last.target)
            sat_name = sat.name if sat else last.target
            short = lid.split("__")[1]

            # Equipment / interference monitor on the physics residual.
            th, rr = self.fc.residuals(lid, [s for s in histories[lid] if s.target == last.target][-24:])
            rain_now = float(f.rain[0]) if f.rain is not None else 0.0
            mon.update(th, rr, rain_now, f.resid_sigma)
            deficit = (mon.ref - float(np.mean(rr[-4:]))) if (mon.ref is not None and rr.size) else 0.0

            # 0. Link does not close now: say why.
            closed = last.snr_db >= CLOSURE_THRESHOLD_DB
            if not closed:
                if rain_now > 3.0:
                    why, cause = f"rain on the path ({rain_now:.0f} dB nowcast)", "rain"
                elif deficit > 3.0:
                    why, cause = f"an equipment fault ({deficit:.0f} dB below its physics expectation)", "equipment"
                else:
                    why, cause = (f"the link budget itself: physics expects {float(f.clear[0]):.1f} dB at "
                                  f"{last.range_km:,.0f} km slant range"), "link budget"
                self._raise(now, seen, id=f"NO_CLOSURE:{lid}:{last.target}", kind="NO_CLOSURE", cause=cause,
                            severity="warning" if cause == "link budget" else "critical",
                            station_id=sid, link_id=lid, pool_id=st.pool_id, event_t=None,
                            title=f"{st.name} {short} → {sat_name} does not close",
                            detail=f"SNR {last.snr_db:.1f} dB vs {CLOSURE_THRESHOLD_DB:.1f} dB closure. Cause: {why}.",
                            action="Release the antenna for a closing pass." if cause == "link budget"
                            else "Carry the pool's traffic elsewhere until the link recovers.",
                            evidence={"snr_db": round(last.snr_db, 2), "range_km": round(last.range_km),
                                      "rain_db": round(rain_now, 1), "deficit_db": round(deficit, 1)})

            # 1. Predicted loss of signal, with a handover plan.
            if f.los_t is not None and f.los_t - now <= self.horizon_s:
                spares = [s for s in spares_by_station.get(sid, []) if s not in claimed_spares]
                plan = self.plan_handover(lid, last.target, f.los_t, busy_by_station.get(sid, set()), spares, now)
                if plan.antenna and plan.mode == "make-before-break":
                    claimed_spares.add(plan.antenna)
                if plan.successor:
                    busy_by_station.setdefault(sid, set()).add(plan.successor)
                plans[lid] = plan
                if plan.mode == "make-before-break":
                    sev = "info"
                    act = (f"Command spare {plan.antenna.split('__')[1]} to acquire {plan.successor_name} at "
                           f"{iso(plan.command_t)[11:19]}Z: make-before-break, no gap.")
                elif plan.successor:
                    sev = "warning"
                    act = (f"No spare antenna: {short} slews to {plan.successor_name} after LOS, "
                           f"~{plan.gap_s:.0f} s without this link.")
                else:
                    sev = "warning"
                    act = "No successor stays above the mask for 10 min: release the antenna."
                a = self._raise(now, seen, id=f"LOS:{lid}:{last.target}", kind="LOS", cause="geometry", severity=sev,
                                station_id=sid, link_id=lid, pool_id=st.pool_id, event_t=f.los_t,
                                title=f"{st.name} {short} → {sat_name} sets below {self.mask:.0f}°",
                                detail=f"Elevation {last.el_deg:.1f}° and falling; SGP4 puts LOS at "
                                       f"{iso(f.los_t)[11:19]}Z. A persistence forecast keeps predicting "
                                       f"{last.snr_db:.1f} dB until the samples stop.",
                                action=act, evidence={"el_deg": round(last.el_deg, 2), "plan": plan.to_dict()})
                los_alerts.append((a, plan))

            if not closed:
                continue

            # 2. Predicted fade: one alert per link that escalates warning -> critical.
            valid = ~np.isnan(f.p50)
            cross50 = np.nonzero(valid & (f.p50 < CLOSURE_THRESHOLD_DB))[0]
            cross10 = np.nonzero(valid & (f.p10 < CLOSURE_THRESHOLD_DB))[0]
            rain_v = np.where(valid, f.rain, 0.0) if f.rain is not None else np.zeros_like(f.p50)
            rain_peak_i = int(np.argmax(rain_v))
            rain_peak = float(rain_v[rain_peak_i])
            if cross50.size or cross10.size:
                i = int(cross50[0]) if cross50.size else int(cross10[0])
                cause, parts = self._attribute(f, i)
                t_cross = now + float(f.horizons_s[i])
                self._raise(now, seen, id=f"FADE:{lid}", kind="FADE", cause=cause,
                            severity="critical" if cross50.size else "warning", station_id=sid,
                            link_id=lid, pool_id=st.pool_id, event_t=t_cross,
                            title=f"{st.name} {short} drops below closure ({cause})",
                            detail=f"{'Median' if cross50.size else '10th-percentile'} forecast crosses "
                                   f"{CLOSURE_THRESHOLD_DB:.1f} dB at {iso(t_cross)[11:19]}Z. Terms at the crossing "
                                   f"(dB): geometry {parts['geometry']:+.1f}, rain {parts['rain']:+.1f}, "
                                   f"equipment {parts['equipment']:+.1f}.",
                            action={"rain": "Lower the ACM floor and move pool load to an unaffected link or site "
                                            "before the fade; a spare pointed away from the cell keeps capacity.",
                                    "equipment": "Fail over to the redundant receive chain or a spare antenna now.",
                                    "geometry": "Hand over before the elevation floor."}[cause],
                            evidence={"parts_db": parts, "p50_at_cross": round(float(f.p50[i]), 2)})
            elif rain_peak >= self.fade_alert_db:
                cap_now = capacity_mbps(last.snr_db, ant.bandwidth_mhz)
                cap_min = capacity_mbps(float(f.p50[rain_peak_i]), ant.bandwidth_mhz)
                peak_t = now + float(f.horizons_s[rain_peak_i])
                self._raise(now, seen, id=f"FADE:{lid}", kind="FADE", cause="rain", severity="warning",
                            station_id=sid, link_id=lid, pool_id=st.pool_id, event_t=peak_t,
                            title=f"Rain fade on {st.name} {short} ({ant.frequency_ghz:.1f} GHz)",
                            detail=f"Nowcast rain crosses the slant path (az {float(f.az[rain_peak_i]):.0f}°, el "
                                   f"{float(f.el[rain_peak_i]):.0f}°): −{rain_peak:.1f} dB at {iso(peak_t)[11:19]}Z; "
                                   f"MODCOD {modcod_for(last.snr_db)[0]} → {modcod_for(float(f.p50[rain_peak_i]))[0]}, "
                                   f"{cap_now:.0f} → {cap_min:.0f} Mbps.",
                            action="Pre-emptively lower the ACM floor and pre-position pool traffic before the "
                                   "fade, instead of reacting to errored frames.",
                            evidence={"rain_db": round(rain_peak, 2), "capacity_mbps": [round(cap_now), round(cap_min)]})

            # 3. Equipment degradation (not while a step explains the deficit).
            if mon.alarm_t is not None and mon.step_t is None:
                ttc = None
                if f.trend_db_min < 0:
                    ttc = (last.snr_db - CLOSURE_THRESHOLD_DB) / -f.trend_db_min * 60.0
                    if ttc > 7200:
                        ttc = None  # too slow to call yet: report the deficit, not a far-off ETA
                sev = "critical" if (ttc is not None and ttc < self.horizon_s) else "warning"
                self._raise(now, seen, id=f"DEGRADATION:{lid}", kind="DEGRADATION", cause="equipment", severity=sev,
                            station_id=sid, link_id=lid, pool_id=st.pool_id,
                            event_t=(now + ttc) if ttc is not None else None,
                            title=f"{st.name} {short} degrading: not geometry, not weather",
                            detail=f"SNR runs {deficit:.1f} dB below the physics expectation, moving "
                                   f"{f.trend_db_min:+.2f} dB/min, clear sky, {last.el_deg:.0f}° elevation. "
                                   + (f"Linear projection reaches closure at {iso(now + ttc)[11:19]}Z." if ttc else ""),
                            action="Suspect the receive chain (LNA/LNB, feed, tracking). Fail over to the "
                                   "redundant chain or a spare antenna and dispatch.",
                            evidence={"deficit_db": round(deficit, 2), "trend_db_min": round(f.trend_db_min, 3),
                                      "cusum": round(mon.cusum, 1)})

            # 4. Abrupt drop (interference / blockage).
            if mon.step_t is not None:
                self._raise(now, seen, id=f"INTERFERENCE:{lid}", kind="INTERFERENCE", cause="interference",
                            severity="warning", station_id=sid, link_id=lid, pool_id=st.pool_id, event_t=mon.step_t,
                            title=f"{st.name} {short}: abrupt {mon.step_db:.1f} dB drop",
                            detail=f"Step at {iso(mon.step_t)[11:19]}Z with no rain on the path and no geometry "
                                   f"change: consistent with RF interference or a blockage. Trend extrapolation is "
                                   f"suspended while the step lasts.",
                            action="Check the spectrum analyser for an in-band carrier; geolocate if it persists.",
                            evidence={"step_db": round(mon.step_db, 2)})

        pools = self._pools(now, live, forecasts, plans)
        # A handover gap is critical only if the pool cannot carry its demand through it.
        for a, plan in los_alerts:
            p = pools.get(a.pool_id)
            if p is None or plan.mode == "make-before-break":
                continue
            tf = p.horizons_s + now
            gap = (tf >= plan.los_t) & (tf <= plan.los_t + max(plan.gap_s, 30))
            if gap.any() and float(p.capacity_mbps[gap].min()) < p.demand_p50_mbps:
                a.severity = "critical"
                if "cannot carry" not in a.action:
                    a.action += f" Pool {a.pool_id} cannot carry its demand through the gap: reroute or buffer."
        diversity: list[DiversityPlan] = []
        for p in pools.values():
            if p.shortfall_t is None:
                continue
            p50, p99 = p.demand_p50_mbps, p.demand_p99_mbps
            names = ", ".join(self.fleet.stations[s].name for s in p.stations)
            dur = (p.shortfall_end_t - p.shortfall_t) if p.shortfall_end_t else None
            # Bring a spare online on the clearest path, if any station in the pool has one.
            div = None
            had_spare = False
            for sid in p.stations:
                free = [s for s in spares_by_station.get(sid, []) if s not in claimed_spares]
                had_spare = had_spare or bool(free)
                if free:
                    div = self.plan_diversity(sid, free[0], busy_by_station.get(sid, set()), now)
                    if div:
                        claimed_spares.add(div.antenna)
                        busy_by_station.setdefault(sid, set()).add(div.successor)
                        diversity.append(div)
                        break
            self._raise(now, seen, id=f"CAPACITY:{p.pool_id}", kind="CAPACITY", cause="capacity",
                        severity="critical" if p.min_capacity_mbps < p50 else "warning",
                        station_id=p.stations[0] if p.stations else "", link_id=None, pool_id=p.pool_id,
                        event_t=p.shortfall_t,
                        title=f"Pool {p.pool_id} short of capacity",
                        detail=f"Projected capacity {p.min_capacity_mbps:.0f} Mbps vs demand p99 {p99:.0f} Mbps "
                               f"(p50 {p50:.0f}) from {iso(p.shortfall_t)[11:19]}Z"
                               + (f" for ~{dur / 60:.1f} min" if dur else "") + f" ({names}).",
                        action=(f"Bring spare {div.antenna.split('__')[1]} at {self.fleet.stations[div.station_id].name} "
                                f"online to {div.successor_name} (path az {div.mean_az_deg:.0f}°, nowcast min "
                                f"{div.min_snr_db:.0f} dB): +{div.min_capacity_mbps:.0f} Mbps through the window."
                                if div else
                                "A spare is free, but no satellite path stays clear of the weather for the window: "
                                "reroute or buffer the pool's traffic." if had_spare else
                                "No spare antenna in the pool: reroute or buffer the pool's traffic for the window."),
                        evidence={"min_capacity_mbps": round(p.min_capacity_mbps, 1), "demand_p99_mbps": round(p99, 1),
                                  "diversity": div.to_dict() if div else None})

        for aid in list(self.active):
            if aid not in seen:
                a = self.active.pop(aid)
                a.resolved_t = now
                self.resolved.append(a)
        alerts = sorted(self.active.values(), key=lambda a: (SEVERITY_RANK[a.severity], a.event_t or now))
        return {"t": now, "forecasts": forecasts, "alerts": alerts, "plans": plans, "pools": pools,
                "diversity": diversity}

    def _pools(self, now: float, live: dict[str, LinkSample], forecasts: dict[str, Forecast],
               plans: dict[str, HandoverPlan]) -> dict[str, PoolOutlook]:
        out = {}
        h = DENSE_HORIZONS_S
        tf = now + h
        for pool_id in sorted({s.pool_id for s in self.fleet.stations.values()}):
            sids = [s.id for s in self.fleet.stations_in_pool(pool_id)]
            cap = np.zeros_like(h)
            for lid, last in live.items():
                ant = self.fleet.antennas[lid]
                if ant.station_id not in sids:
                    continue
                f = forecasts.get(lid)
                if f is None or f.error:
                    cap += capacity_mbps(last.snr_db, ant.bandwidth_mhz)
                    continue
                cap += np.nan_to_num(capacity_mbps(f.p50, ant.bandwidth_mhz))
                plan = plans.get(lid)
                if plan and plan.successor and f.los_t is not None:
                    back = tf >= f.los_t + plan.gap_s
                    if back.any():
                        succ_ant = plan.antenna or lid
                        ex = self.fc.expected(succ_ant, plan.successor, tf[back], now)
                        cap[back] += np.nan_to_num(capacity_mbps(ex["clear"] - ex["rain"],
                                                                 self.fleet.antennas[succ_ant].bandwidth_mhz))
            p50, p99 = self.demand.demand(pool_id)
            short = np.nonzero(cap < p99)[0]
            st_t = float(tf[short[0]]) if short.size else None
            if short.size and short[0] == 0:
                st_t = now  # already short: an ongoing condition, not a future event
            end_t = None
            if short.size:
                after = np.nonzero(cap[short[0]:] >= p99)[0]
                end_t = float(tf[short[0] + after[0]]) if after.size else None
            out[pool_id] = PoolOutlook(pool_id, sids, h, cap, p50, p99, st_t, end_t, float(cap.min()))
        return out
