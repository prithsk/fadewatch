"""Fadewatch MCP server (stdio).

Run next to Constellation's own MCP server so an agent can combine them:
Constellation for the fleet's telemetry, topology and served forecasts;
Fadewatch for what physics says happens next and what to do about it.

    claude mcp add fadewatch -- fadewatch mcp
    claude mcp add --transport http constellation https://api.constellation.space/mcp \\
        --header "Authorization: Bearer $CONSTELLATION_API_KEY"

The server replays the demo-fleet scenario on its own clock. Move the clock
with `set_time`, then ask what is coming.
"""
from __future__ import annotations

import threading

try:  # MCP Python SDK 2.x
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # 1.x
    from mcp.server.fastmcp import FastMCP

from .session import ReplaySession, load_scorecard

mcp = FastMCP(
    "fadewatch",
    instructions=(
        "Fadewatch forecasts satellite link SNR from orbital geometry, a calibrated link budget and a rain "
        "nowcast, and warns before links drop. It runs a replay of Constellation's demo fleet (6 ground "
        "stations, 21 antennas, O3b/Globalstar/EO satellites) with a storm, a failing LNA and an "
        "interferer. Start with fleet_overview or predicted_drops; use set_time to move the replay clock."
    ),
)

_session: ReplaySession | None = None
_lock = threading.Lock()


def session() -> ReplaySession:
    global _session
    with _lock:
        if _session is None:
            _session = ReplaySession("demo")
            _session.set_time(minutes_after_start=15)
        return _session


@mcp.tool()
def set_time(minutes_after_start: float | None = None, iso_time: str | None = None) -> dict:
    """Move the replay clock. The replay starts at the demo-fleet snapshot (2026-10-10T23:07:00Z)
    and runs 75 minutes. Give minutes_after_start (0-75) or an ISO-8601 UTC time."""
    s = session()
    s.set_time(iso_time, minutes_after_start)
    return s.clock()


@mcp.tool()
def fleet_overview() -> dict:
    """Every station and antenna at the current replay time: what each antenna tracks, SNR,
    elevation and MODCOD, plus each demand pool's capacity against demand."""
    return session().overview()


@mcp.tool()
def predicted_drops(window_minutes: float = 30, min_severity: str = "warning") -> dict:
    """Links and pools Fadewatch expects to fail within the window, soonest and most severe first:
    loss of signal (satellite setting), fades below closure (rain or equipment), equipment
    degradation, interference and pool capacity shortfalls. Each has a cause, the predicted time,
    how long Fadewatch has been warning, and a recommended action. min_severity: critical|warning|info."""
    return session().alerts(window_minutes, min_severity)


@mcp.tool()
def forecast_link(link_id: str, horizons_minutes: list[float] | None = None) -> dict:
    """SNR forecast for one antenna link (e.g. SITE-7576272aee77__ant1): Fadewatch physics
    p10/p50/p90 out to 30 min next to the persistence baseline (snr-baseline-v1 replica) at
    1/3/5 min, with the elevation, rain and equipment-trend terms behind each horizon."""
    return session().forecast(link_id, horizons_minutes)


@mcp.tool()
def explain_link(link_id: str) -> dict:
    """Link budget for one link right now: EIRP, G/T, free-space loss, gas, rain, calibrated
    station losses and bandwidth adding up to the expected SNR, against the observed SNR."""
    return session().link_budget(link_id)


@mcp.tool()
def handover_plans(station_id: str | None = None) -> dict:
    """Upcoming losses of signal with the successor satellite chosen for each, and whether a
    spare antenna allows make-before-break (no gap); plus rain-diversity acquisitions."""
    return session().handovers(station_id)


@mcp.tool()
def pool_outlook() -> dict:
    """Each demand pool's projected capacity (ACM throughput of its links, every 5 min out to
    30 min) against its demand p50/p99, and any predicted shortfall window."""
    return session().pools()


@mcp.tool()
def fleet_findings() -> dict:
    """What Fadewatch learned from the demo-fleet snapshot: per-link agreement with SGP4
    (including the -25.5 day replay offset of the LEO/HEO links) and per-station link-budget
    calibration residuals."""
    return session().findings()


@mcp.tool()
def backtest_scorecard() -> dict:
    """Backtest of Fadewatch against the persistence baseline on the demo scenario: event lead
    times, forecast skill at 1/3/5 min by regime, alert precision and closed-loop outage avoided."""
    sc = load_scorecard()
    return sc or {"error": "No scorecard yet: run `fadewatch backtest` first."}


def main() -> None:
    threading.Thread(target=session, daemon=True).start()  # warm the replay while the client connects
    mcp.run()


if __name__ == "__main__":
    main()
