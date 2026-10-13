"""Check reported pointing against propagated orbits, and recover replay offsets.

The demo fleet's O3b (MEO) links match SGP4 geometry at their stamped
`observed_at` to ~0.1 deg. Its Globalstar, BIOMASS and PROBA-3 links do not;
they match exactly at `observed_at - 25.5 days`: those passes were recorded on
2026-09-15 and re-stamped to the present. `align_fleet` finds that offset per
satellite so every downstream model works on the geometry the telemetry was
actually recorded with.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .fleet import Fleet
from .orbit import Propagator


@dataclass
class Alignment:
    link_id: str
    sat_id: str
    offset_s: float
    az_err_deg: float
    el_err_deg: float
    range_err_km: float

    @property
    def matched(self) -> bool:
        return self.el_err_deg < 0.5 and self.az_err_deg < 1.0


def _cost(look, az, el, rng):
    daz = np.abs((look.az_deg - az + 180) % 360 - 180)
    return daz * np.cos(np.radians(el)) + np.abs(look.el_deg - el) + np.abs(look.range_km - rng) / 50.0


def check_link(prop: Propagator, fleet: Fleet, link_id: str, offset_s: float = 0.0) -> Alignment:
    s = fleet.latest[link_id]
    st = fleet.stations[fleet.antennas[link_id].station_id]
    saved = prop.offsets.get(s.target)
    prop.offsets[s.target] = offset_s
    lk = prop.look(s.target, st.lat_deg, st.lon_deg, np.array([s.t]))
    if saved is None:
        prop.offsets.pop(s.target)
    else:
        prop.offsets[s.target] = saved
    return Alignment(link_id, s.target, offset_s,
                     float(abs((lk.az_deg[0] - s.az_deg + 180) % 360 - 180)),
                     float(abs(lk.el_deg[0] - s.el_deg)), float(abs(lk.range_km[0] - s.range_km)))


def search_offset(prop: Propagator, fleet: Fleet, link_id: str, span_days: float = 40.0,
                  coarse_s: float = 5.0) -> float:
    s = fleet.latest[link_id]
    st = fleet.stations[fleet.antennas[link_id].station_id]
    offs = np.arange(-span_days * 86400, 2 * 86400, coarse_s)
    saved = prop.offsets.pop(s.target, None)
    lk = prop.look(s.target, st.lat_deg, st.lon_deg, s.t + offs)
    cost = np.nan_to_num(_cost(lk, s.az_deg, s.el_deg, s.range_km), nan=1e9)
    best = float(offs[int(np.argmin(cost))])
    fine = np.arange(best - coarse_s, best + coarse_s, 0.25)
    lk = prop.look(s.target, st.lat_deg, st.lon_deg, s.t + fine)
    cost = np.nan_to_num(_cost(lk, s.az_deg, s.el_deg, s.range_km), nan=1e9)
    if saved is not None:
        prop.offsets[s.target] = saved
    return float(fine[int(np.argmin(cost))])


def align_fleet(prop: Propagator, fleet: Fleet, quantum_s: float = 1800.0) -> list[Alignment]:
    """Fill `prop.offsets` for satellites whose reported geometry needs a replay offset.

    Offsets found independently per link are snapped to a common value when
    they agree within a few minutes, so one consistent clock shift is applied
    to the whole replayed segment (the demo fleet's is exactly -25.5 days).
    """
    report: list[Alignment] = []
    found: dict[str, float] = {}
    for lid, s in fleet.latest.items():
        if s.target not in prop:
            continue
        a = check_link(prop, fleet, lid, 0.0)
        if not a.matched:
            off = search_offset(prop, fleet, lid)
            found[lid] = off
    if found:
        # Snap to the dominant offset: recordings are shifted by whole half-hours.
        vals = np.array(list(found.values()))
        snapped = np.round(vals / quantum_s) * quantum_s
        common = float(np.median(snapped))
        for lid in found:
            sat = fleet.latest[lid].target
            a = check_link(prop, fleet, lid, common)
            prop.offsets[sat] = common if a.matched else found[lid]
    for lid, s in fleet.latest.items():
        if s.target in prop:
            report.append(check_link(prop, fleet, lid, prop.offsets.get(s.target, 0.0)))
    # Satellites never seen in a link inherit the segment offset if their family was replayed.
    if found:
        replayed = {fleet.satellites[fleet.latest[l].target].family for l in found}
        for sid, sat in fleet.satellites.items():
            if sid in prop and sid not in prop.offsets and sat.family in replayed:
                prop.offsets[sid] = common
    return report
