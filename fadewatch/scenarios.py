"""Named scenarios over the demo fleet, and a one-call context loader."""
from __future__ import annotations

import math
from dataclasses import dataclass

from .align import Alignment, align_fleet
from .fleet import DEMO_TLE, Fleet
from .linkbudget import StationCalibration, calibrate
from .orbit import Propagator, load_tle_file, to_unix
from .simulate import Fault, ScenarioConfig
from .weather import KM_PER_DEG, RainCell, RainField

SNAPSHOT_T = to_unix("2026-10-10T23:07:00Z")  # newest sample in the demo-fleet snapshot


@dataclass
class Context:
    fleet: Fleet
    prop: Propagator
    cal: dict[str, StationCalibration]
    alignment: list[Alignment]


def load_context(tle_path: str = DEMO_TLE, fleet: Fleet | None = None) -> Context:
    fleet = fleet or Fleet.load_demo()
    prop = Propagator(load_tle_file(tle_path))
    alignment = align_fleet(prop, fleet)
    return Context(fleet, prop, calibrate(fleet), alignment)


def _storm(lat: float, lon: float, t_pass: float, miss_km_south: float = 3.0) -> RainField:
    """A convective cell tracking ENE (bearing 65 deg) at 55 km/h, passing 3 km
    south of (lat, lon), inside a light stratiform shield. Peak 40 mm/h,
    6 km radius: an ordinary Texas thunderstorm, which is plenty at 19 GHz."""
    lat_c = lat - miss_km_south / KM_PER_DEG
    ve, vn = 55 * math.sin(math.radians(65)), 55 * math.cos(math.radians(65))
    cell = RainCell(lat_c, lon, t_pass, v_east_kmh=ve, v_north_kmh=vn, peak_mmh=40.0, radius_km=6.0,
                    t_peak=t_pass + 120, life_s=2100, name="convective cell")
    shield = RainCell(lat_c - 0.05, lon - 0.15, t_pass, v_east_kmh=ve, v_north_kmh=vn, peak_mmh=4.0,
                      radius_km=30.0, t_peak=t_pass + 600, life_s=4200, name="stratiform shield")
    return RainField([cell, shield], freezing_level_km=4.4)


def demo(fleet: Fleet) -> ScenarioConfig:
    t0 = SNAPSHOT_T
    texas = fleet.stations["SITE-7576272aee77"]
    return ScenarioConfig(
        name="demo-fleet: storm, failing LNA, interference",
        start=t0 - 600, end=t0 + 75 * 60, replay_start=t0,
        rain=_storm(texas.lat_deg, texas.lon_deg, t_pass=t0 + 28 * 60),
        faults=[
            # Phoenix Gateway: single antenna, sole server of pool TMPEAZX1.
            Fault("lna_degradation", "SITE-0da7faea0f45__ant0", start=t0 + 33 * 60, rate_db_per_min=0.9),
            # O3B1.WMP: a 6-minute in-band interferer.
            Fault("interference", "SITE-5c44ebc4a84e__ant7", start=t0 + 51 * 60, end=t0 + 57 * 60,
                  step_db=9.0, jitter_db=1.5),
        ],
    )


def quiet(fleet: Fleet) -> ScenarioConfig:
    """Clear sky, healthy equipment: only geometry happens. Used for false-alarm rates."""
    t0 = SNAPSHOT_T
    return ScenarioConfig(name="demo-fleet: quiet", start=t0 - 600, end=t0 + 75 * 60, replay_start=t0, seed=5)


SCENARIOS = {"demo": demo, "quiet": quiet}
