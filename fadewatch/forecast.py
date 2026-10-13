"""SNR forecasters.

`PersistenceBaseline` replicates Constellation's served `snr-baseline-v1`
as documented: the median at every horizon is the last observed `snr_db`
(read from a 4-minute window), the 80 % interval widens with the horizon, and
a link with no sample in the window is declined (`insufficient_telemetry`).
Its 1-minute half-width is anchored on the documented example
(12.5 -> 12.166 / 12.834 dB); widening as sqrt(h) is our assumption.

`PhysicsForecaster` forecasts the same quantity from what the link *will* see:

    p50(t+h) = budget(geometry(t+h)) - rain_nowcast(t+h) + bias + trend(h)

where geometry comes from SGP4, the budget is the calibrated station model,
rain comes from the nowcast along the future slant path, and bias/trend are
learned online from the residual between telemetry and physics. Intervals
add the residual noise, P.618 scintillation at the *future* elevation,
nowcast uncertainty and trend uncertainty.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .atmosphere import scintillation_sigma_db
from .fleet import Fleet, LinkSample
from .linkbudget import StationCalibration, clear_sky_snr
from .orbit import Propagator

Z80 = 1.2815515655446004  # p90 z-score: p10/p90 bound an 80 % interval
STD_HORIZONS_S = np.array([60.0, 180.0, 300.0])
DENSE_HORIZONS_S = np.arange(30.0, 1800.0 + 1, 30.0)


@dataclass
class Forecast:
    link_id: str
    target: str
    model: str
    issue_t: float
    horizons_s: np.ndarray
    p10: np.ndarray
    p50: np.ndarray
    p90: np.ndarray
    los_t: float | None = None           # predicted loss of signal (below mask)
    el: np.ndarray | None = None
    az: np.ndarray | None = None
    clear: np.ndarray | None = None
    rain: np.ndarray | None = None
    trend: np.ndarray | None = None
    bias_db: float = 0.0
    trend_db_min: float = 0.0
    resid_sigma: float = 0.0
    last: LinkSample | None = None
    error: str | None = None

    def at(self, horizon_s: float) -> tuple[float, float, float]:
        i = int(np.argmin(np.abs(self.horizons_s - horizon_s)))
        return float(self.p10[i]), float(self.p50[i]), float(self.p90[i])


class PersistenceBaseline:
    name = "snr-baseline-v1 (replica)"
    window_s = 240.0
    sigma_1min = 0.333696 / Z80

    def forecast(self, link_id: str, history: list[LinkSample], issue_t: float,
                 horizons_s: np.ndarray = STD_HORIZONS_S) -> Forecast:
        recent = [s for s in history if issue_t - self.window_s <= s.t <= issue_t]
        h = np.asarray(horizons_s, dtype=float)
        if not recent:
            nan = np.full(h.shape, np.nan)
            return Forecast(link_id, "", self.name, issue_t, h, nan, nan, nan, error="insufficient_telemetry")
        last = recent[-1]
        sig = self.sigma_1min * np.sqrt(h / 60.0)
        p50 = np.full(h.shape, last.snr_db)
        return Forecast(link_id, last.target, self.name, issue_t, h, p50 - Z80 * sig, p50, p50 + Z80 * sig, last=last)


@dataclass
class PhysicsForecaster:
    fleet: Fleet
    prop: Propagator
    cal: dict[str, StationCalibration]
    nowcast: object | None = None        # weather.Nowcast or weather.PointNowcast
    mask_deg: float = 10.0
    bias_tau_s: float = 60.0
    trend_window_s: float = 360.0
    trend_damping_s: float = 1200.0
    name: str = "fadewatch-physics-v1"
    _rain_cache: dict = field(default_factory=dict, repr=False)

    # ---- physics pieces -------------------------------------------------
    def rain_db(self, station_id: str, az: float, el: float, f_ghz: float, t: float, issue_t: float) -> float:
        if self.nowcast is None or el <= 0:
            return 0.0
        st = self.fleet.stations[station_id]
        if hasattr(self.nowcast, "attenuation_for"):           # live point nowcast
            return self.nowcast.attenuation_for(station_id, st.lat_deg, el, f_ghz, t)
        key = (station_id, round(az, 1), round(el, 1), round(f_ghz, 2), round(t), round(issue_t))
        if key not in self._rain_cache:
            if len(self._rain_cache) > 200_000:
                self._rain_cache.clear()
            self._rain_cache[key] = self.nowcast.attenuation(st.lat_deg, st.lon_deg, az, el, f_ghz, t, issue_t)
        return self._rain_cache[key]

    def rain_near(self, station_id: str, t: float, issue_t: float, reach_km: float = 45.0) -> bool:
        """Cheap gate: is any nowcast cell close enough to touch this station's paths?"""
        if self.nowcast is None:
            return False
        if hasattr(self.nowcast, "attenuation_for"):
            return self.nowcast.rain_rate(station_id, t) > 0
        st = self.fleet.stations[station_id]
        for c, amp in self.nowcast.field_at(issue_t):
            lat, lon = c.centre(t)
            d = math.hypot((lat - st.lat_deg) * 111.32, (lon - st.lon_deg) * 111.32 * math.cos(math.radians(st.lat_deg)))
            if d < reach_km + 3 * c.radius_km and amp > 0.2:
                return True
        return False

    def expected(self, link_id: str, target: str, t: np.ndarray, issue_t: float,
                 geometry: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None) -> dict:
        ant = self.fleet.antennas[link_id]
        st = self.fleet.stations[ant.station_id]
        if geometry is None:
            lk = self.prop.look(target, st.lat_deg, st.lon_deg, t)
            az, el, rng = lk.az_deg, lk.el_deg, lk.range_km
        else:
            az, el, rng = geometry
        clear = clear_sky_snr(ant, self.cal[ant.station_id], rng, el)
        rain = np.zeros_like(clear)
        for i, ti in enumerate(np.atleast_1d(t)):
            if el[i] > 0 and self.rain_near(ant.station_id, ti, issue_t):
                rain[i] = self.rain_db(ant.station_id, az[i], el[i], ant.frequency_ghz, ti, issue_t)
        return {"az": az, "el": el, "rng": rng, "clear": clear, "rain": rain}

    # ---- online residual model -----------------------------------------
    def residuals(self, link_id: str, history: list[LinkSample]) -> tuple[np.ndarray, np.ndarray]:
        if not history:
            return np.array([]), np.array([])
        t = np.array([s.t for s in history])
        geo = (np.array([s.az_deg for s in history]), np.array([s.el_deg for s in history]),
               np.array([s.range_km for s in history]))
        exp = np.empty(len(history))
        # Residuals are taken against the physics expected *at each sample*,
        # using the nowcast analysis issued at that sample time.
        ant = self.fleet.antennas[link_id]
        cal = self.cal[ant.station_id]
        exp[:] = clear_sky_snr(ant, cal, geo[2], geo[1])
        for i, s in enumerate(history):
            if self.rain_near(ant.station_id, s.t, s.t):
                exp[i] -= self.rain_db(ant.station_id, s.az_deg, s.el_deg, ant.frequency_ghz, s.t, s.t)
        return t, np.array([s.snr_db for s in history]) - exp

    @staticmethod
    def trend_fit(t: np.ndarray, r: np.ndarray) -> tuple[float, float]:
        """OLS slope (dB/min) and its standard error."""
        if len(t) < 8:
            return 0.0, 0.0
        x = (t - t.mean()) / 60.0
        sxx = float(np.sum(x * x))
        if sxx <= 0:
            return 0.0, 0.0
        b = float(np.sum(x * (r - r.mean())) / sxx)
        res = r - r.mean() - b * x
        se = math.sqrt(float(np.sum(res**2)) / max(len(t) - 2, 1) / sxx)
        return b, se

    def forecast(self, link_id: str, history: list[LinkSample], issue_t: float,
                 horizons_s: np.ndarray = DENSE_HORIZONS_S, allow_trend: bool = True,
                 drift_confirmed: bool = False) -> Forecast:
        h = np.asarray(horizons_s, dtype=float)
        recent = [s for s in history if s.t <= issue_t]
        if not recent or issue_t - recent[-1].t > 240:
            nan = np.full(h.shape, np.nan)
            return Forecast(link_id, "", self.name, issue_t, h, nan, nan, nan, error="insufficient_telemetry")
        last = recent[-1]
        target = last.target
        # Only residuals on the current target: a handover resets geometry, not hardware,
        # but a new pass can carry a different (uncalibrated) satellite EIRP.
        same = [s for s in recent if s.target == target and s.t >= issue_t - max(self.trend_window_s, 600)]
        t_hist, resid = self.residuals(link_id, same)
        w = np.exp(-(issue_t - t_hist) / self.bias_tau_s)
        bias = float(np.sum(w * resid) / np.sum(w))
        win = t_hist >= issue_t - self.trend_window_s
        slope, se = self.trend_fit(t_hist[win], resid[win])
        if not allow_trend:
            slope_used, se_used = 0.0, 0.0      # the monitor saw a step: a step is not a drift
        elif abs(slope) < max(3 * se, 0.08):
            slope_used, se_used = 0.0, se       # not significant: widen, do not bend
        else:
            slope_used, se_used = slope, se
        d = np.diff(resid[-12:])
        resid_sigma = float(np.std(d) / math.sqrt(2)) if d.size >= 3 else 0.2

        if target not in self.prop:
            # No orbit for this target: fall back to persistence of the residual-free level.
            nan = np.full(h.shape, np.nan)
            return Forecast(link_id, target, self.name, issue_t, h, nan, nan, nan, error="no_orbit", last=last)
        if slope_used != 0.0:
            # With a real trend, an EWMA lags a ramp by its time constant: anchor
            # on the fitted line at the last sample instead.
            tw = t_hist[win]
            bias = float(np.mean(resid[win]) + slope_used * (last.t - tw.mean()) / 60.0)
        tf = issue_t + h
        ex = self.expected(link_id, target, tf, issue_t)
        if drift_confirmed:
            # The monitor has confirmed a persistent equipment drift: extrapolate it.
            trend = slope_used * (tf - last.t) / 60.0
        else:
            tau = self.trend_damping_s
            trend = slope_used * (tau / 60.0) * (1 - np.exp(-(tf - last.t) / tau))
        p50 = ex["clear"] - ex["rain"] + bias + trend
        ant = self.fleet.antennas[link_id]
        scint = scintillation_sigma_db(ant.frequency_ghz, ex["el"])
        hm = (tf - last.t) / 60.0
        sigma = np.sqrt(np.maximum(resid_sigma, scint) ** 2
                        + (0.04 * np.sqrt(hm)) ** 2
                        + (ex["rain"] * (0.12 + 0.03 * hm)) ** 2
                        + (se_used * hm) ** 2)
        below = ex["el"] < self.mask_deg
        los_t = None
        if below.any():
            i = int(np.argmax(below))
            if i == 0:
                los_t = float(issue_t)
            else:
                e0, e1 = ex["el"][i - 1], ex["el"][i]
                frac = (e0 - self.mask_deg) / (e0 - e1) if e0 != e1 else 0.0
                los_t = float(tf[i - 1] + frac * (tf[i] - tf[i - 1]))
            p50 = np.where(below, np.nan, p50)
        p10, p90 = p50 - Z80 * sigma, p50 + Z80 * sigma
        return Forecast(link_id, target, self.name, issue_t, h, p10, p50, p90, los_t=los_t,
                        el=ex["el"], az=ex["az"], clear=ex["clear"], rain=ex["rain"], trend=trend,
                        bias_db=bias, trend_db_min=slope_used, resid_sigma=resid_sigma, last=last)


