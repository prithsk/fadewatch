"""Orbit propagation and station-relative look angles.

SGP4 (via the `sgp4` package) gives TEME positions; we rotate to ECEF with
GMST (IAU-82) and project into each ground station's local ENU frame. Polar
motion and the equation of the equinoxes are ignored: that costs tens of
metres, far below what a link budget can see.

All times are float64 Unix seconds (UTC) so they vectorise cleanly.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
from sgp4.api import Satrec

WGS84_A = 6378.137
WGS84_F = 1 / 298.257223563
WGS84_E2 = WGS84_F * (2 - WGS84_F)
UNIX_EPOCH_JD = 2440587.5


def to_unix(t: datetime | str | float) -> float:
    if isinstance(t, (int, float, np.floating)):
        return float(t)
    if isinstance(t, str):
        t = datetime.fromisoformat(t.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.timestamp()


def iso(t: float) -> str:
    return datetime.fromtimestamp(float(t), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _jd(t: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    days = np.floor(t / 86400.0)
    return days + UNIX_EPOCH_JD, (t - days * 86400.0) / 86400.0


def gmst_rad(t: np.ndarray) -> np.ndarray:
    jd, fr = _jd(t)
    tu = ((jd - 2451545.0) + fr) / 36525.0
    g = 67310.54841 + (876600 * 3600 + 8640184.812866) * tu + 0.093104 * tu**2 - 6.2e-6 * tu**3
    return np.radians((g % 86400.0) / 240.0)


def geodetic_to_ecef(lat_deg: float, lon_deg: float, alt_km: float = 0.0) -> np.ndarray:
    la, lo = math.radians(lat_deg), math.radians(lon_deg)
    n = WGS84_A / math.sqrt(1 - WGS84_E2 * math.sin(la) ** 2)
    return np.array([
        (n + alt_km) * math.cos(la) * math.cos(lo),
        (n + alt_km) * math.cos(la) * math.sin(lo),
        (n * (1 - WGS84_E2) + alt_km) * math.sin(la),
    ])


def ecef_to_geodetic(r: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Bowring's method; r is (N, 3) km. Returns lat, lon (deg), alt (km)."""
    x, y, z = r[:, 0], r[:, 1], r[:, 2]
    lon = np.arctan2(y, x)
    p = np.hypot(x, y)
    b = WGS84_A * (1 - WGS84_F)
    ep2 = (WGS84_A**2 - b**2) / b**2
    th = np.arctan2(z * WGS84_A, p * b)
    lat = np.arctan2(z + ep2 * b * np.sin(th) ** 3, p - WGS84_E2 * WGS84_A * np.cos(th) ** 3)
    n = WGS84_A / np.sqrt(1 - WGS84_E2 * np.sin(lat) ** 2)
    alt = p / np.cos(lat) - n
    return np.degrees(lat), np.degrees(lon), alt


@dataclass(frozen=True)
class Look:
    """Station-relative geometry for one satellite over a time grid."""

    t: np.ndarray
    az_deg: np.ndarray
    el_deg: np.ndarray
    range_km: np.ndarray
    range_rate_kms: np.ndarray


@dataclass(frozen=True)
class Pass:
    sat_id: str
    aos: float
    los: float
    tca: float
    max_el_deg: float

    @property
    def duration_s(self) -> float:
        return self.los - self.aos


