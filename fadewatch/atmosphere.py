"""Propagation impairments for an Earth-space path (ITU-R based).

* Rain: ITU-R P.838-3 specific attenuation, and either an ITU-R P.618-13
  point-rate slant-path estimate (live mode, one rain rate at the station) or
  a direct integral of the rain field along the slant path (scenario mode,
  where we know the spatial rain field).
* Gas: cosecant law on a per-station zenith attenuation. The zenith value is
  calibrated from the fleet's own telemetry (see `linkbudget.calibrate`), with
  a standard-atmosphere prior for stations that only report one elevation.
* Scintillation: the ITU-R P.618-13 §2.4.1 standard deviation, used for
  forecast intervals and for the scenario's truth noise.
"""
from __future__ import annotations

import math

import numpy as np

# ITU-R P.838-3 regression coefficients: (a_j, b_j, c_j) rows, then (m, c).
_KH = ([(-5.33980, -0.10008, 1.13098), (-0.35351, 1.26970, 0.45400),
        (-0.23789, 0.86036, 0.15354), (-0.94158, 0.64552, 0.16817)], (-0.18961, 0.71147))
_KV = ([(-3.80595, 0.56934, 0.81061), (-3.44965, -0.22911, 0.51059),
        (-0.39902, 0.73042, 0.11899), (0.50167, 1.07319, 0.27195)], (-0.16398, 0.63297))
_AH = ([(-0.14318, 1.82442, -0.55187), (0.29591, 0.77564, 0.19822),
        (0.32177, 0.63773, 0.13164), (-5.37610, -0.96230, 1.47828),
        (16.1721, -3.29980, 3.43990)], (0.67849, -1.95537))
_AV = ([(-0.07771, 2.33840, -0.76284), (0.56727, 0.95545, 0.54039),
        (-0.20238, 1.14520, 0.26809), (-48.2991, 0.791669, 0.116226),
        (48.5833, 0.791459, 0.116479)], (-0.053739, 0.83433))


def _p838(table, f_ghz: float) -> float:
    rows, (m, c) = table
    x = math.log10(f_ghz)
    return sum(a * math.exp(-(((x - b) / cc) ** 2)) for a, b, cc in rows) + m * x + c


def rain_coefficients(f_ghz: float, el_deg: float = 90.0, tau_deg: float = 45.0) -> tuple[float, float]:
    """(k, alpha) for gamma_R = k * R**alpha in dB/km. tau=45 deg is circular polarisation."""
    kh, kv = 10 ** _p838(_KH, f_ghz), 10 ** _p838(_KV, f_ghz)
    ah, av = _p838(_AH, f_ghz), _p838(_AV, f_ghz)
    tilt = math.cos(math.radians(el_deg)) ** 2 * math.cos(math.radians(2 * tau_deg))
    k = (kh + kv + (kh - kv) * tilt) / 2
    alpha = (kh * ah + kv * av + (kh * ah - kv * av) * tilt) / (2 * k)
    return k, alpha


def specific_rain_attenuation(rain_mmh, f_ghz: float, el_deg: float = 90.0):
    k, a = rain_coefficients(f_ghz, el_deg)
    return k * np.power(np.maximum(rain_mmh, 0.0), a)


def rain_height_km(freezing_level_km: float) -> float:
    """ITU-R P.839: mean rain height is the 0 degC isotherm plus 0.36 km."""
    return freezing_level_km + 0.36