@dataclass
class AntennaMonitor:
    """Per-antenna anomaly state, carried across handovers (hardware is the antenna's).

    A two-sided view of the physics residual:
      * CUSUM on the residual relative to a slow reference catches gradual
        equipment degradation (LNA, feed, pointing) well before it costs a
        MODCOD.
      * A step test catches abrupt drops (interference, blockage).
    Rain is excluded by construction: residuals are taken against a budget
    that already subtracts the nowcast rain on that path, and alarms are
    suppressed where nowcast rain is significant (its own errors dominate).
    """

    link_id: str
    ref: float | None = None
    cusum: float = 0.0
    k: float = 0.5
    h: float = 10.0
    alarm_t: float | None = None
    step_t: float | None = None
    step_db: float = 0.0
    last_t: float = -math.inf
    ref_tau_s: float = 1800.0
    sigma: float = 0.25
    step_cleared_t: float = -math.inf

    def update(self, t_hist: np.ndarray, resid: np.ndarray, rain_db: float, sigma: float) -> None:
        if self.ref is None:
            # Reference = median of the first minutes of a healthy track, not one noisy sample.
            if len(resid) < 8:
                return
            self.ref = float(np.median(resid[:8]))
            self.last_t = float(t_hist[7])
        new = t_hist > self.last_t
        if not new.any():
            return
        d = np.diff(resid[-24:])
        if d.size >= 6:
            self.sigma = max(1.4826 * float(np.median(np.abs(d - np.median(d)))) / math.sqrt(2), 0.2)
        sig = max(self.sigma, sigma, 0.2)
        for t, r in zip(t_hist[new], resid[new]):
            z = (self.ref - r) / sig
            if rain_db > 1.0:
                z = 0.0  # rain mis-forecast is not an equipment fault
            # Capped so a burst cannot hold the alarm for an hour after it ends.
            self.cusum = min(max(0.0, self.cusum + z - self.k), 3 * self.h)
            if self.cusum > self.h and self.alarm_t is None:
                self.alarm_t = float(t)
            if self.cusum == 0.0:
                self.alarm_t = None
                # Track slow calibration drift only while healthy.
                a = 1 - math.exp(-15.0 / self.ref_tau_s)
                self.ref = (1 - a) * self.ref + a * float(r)
        self.last_t = float(t_hist[-1])
        if len(resid) >= 6 and rain_db <= 1.0:
            before = float(np.median(resid[-12:-3])) if len(resid) >= 12 else float(np.median(resid[:-3]))
            after = float(np.median(resid[-3:]))
            if before - after > 4.0:
                if self.step_t is None:
                    self.step_t, self.step_db = float(t_hist[-3]), before - after
            elif self.step_t is not None and abs(after - (self.ref or 0)) < 1.5:
                # Back to normal: the step explained the deficit, so clear the drift detector too.
                self.step_t, self.step_db = None, 0.0
                self.step_cleared_t = float(t_hist[-1])
                self.cusum, self.alarm_t = 0.0, None

    def trend_allowed(self, now: float, holdoff_s: float = 360.0) -> bool:
        """No trend extrapolation during a step or while its edge is still in the fit window."""
        return self.step_t is None and now - self.step_cleared_t > holdoff_s
