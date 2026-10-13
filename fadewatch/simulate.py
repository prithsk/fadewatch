"""Scenario simulator: the demo fleet, continued forward in time.

Every antenna in the demo-fleet snapshot keeps tracking its satellite on real
SGP4 geometry. When a satellite sets below the mask, the antenna (gateway
policy: break-before-make) slews to the best satellite of the same family
that is up and untracked at that station. Antennas that are idle in the
snapshot stay idle as spares unless an `Action` commands them, which is how
Fadewatch's plans are replayed in closed loop.

Truth SNR = clear-sky budget (calibrated) - rain along the true slant path -
injected equipment faults + scintillation + estimator noise. The output is a
`Recording` whose samples have exactly the shape of Constellation
`measurement=link` records, so they can be POSTed to /telemetry as-is.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .atmosphere import scintillation_sigma_db
from .fleet import Fleet, LinkSample
from .linkbudget import StationCalibration, clear_sky_snr
from .orbit import Propagator, iso
from .weather import RainField


@dataclass(frozen=True)
class Fault:
    kind: str              # "lna_degradation" | "interference"
    antenna_id: str
    start: float
    end: float = math.inf
    rate_db_per_min: float = 0.0
    step_db: float = 0.0
    jitter_db: float = 0.0
    max_db: float = 40.0

    def loss(self, t: float, rng: np.random.Generator) -> float:
        if not (self.start <= t < self.end):
            return 0.0
        if self.kind == "lna_degradation":
            return min(self.rate_db_per_min * (t - self.start) / 60.0, self.max_db)
        if self.kind == "interference":
            return max(self.step_db + self.jitter_db * rng.normal(), 0.0)
        return 0.0


@dataclass(frozen=True)
class Action:
    """A command to the gateway (closed-loop replay).

    kind="acquire": slew `antenna_id` to `target` now.
    kind="release_at_los": when `antenna_id` loses its satellite, idle it as a
    spare instead of re-acquiring (its successor is already up elsewhere).
    """

    t: float
    antenna_id: str
    target: str = ""
    reason: str = ""
    kind: str = "acquire"


@dataclass
class ScenarioConfig:
    name: str
    start: float
    end: float
    replay_start: float          # the dashboard's "now" at load: the snapshot time
    step_s: float = 15.0
    mask_deg: float = 10.0
    slew_s: float = 75.0
    seed: int = 11
    rain: RainField = field(default_factory=RainField)
    faults: list[Fault] = field(default_factory=list)
    spares_idle: bool = True     # antennas idle at the snapshot stay idle unless commanded
    meas_noise_db: float = 0.05


@dataclass
class Recording:
    config: ScenarioConfig
    t: np.ndarray
    antennas: list[str]
    target: np.ndarray           # (A, K) object, "" when not tracking
    mode: np.ndarray             # (A, K) "track" | "slew" | "idle"
    az: np.ndarray
    el: np.ndarray
    rng: np.ndarray
    snr: np.ndarray              # observed (NaN unless tracking)
    clear: np.ndarray            # clear-sky budget component
    rain: np.ndarray             # true rain attenuation on the path
    fault: np.ndarray            # injected equipment loss
    events: list[dict]

    def index(self, link_id: str) -> int:
        return self.antennas.index(link_id)

    def k_at(self, t: float) -> int:
        return int(np.clip(np.searchsorted(self.t, t, side="right") - 1, 0, len(self.t) - 1))

    def history(self, link_id: str, upto_t: float, window_s: float = 600.0) -> list[LinkSample]:
        """Samples a forecaster issued at `upto_t` could have seen."""
        a = self.index(link_id)
        k1 = self.k_at(upto_t)
        k0 = max(0, k1 - int(window_s // self.config.step_s))
        out = []
        for k in range(k0, k1 + 1):
            if self.mode[a, k] == "track" and not np.isnan(self.snr[a, k]):
                out.append(LinkSample(link_id, self.target[a, k], float(self.t[k]), float(self.az[a, k]),
                                      float(self.el[a, k]), float(self.rng[a, k]), float(self.snr[a, k])))
        return out

    def telemetry_records(self, fleet: Fleet, t0: float, t1: float, time_shift_s: float = 0.0,
                          entity_prefix: str = "") -> list[dict]:
        """Constellation `measurement=link` records for samples in [t0, t1)."""
        out = []
        for a, lid in enumerate(self.antennas):
            ant = fleet.antennas[lid]
            for k in np.nonzero((self.t >= t0) & (self.t < t1))[0]:
                if self.mode[a, k] != "track":
                    continue
                out.append({
                    "measurement": "link",
                    "time": iso(self.t[k] + time_shift_s),
                    "tags": {"entity_id": entity_prefix + lid, "source": ant.station_id, "target": self.target[a, k]},
                    "fields": {
                        "snr_db": round(float(self.snr[a, k]), 3), "elevation_deg": round(float(self.el[a, k]), 3),
                        "azimuth_deg": round(float(self.az[a, k]), 3), "slant_range_km": round(float(self.rng[a, k]), 3),
                        "frequency_ghz": ant.frequency_ghz, "bandwidth_mhz": ant.bandwidth_mhz,
                        "g_t_db_k": ant.g_t_db_k, "eirp_dbw": ant.eirp_dbw,
                    },
                })
        return out


class Simulator:
    def __init__(self, fleet: Fleet, prop: Propagator, cal: dict[str, StationCalibration], config: ScenarioConfig):
        self.fleet, self.prop, self.cal, self.cfg = fleet, prop, cal, config
        self.t = np.arange(config.start, config.end + 1e-6, config.step_s)
        # Candidate satellites per station = its family; precompute look angles.
        self.candidates: dict[str, list[str]] = {}
        self.look: dict[tuple[str, str], tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
        tail = np.arange(config.end, config.end + 3 * 3600, 60.0)
        self.t_ext = np.concatenate([self.t, tail[1:]])
        for sid, st in fleet.stations.items():
            fam = fleet.station_family(sid)
            sats = [s for s, sat in fleet.satellites.items() if s in prop and (sat.family == fam or fam == "mixed")]
            self.candidates[sid] = sats
            for s in sats:
                lk = prop.look(s, st.lat_deg, st.lon_deg, self.t_ext)
                self.look[(sid, s)] = (lk.az_deg, lk.el_deg, lk.range_km)

    def el(self, sid: str, sat: str, k: int) -> float:
        return float(self.look[(sid, sat)][1][k])

    def remaining_s(self, sid: str, sat: str, k: int) -> float:
        el = self.look[(sid, sat)][1][k:]
        below = np.nonzero(np.nan_to_num(el, nan=-90) < self.cfg.mask_deg)[0]
        if below.size == 0:
            return float(self.t_ext[-1] - self.t_ext[k])
        return float(self.t_ext[k + below[0]] - self.t_ext[k])

    def best_successor(self, sid: str, k: int, busy: set[str]) -> str | None:
        best, best_rem = None, 600.0  # a successor must stay up for at least 10 minutes
        for s in self.candidates[sid]:
            if s in busy or self.el(sid, s, k) < self.cfg.mask_deg + 0.5:
                continue
            rem = self.remaining_s(sid, s, k)
            if rem > best_rem:
                best, best_rem = s, rem
        return best

    def run(self, actions: list[Action] | None = None, controller=None) -> Recording:
        """Simulate the window. `controller(t, recording_so_far)` may return
        Actions each tick; it only sees samples strictly before t."""
        cfg, fleet = self.cfg, self.fleet
        rng = np.random.default_rng(cfg.seed)
        ants = sorted(fleet.antennas)
        A, K = len(ants), len(self.t)
        target = np.full((A, K), "", dtype=object)
        mode = np.full((A, K), "idle", dtype=object)
        arrs = {n: np.full((A, K), np.nan) for n in ("az", "el", "rng", "snr", "clear", "rain", "fault")}
        events: list[dict] = []
        pending = sorted(actions or [], key=lambda a: a.t)
        faults_by_ant: dict[str, list[Fault]] = {}
        for f in cfg.faults:
            faults_by_ant.setdefault(f.antenna_id, []).append(f)

        # Initial state from the snapshot: links reporting at the snapshot
        # time track their satellite (from its AOS if that falls inside the
        # pre-roll); antennas that had already gone quiet are spares.
        state = {}
        for lid in ants:
            s = fleet.latest.get(lid)
            sid = fleet.antennas[lid].station_id
            fresh = s is not None and s.t >= cfg.replay_start - 90
            state[lid] = {"mode": "idle", "target": "", "until": 0.0}
            if s is None or (sid, s.target) not in self.look or not (fresh or not cfg.spares_idle):
                continue
            up = np.nonzero(np.nan_to_num(self.look[(sid, s.target)][1][:K], nan=-90) >= cfg.mask_deg)[0]
            if up.size:
                state[lid] = {"mode": "track" if up[0] == 0 else "slew", "target": s.target,
                              "until": float(self.t[up[0]])}
        scint = {lid: 0.0 for lid in ants}
        phi = 0.3
        release: set[str] = set()
        rec = Recording(cfg, self.t, ants, target, mode, arrs["az"], arrs["el"], arrs["rng"], arrs["snr"],
                        arrs["clear"], arrs["rain"], arrs["fault"], events)

        def apply(act: Action, t: float) -> None:
            st = state[act.antenna_id]
            if act.kind == "release_at_los":
                release.add(act.antenna_id)
                return
            if st["target"] != act.target:
                events.append({"t": float(t), "type": "command", "link_id": act.antenna_id,
                               "from": st["target"], "to": act.target, "reason": act.reason})
                st.update(mode="slew", target=act.target, until=t + cfg.slew_s)

        for k, t in enumerate(self.t):
            while pending and pending[0].t <= t:
                apply(pending.pop(0), t)
            if controller is not None:
                for act in controller(float(t), rec) or []:
                    apply(act, t)
            for sid in fleet.stations:
                for lid in [a for a in ants if fleet.antennas[a].station_id == sid]:
                    st = state[lid]
                    if st["mode"] == "track" and self.el(sid, st["target"], k) < cfg.mask_deg:
                        events.append({"t": float(t), "type": "los", "link_id": lid, "target": st["target"]})
                        busy = {state[o]["target"] for o in ants if fleet.antennas[o].station_id == sid and o != lid}
                        nxt = None if lid in release else self.best_successor(sid, k, busy | {st["target"]})
                        release.discard(lid)
                        if nxt:
                            st.update(mode="slew", target=nxt, until=t + cfg.slew_s)
                            events.append({"t": float(t), "type": "slew", "link_id": lid, "target": nxt})
                        else:
                            st.update(mode="idle", target="")
                    if st["mode"] == "slew" and t >= st["until"]:
                        if self.el(sid, st["target"], k) >= cfg.mask_deg:
                            st["mode"] = "track"
                            events.append({"t": float(t), "type": "aos", "link_id": lid, "target": st["target"]})
                        elif not (np.nan_to_num(self.look[(sid, st["target"])][1][k:], nan=-90)
                                  >= cfg.mask_deg).any():
                            st.update(mode="idle", target="")  # target never rises: give up
            for a, lid in enumerate(ants):
                st = state[lid]
                mode[a, k], target[a, k] = st["mode"], st["target"]
                if st["mode"] != "track":
                    continue
                ant = fleet.antennas[lid]
                sid = ant.station_id
                az, el, rg = (float(x[k]) for x in self.look[(sid, st["target"])])
                station = fleet.stations[sid]
                clear = float(clear_sky_snr(ant, self.cal[sid], rg, el))
                rain = cfg.rain.attenuation(station.lat_deg, station.lon_deg, az, el, ant.frequency_ghz, t)
                fl = sum(f.loss(t, rng) for f in faults_by_ant.get(lid, []))
                sig = float(scintillation_sigma_db(ant.frequency_ghz, el))
                scint[lid] = phi * scint[lid] + math.sqrt(1 - phi**2) * sig * rng.normal()
                snr = clear - rain - fl + scint[lid] + cfg.meas_noise_db * rng.normal()
                for n, v in (("az", az), ("el", el), ("rng", rg), ("snr", snr), ("clear", clear),
                             ("rain", rain), ("fault", fl)):
                    arrs[n][a, k] = v
        return rec
