"""Rain fields, nowcasts and live weather.

Scenario mode keeps a spatial rain field made of moving convective cells
(Gaussian rate profiles with a growth/decay life cycle) over a weak stratiform
shield. The *truth* field drives the simulator. The forecaster only sees a
`Nowcast`: a radar-extrapolation-style product that knows each cell's
position and intensity at issue time (with QPE error) and advects it with an
imperfect motion vector, holding intensity constant (Lagrangian
persistence). Its errors therefore grow with lead time the way real
nowcasts do: wrong motion, and no knowledge of growth or decay.

Live mode uses Open-Meteo's 15-minute precipitation and freezing-level
forecasts at each station and along each link's azimuth.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

import httpx
import numpy as np

from .atmosphere import (path_ground_points, rain_height_km, slant_rain_attenuation_point,
                         specific_rain_attenuation)

KM_PER_DEG = 111.32


@dataclass(frozen=True)
class RainCell:
    lat0: float
    lon0: float
    t0: float                 # time the cell centre is at (lat0, lon0)
    v_east_kmh: float
    v_north_kmh: float
    peak_mmh: float
    radius_km: float          # Gaussian e-folding radius
    t_peak: float             # life cycle: intensity peaks here...
    life_s: float = 3600.0    # ...with this half-width (raised cosine)
    name: str = "cell"

    def centre(self, t):
        dt_h = (np.asarray(t, dtype=float) - self.t0) / 3600.0
        lat = self.lat0 + self.v_north_kmh * dt_h / KM_PER_DEG
        lon = self.lon0 + self.v_east_kmh * dt_h / (KM_PER_DEG * math.cos(math.radians(self.lat0)))
        return lat, lon

    def intensity(self, t):
        x = np.clip((np.asarray(t, dtype=float) - self.t_peak) / self.life_s, -1, 1)
        return self.peak_mmh * 0.5 * (1 + np.cos(math.pi * x))

    def rate(self, lat, lon, t, intensity=None):
        clat, clon = self.centre(t)
        dn = (np.asarray(lat) - clat) * KM_PER_DEG
        de = (np.asarray(lon) - clon) * KM_PER_DEG * math.cos(math.radians(self.lat0))
        peak = self.intensity(t) if intensity is None else intensity
        return peak * np.exp(-(dn * dn + de * de) / (self.radius_km**2))


@dataclass
class RainField:
    cells: list[RainCell] = field(default_factory=list)
    freezing_level_km: float = 4.3

    def rate(self, lat, lon, t):
        out = np.zeros(np.broadcast(np.asarray(lat), np.asarray(t)).shape)
        for c in self.cells:
            out = out + c.rate(lat, lon, t)
        return out

    def attenuation(self, lat, lon, az_deg, el_deg, f_ghz, t) -> float:
        """Integrate k R^alpha along the slant path below the rain height."""
        if not self.cells or el_deg <= 0:
            return 0.0
        h = rain_height_km(self.freezing_level_km)
        plat, plon, dl = path_ground_points(lat, lon, az_deg, el_deg, h)
        r = self.rate(plat, plon, t)
        if float(r.max(initial=0.0)) < 0.05:
            return 0.0
        return float(np.sum(specific_rain_attenuation(r, f_ghz, el_deg)) * dl)


@dataclass
class Nowcast:
    """What the forecaster can know about `truth` at an issue time."""

    truth: RainField
    motion_error_kmh: float = 6.0
    qpe_error: float = 0.15
    seed: int = 7
    _err: dict = field(default_factory=dict, init=False, repr=False)

    def _cell_error(self, i: int, issue_t: float) -> tuple[float, float, float]:
        # Errors drift smoothly with issue time (a new radar volume every
        # 5 minutes refines the motion estimate) and are reproducible.
        slot = int(issue_t // 300)
        key = (i, slot)
        if key not in self._err:
            rng = np.random.default_rng(hash((self.seed, i, slot)) & 0xFFFFFFFF)
            self._err[key] = (rng.normal(0, self.motion_error_kmh), rng.normal(0, self.motion_error_kmh),
                              float(np.exp(rng.normal(0, self.qpe_error))))
        return self._err[key]

    def field_at(self, issue_t: float) -> list[tuple[RainCell, float]]:
        """Extrapolated cells: (cell with forecast motion, frozen intensity)."""
        out = []
        for i, c in enumerate(self.truth.cells):
            amp = float(c.intensity(issue_t))
            if amp < 0.2:
                # Not yet visible on radar (or already dissipated): the
                # nowcast cannot know about it.
                continue
            de, dn, qpe = self._cell_error(i, issue_t)
            lat, lon = c.centre(issue_t)
            moved = replace(c, lat0=float(lat), lon0=float(lon), t0=issue_t,
                            v_east_kmh=c.v_east_kmh + de, v_north_kmh=c.v_north_kmh + dn)
            out.append((moved, amp * qpe))
        return out

    def attenuation(self, lat, lon, az_deg, el_deg, f_ghz, t, issue_t) -> float:
        cells = self.field_at(issue_t)
        if not cells or el_deg <= 0:
            return 0.0
        h = rain_height_km(self.truth.freezing_level_km)
        plat, plon, dl = path_ground_points(lat, lon, az_deg, el_deg, h)
        r = sum(c.rate(plat, plon, t, intensity=amp) for c, amp in cells)
        if float(np.max(r)) < 0.05:
            return 0.0
        return float(np.sum(specific_rain_attenuation(r, f_ghz, el_deg)) * dl)

    def rate_grid(self, lat0, lon0, half_km, n, t, issue_t):
        """Rain-rate raster for display."""
        ax = np.linspace(-half_km, half_km, n)
        lat = lat0 + ax[:, None] / KM_PER_DEG + 0 * ax[None, :]
        lon = lon0 + 0 * ax[:, None] + ax[None, :] / (KM_PER_DEG * math.cos(math.radians(lat0)))
        return sum((c.rate(lat, lon, t, intensity=a) for c, a in self.field_at(issue_t)), np.zeros_like(lat))


class OpenMeteo:
    """Live 15-minute precipitation and freezing level (no API key needed)."""

    URL = "https://api.open-meteo.com/v1/forecast"

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(timeout=20)

    def fetch(self, points: list[tuple[float, float]]) -> list[dict]:
        r = self.client.get(self.URL, params={
            "latitude": ",".join(f"{p[0]:.4f}" for p in points),
            "longitude": ",".join(f"{p[1]:.4f}" for p in points),
            "minutely_15": "precipitation",
            "hourly": "freezing_level_height",
            "forecast_minutely_15": 8, "forecast_hours": 3, "past_minutely_15": 4,
            "timezone": "GMT", "timeformat": "unixtime",
        })
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else [data]

    @staticmethod
    def series(point: dict) -> tuple[np.ndarray, np.ndarray, float]:
        """(times, rain mm/h, freezing level km) for one point."""
        m = point.get("minutely_15", {})
        t = np.asarray(m.get("time", []), dtype=float)
        # precipitation is mm accumulated over the 15-minute step -> mm/h
        rr = np.nan_to_num(np.asarray(m.get("precipitation", []), dtype=float)) * 4.0
        fl = np.asarray(point.get("hourly", {}).get("freezing_level_height", [4300.0]), dtype=float)
        return t, rr, float(np.nanmedian(fl)) / 1000.0 if fl.size else 4.3


@dataclass
class PointNowcast:
    """Live-mode nowcast: Open-Meteo rain at each station, P.618 point method."""

    stations: dict[str, tuple[float, float]]
    data: dict[str, tuple[np.ndarray, np.ndarray, float]] = field(default_factory=dict)

    def refresh(self, om: OpenMeteo | None = None) -> None:
        om = om or OpenMeteo()
        ids = list(self.stations)
        for sid, pt in zip(ids, om.fetch([self.stations[s] for s in ids])):
            self.data[sid] = OpenMeteo.series(pt)

    def rain_rate(self, station_id: str, t: float) -> float:
        if station_id not in self.data:
            return 0.0
        ts, rr, _ = self.data[station_id]
        return float(np.interp(t, ts, rr)) if ts.size else 0.0

    def attenuation_for(self, station_id: str, lat: float, el_deg: float, f_ghz: float, t: float) -> float:
        rr = self.rain_rate(station_id, t)
        fl = self.data.get(station_id, (None, None, 4.3))[2]
        return slant_rain_attenuation_point(rr, f_ghz, el_deg, lat, rain_height_km(fl))