def slant_rain_attenuation_point(rain_mmh: float, f_ghz: float, el_deg: float, lat_deg: float,
                                 rain_height: float = 4.6, station_alt_km: float = 0.0) -> float:
    """ITU-R P.618-13 steps 2-10 driven by an instantaneous point rain rate.

    P.618 is a statistical method; using the instantaneous rate with the
    0.01 % path-reduction factors is the common nowcasting approximation when
    only a point rain gauge or a gridded rate at the station is available.
    """
    if rain_mmh <= 0 or el_deg <= 0:
        return 0.0
    th = math.radians(max(el_deg, 5.0))
    dh = max(rain_height - station_alt_km, 0.0)
    if dh == 0:
        return 0.0
    ls = dh / math.sin(th)
    lg = ls * math.cos(th)
    gamma = float(specific_rain_attenuation(rain_mmh, f_ghz, el_deg))
    r = 1.0 / (1 + 0.78 * math.sqrt(lg * gamma / f_ghz) - 0.38 * (1 - math.exp(-2 * lg)))
    zeta = math.degrees(math.atan2(dh, lg * r))
    lr = lg * r / math.cos(th) if zeta > math.degrees(th) else dh / math.sin(th)
    chi = 36 - abs(lat_deg) if abs(lat_deg) < 36 else 0.0
    v = 1.0 / (1 + math.sqrt(math.sin(th)) * (
        31 * (1 - math.exp(-math.degrees(th) / (1 + chi))) * math.sqrt(lr * gamma) / f_ghz**2 - 0.45))
    return gamma * lr * v


def path_ground_points(lat_deg: float, lon_deg: float, az_deg: float, el_deg: float,
                       rain_height: float, step_km: float = 0.5) -> tuple[np.ndarray, np.ndarray, float]:
    """Ground projection of the slant path below the rain height.

    Returns lat/lon samples (flat-earth offsets; the path is < 60 km) and the
    slant length represented by each sample.
    """
    th = math.radians(max(el_deg, 2.0))
    lg = rain_height / math.tan(th)
    n = max(int(math.ceil(lg / step_km)), 1)
    s = (np.arange(n) + 0.5) * (lg / n)
    az = math.radians(az_deg)
    dn, de = s * math.cos(az), s * math.sin(az)
    lat = lat_deg + dn / 111.32
    lon = lon_deg + de / (111.32 * math.cos(math.radians(lat_deg)))
    return lat, lon, (lg / n) / math.cos(th)


# Mid-latitude standard-atmosphere zenith gaseous attenuation (dB), coarse
# P.676-order values, used only as a prior when a station cannot be calibrated.
_ZENITH_PRIOR = np.array([
    (1, 0.035), (4, 0.040), (7, 0.045), (10, 0.055), (12, 0.065), (15, 0.090),
    (18, 0.17), (19, 0.21), (20, 0.26), (22, 0.38), (25, 0.25), (30, 0.22),
    (40, 0.35), (50, 1.5),
])


def zenith_gas_prior_db(f_ghz: float) -> float:
    return float(np.interp(f_ghz, _ZENITH_PRIOR[:, 0], _ZENITH_PRIOR[:, 1]))


def gas_attenuation(zenith_db: float, el_deg):
    """Cosecant law with an Earth-curvature correction that stays finite near
    the horizon (the P.618 slant-path form for low elevations)."""
    el = np.radians(np.maximum(el_deg, 0.5))
    return zenith_db * 2.0 / (np.sqrt(np.sin(el) ** 2 + 2.35e-4) + np.sin(el))


def scintillation_sigma_db(f_ghz: float, el_deg, n_wet: float = 42.0,
                           dish_m: float = 9.0, efficiency: float = 0.6):
    """ITU-R P.618-13 §2.4.1 tropospheric scintillation standard deviation."""
    el = np.radians(np.maximum(el_deg, 4.0))
    sigma_ref = 3.6e-3 + 1e-4 * n_wet
    hl = 1000.0
    path = 2 * hl / (np.sqrt(np.sin(el) ** 2 + 2.35e-4) + np.sin(el))
    d_eff = math.sqrt(efficiency) * dish_m
    x = 1.22 * d_eff**2 * (f_ghz / path)
    g = np.sqrt(np.maximum(3.86 * (x**2 + 1) ** (11 / 12) * np.sin(11 / 6 * np.arctan(1 / x))
                           - 7.08 * x ** (5 / 6), 1e-6))
    return sigma_ref * f_ghz ** (7 / 12) * g / np.sin(el) ** 1.2
