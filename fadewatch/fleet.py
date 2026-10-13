"""Fleet model: ground stations, satellites, antennas (links) and demand pools.

A Constellation link entity is one ground antenna (`<station>__ant<n>`); its
`target` tag names the satellite it is tracking right now. We keep that
shape: an `Antenna` owns the RF configuration, and its target can change at
every handover.
"""
from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass, field

from .orbit import to_unix

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DEMO_DIR = os.path.join(DATA_DIR, "demo-fleet")
DEMO_TLE = os.path.join(DATA_DIR, "tle", "demo-fleet-2026-10-12.tle")


@dataclass(frozen=True)
class GroundStation:
    id: str
    name: str
    lat_deg: float
    lon_deg: float
    pool_id: str
    alt_km: float = 0.0


@dataclass(frozen=True)
class Satellite:
    id: str
    name: str

    @property
    def family(self) -> str:
        n = self.name.upper()
        if n.startswith("O3B"):
            return "O3b"
        if n.startswith("GLOBALSTAR"):
            return "Globalstar"
        return "EO"  # PROBA-2/3, Sentinel-1C, BIOMASS: X-band science downlinks


@dataclass
class Antenna:
    id: str
    station_id: str
    frequency_ghz: float
    bandwidth_mhz: float
    eirp_dbw: float
    g_t_db_k: float


@dataclass
class LinkSample:
    """One `measurement=link` record."""

    link_id: str
    target: str
    t: float
    az_deg: float
    el_deg: float
    range_km: float
    snr_db: float


@dataclass
class Pool:
    id: str
    interval_start: float
    inlet_bytes: float

    @property
    def offered_mbps(self) -> float:
        return self.inlet_bytes * 8 / 600 / 1e6


@dataclass
class Fleet:
    stations: dict[str, GroundStation]
    satellites: dict[str, Satellite]
    antennas: dict[str, Antenna]
    latest: dict[str, LinkSample]
    pools: dict[str, Pool]
    as_of: float
    source: str = "demo-fleet"
    extras: dict = field(default_factory=dict)

    def antennas_at(self, station_id: str) -> list[Antenna]:
        return [a for a in self.antennas.values() if a.station_id == station_id]

    def stations_in_pool(self, pool_id: str) -> list[GroundStation]:
        return [s for s in self.stations.values() if s.pool_id == pool_id]

    def station_family(self, station_id: str) -> str:
        """Which satellite family a station's antennas serve (from what they track)."""
        fams = {self.satellites[s.target].family for s in self.latest.values()
                if self.antennas[s.link_id].station_id == station_id and s.target in self.satellites}
        return fams.pop() if len(fams) == 1 else "mixed"

    @classmethod
    def load_demo(cls, data_dir: str = DEMO_DIR) -> "Fleet":
        return cls.from_topology(json.load(open(os.path.join(data_dir, "topology.json"), encoding="utf-8")),
                                 satellites_csv=os.path.join(data_dir, "satellites.csv"),
                                 pools_csv=os.path.join(data_dir, "demand_pools.csv"))

    @classmethod
    def from_topology(cls, topo: dict, satellites_csv: str | None = None, pools_csv: str | None = None,
                      source: str = "demo-fleet") -> "Fleet":
        """Build from a GET /topology response (or the dataset's topology.json)."""
        stations, sats, ants, latest, pools = {}, {}, {}, {}, {}
        for e in topo.get("entities", []):
            m, eid, tags, f = e["measurement"], e["entity_id"], e.get("tags", {}), e.get("fields", {})
            if m == "ground_station":
                stations[eid] = GroundStation(eid, f.get("name", eid), float(f.get("lat_deg", f.get("lat", 0))),
                                              float(f.get("lon_deg", f.get("lon", 0))), tags.get("pop_id", ""))
            elif m == "satellite":
                sats[eid] = Satellite(eid, f.get("name", eid))
            elif m == "link":
                src = tags.get("source") or eid.split("__")[0]
                ants[eid] = Antenna(eid, src, float(f.get("frequency_ghz", 20.0)), float(f.get("bandwidth_mhz", 36.0)),
                                    float(f.get("eirp_dbw", 50.0)), float(f.get("g_t_db_k", 20.0)))
                if "snr_db" in f:
                    latest[eid] = LinkSample(eid, tags.get("target", ""), to_unix(e["observed_at"]),
                                             float(f.get("azimuth_deg", 0)), float(f.get("elevation_deg", 0)),
                                             float(f.get("slant_range_km", 0)), float(f["snr_db"]))
            elif m in ("pool_interval", "demand_pool", "telemetry") and "inlet_bytes" in f:
                pools[eid] = Pool(eid, to_unix(e["observed_at"]), float(f["inlet_bytes"]))
        if satellites_csv and os.path.exists(satellites_csv):
            for r in csv.DictReader(open(satellites_csv, encoding="utf-8")):
                sats.setdefault(r["entity_id"], Satellite(r["entity_id"], r["name"]))
        if pools_csv and os.path.exists(pools_csv):
            for r in csv.DictReader(open(pools_csv, encoding="utf-8")):
                pools.setdefault(r["pool_id"], Pool(r["pool_id"], to_unix(r["interval_start"]), float(r["inlet_bytes"])))
        for s in latest.values():
            sats.setdefault(s.target, Satellite(s.target, s.target))
        as_of = to_unix(topo["as_of_utc"]) if topo.get("as_of_utc") else max((s.t for s in latest.values()), default=0.0)
        return cls(stations, sats, ants, latest, pools, as_of, source)