class Propagator:
    """Vectorised SGP4 for a set of satellites.

    `epoch_offsets_s` lets a satellite's geometry be evaluated at `t + offset`.
    The demo fleet's LEO/HEO links are a replay recorded 25.5 days earlier
    (see `align.py`), so the offset re-creates the geometry they were
    actually recorded with.
    """

    def __init__(self, tles: dict[str, tuple[str, str, str]], epoch_offsets_s: dict[str, float] | None = None):
        self.names = {sid: v[0] for sid, v in tles.items()}
        self.lines = {sid: (v[1], v[2]) for sid, v in tles.items()}
        self.sats = {sid: Satrec.twoline2rv(v[1], v[2]) for sid, v in tles.items()}
        self.offsets = dict(epoch_offsets_s or {})

    def __contains__(self, sat_id: str) -> bool:
        return sat_id in self.sats

    def teme(self, sat_id: str, t: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        t = np.atleast_1d(np.asarray(t, dtype=float)) + self.offsets.get(sat_id, 0.0)
        jd, fr = _jd(t)
        err, r, v = self.sats[sat_id].sgp4_array(jd, fr)
        r = np.where(err[:, None] == 0, r, np.nan)
        v = np.where(err[:, None] == 0, v, np.nan)
        return r, v

    def ecef(self, sat_id: str, t: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        t = np.atleast_1d(np.asarray(t, dtype=float))
        r, v = self.teme(sat_id, t)
        # Earth rotation uses the *shifted* epoch too, so the ground track is
        # the one the recording saw.
        th = gmst_rad(t + self.offsets.get(sat_id, 0.0))
        c, s = np.cos(th), np.sin(th)
        omega = 7.2921158553e-5
        re = np.stack([c * r[:, 0] + s * r[:, 1], -s * r[:, 0] + c * r[:, 1], r[:, 2]], axis=1)
        ve = np.stack([
            c * v[:, 0] + s * v[:, 1] + omega * re[:, 1],
            -s * v[:, 0] + c * v[:, 1] - omega * re[:, 0],
            v[:, 2],
        ], axis=1)
        return re, ve

    def subpoint(self, sat_id: str, t: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        re, _ = self.ecef(sat_id, t)
        return ecef_to_geodetic(re)

    def look(self, sat_id: str, lat_deg: float, lon_deg: float, t: np.ndarray, alt_km: float = 0.0) -> Look:
        t = np.atleast_1d(np.asarray(t, dtype=float))
        re, ve = self.ecef(sat_id, t)
        d = re - geodetic_to_ecef(lat_deg, lon_deg, alt_km)
        la, lo = math.radians(lat_deg), math.radians(lon_deg)
        sl, cl, so, co = math.sin(la), math.cos(la), math.sin(lo), math.cos(lo)
        e = -so * d[:, 0] + co * d[:, 1]
        n = -sl * co * d[:, 0] - sl * so * d[:, 1] + cl * d[:, 2]
        u = cl * co * d[:, 0] + cl * so * d[:, 1] + sl * d[:, 2]
        rng = np.sqrt(e * e + n * n + u * u)
        return Look(
            t=t,
            az_deg=np.degrees(np.arctan2(e, n)) % 360.0,
            el_deg=np.degrees(np.arcsin(u / rng)),
            range_km=rng,
            range_rate_kms=np.einsum("ij,ij->i", d, ve) / rng,
        )

    def passes(self, sat_id: str, lat_deg: float, lon_deg: float, t0: float, t1: float,
               mask_deg: float = 10.0, step_s: float = 30.0) -> list[Pass]:
        """Above-mask windows in [t0, t1]; edges refined to ~0.5 s by bisection."""
        t = np.arange(t0, t1 + step_s, step_s)
        el = self.look(sat_id, lat_deg, lon_deg, t).el_deg
        up = np.nan_to_num(el, nan=-90.0) >= mask_deg
        out: list[Pass] = []
        if not up.any():
            return out

        def edge(a: float, b: float, rising: bool) -> float:
            for _ in range(8):
                m = 0.5 * (a + b)
                above = self.look(sat_id, lat_deg, lon_deg, np.array([m])).el_deg[0] >= mask_deg
                if above == rising:
                    b = m
                else:
                    a = m
            return 0.5 * (a + b)

        i = 0
        while i < len(t):
            if not up[i]:
                i += 1
                continue
            j = i
            while j + 1 < len(t) and up[j + 1]:
                j += 1
            aos = t0 if i == 0 else edge(t[i - 1], t[i], rising=True)
            los = t1 if j == len(t) - 1 else edge(t[j], t[j + 1], rising=False)
            k = i + int(np.nanargmax(el[i:j + 1]))
            out.append(Pass(sat_id, float(aos), float(los), float(t[k]), float(el[k])))
            i = j + 1
        return out


def load_tle_file(path: str) -> dict[str, tuple[str, str, str]]:
    """Three-line TLE file -> {"norad-<id>": (name, line1, line2)}."""
    lines = [ln.rstrip() for ln in open(path, encoding="utf-8") if ln.strip()]
    out = {}
    for i in range(0, len(lines) - 2, 3):
        name, l1, l2 = lines[i].strip(), lines[i + 1], lines[i + 2]
        out[f"norad-{int(l1[2:7])}"] = (name, l1, l2)
    return out
