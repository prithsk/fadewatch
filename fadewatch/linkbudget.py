"""Link budget, per-station calibration and ACM capacity.

    SNR = EIRP + G/T - FSPL(f, d) - A_gas(el) - A_rain - L_station + 228.6 - 10 log10(B)

`L_station` lumps everything the telemetry does not describe (pointing,
polarisation, implementation and modem losses). Fitting it, plus a zenith
gaseous attenuation, per station from the demo fleet's snapshot reproduces
every observed SNR to a few hundredths of a dB (see `tests/test_physics.py`).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .atmosphere import gas_attenuation, zenith_gas_prior_db
from .fleet import Antenna, Fleet

BOLTZMANN_DBW = -228.6


def fspl_db(f_ghz, range_km):
    return 92.45 + 20 * np.log10(f_ghz) + 20 * np.log10(range_km)


@dataclass
class StationCalibration:
    station_id: str
    loss_db: float
    zenith_gas_db: float
    rms_db: float
    n: int
    method: str


def free_space_snr(ant: Antenna, range_km, g_t_offset_db: float = 0.0):
    cn0 = ant.eirp_dbw + ant.g_t_db_k + g_t_offset_db - fspl_db(ant.frequency_ghz, range_km) - BOLTZMANN_DBW
    return cn0 - 10 * math.log10(ant.bandwidth_mhz * 1e6)


def clear_sky_snr(ant: Antenna, cal: StationCalibration, range_km, el_deg):
    return free_space_snr(ant, range_km) - gas_attenuation(cal.zenith_gas_db, el_deg) - cal.loss_db


def calibrate(fleet: Fleet, min_snr_db: float = -5.0) -> dict[str, StationCalibration]:
    """Least-squares fit of (loss, zenith gas) per station on the latest samples.

    With a single elevation at a station the two terms are not separable, so
    the zenith term falls back to the standard-atmosphere prior for the
    station's frequency and only the loss is fitted. Links that do not close
    (PROBA-3 near apogee) are excluded: their reported SNR is the modem's
    estimate floor, not a link budget.
    """
    out: dict[str, StationCalibration] = {}
    for sid in fleet.stations:
        samples = [s for s in fleet.latest.values()
                   if fleet.antennas[s.link_id].station_id == sid and s.snr_db > min_snr_db and s.el_deg > 0]
        ants = fleet.antennas_at(sid)
        if not samples:
            f = ants[0].frequency_ghz if ants else 20.0
            out[sid] = StationCalibration(sid, 0.0, zenith_gas_prior_db(f), float("nan"), 0, "uncalibrated")
            continue
        fs = np.array([free_space_snr(fleet.antennas[s.link_id], s.range_km) for s in samples])
        y = fs - np.array([s.snr_db for s in samples])  # = loss + zenith * csc'(el)
        csc = gas_attenuation(1.0, np.array([s.el_deg for s in samples]))
        f_ghz = fleet.antennas[samples[0].link_id].frequency_ghz
        if np.ptp(csc) > 1.0:
            a = np.stack([np.ones_like(csc), csc], axis=1)
            (loss, zen), *_ = np.linalg.lstsq(a, y, rcond=None)
            method = "loss+zenith fit"
            if zen < 0:
                zen, method = zenith_gas_prior_db(f_ghz), "loss fit, zenith prior"
                loss = float(np.mean(y - zen * csc))
        else:
            zen = zenith_gas_prior_db(f_ghz)
            loss, method = float(np.mean(y - zen * csc)), "loss fit, zenith prior"
        resid = y - (loss + zen * csc)
        out[sid] = StationCalibration(sid, float(loss), float(zen), float(np.sqrt(np.mean(resid**2))), len(samples), method)
    return out


# DVB-S2X subset: (name, required Es/N0 dB, spectral efficiency bit/symbol).
MODCODS = [
    ("QPSK 1/4", -2.35, 0.490), ("QPSK 1/3", -1.24, 0.656), ("QPSK 1/2", 1.00, 0.989),
    ("QPSK 2/3", 3.10, 1.319), ("QPSK 3/4", 4.03, 1.484), ("QPSK 5/6", 5.18, 1.650),
    ("8PSK 2/3", 6.62, 1.980), ("8PSK 3/4", 7.91, 2.228), ("8PSK 5/6", 9.35, 2.479),
    ("16APSK 3/4", 10.21, 2.967), ("16APSK 5/6", 11.61, 3.300), ("16APSK 9/10", 12.89, 3.567),
    ("32APSK 3/4", 12.73, 3.703), ("32APSK 5/6", 14.28, 4.124), ("32APSK 9/10", 16.05, 4.453),
]
ACM_MARGIN_DB = 1.0
ROLLOFF = 0.05
# A link "closes" when the most robust MODCOD still has its ACM margin.
CLOSURE_THRESHOLD_DB = MODCODS[0][1] + ACM_MARGIN_DB


def modcod_for(snr_db: float) -> tuple[str, float]:
    best = ("none", 0.0)
    for name, req, eff in MODCODS:
        if snr_db >= req + ACM_MARGIN_DB and eff > best[1]:
            best = (name, eff)
    return best


def capacity_mbps(snr_db, bandwidth_mhz: float):
    """Throughput an ACM modem would select, from the DVB-S2X table above."""
    snr = np.atleast_1d(np.asarray(snr_db, dtype=float))
    eff = np.zeros_like(snr)
    for _, req, e in MODCODS:
        eff = np.where((snr >= req + ACM_MARGIN_DB) & (e > eff), e, eff)
    eff = np.where(np.isnan(snr), 0.0, eff)
    cap = bandwidth_mhz / (1 + ROLLOFF) * eff
    return cap if np.ndim(snr_db) else float(cap[0])
