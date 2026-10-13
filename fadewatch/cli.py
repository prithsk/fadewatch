"""fadewatch: command line.

    fadewatch findings            check the demo fleet against SGP4 and a link budget
    fadewatch watch               run the agent over the demo scenario, printing alerts as they appear
    fadewatch backtest            score Fadewatch vs the persistence baseline (writes data/scorecard.json)
    fadewatch site [DIR]          build the static dashboard (default: web/) and its replay bundle
    fadewatch serve               build if needed and serve the dashboard on http://127.0.0.1:8765
    fadewatch mcp                 run the Fadewatch MCP server (stdio)
    fadewatch live                run on your Constellation account (needs CONSTELLATION_API_KEY)
    fadewatch stream              replay the demo scenario into your Constellation account in real time
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .orbit import iso


def _utf8() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def cmd_findings(args) -> None:
    from .scenarios import load_context
    ctx = load_context()
    print("Pointing vs SGP4 (CelesTrak elements):")
    for a in ctx.alignment:
        sat = ctx.fleet.satellites[a.sat_id].name
        off = "" if a.offset_s == 0 else f"  replay offset {a.offset_s / 86400:+.4f} d"
        print(f"  {a.link_id:26s} {sat:18s} az {a.az_err_deg:5.2f}°  el {a.el_err_deg:5.2f}°  range {a.range_err_km:6.1f} km{off}")
    print("\nLink-budget calibration per station:")
    for c in ctx.cal.values():
        name = ctx.fleet.stations[c.station_id].name
        rms = "n/a" if c.rms_db != c.rms_db else f"{c.rms_db:.4f} dB"
        print(f"  {name:18s} loss {c.loss_db:7.3f} dB  zenith gas {c.zenith_gas_db:.3f} dB  RMS {rms}  ({c.n} links, {c.method})")


def cmd_watch(args) -> None:
    from .backtest import run_engine
    from .scenarios import SCENARIOS, load_context
    from .simulate import Simulator
    ctx = load_context()
    cfg = SCENARIOS[args.scenario](ctx.fleet)
    rec = Simulator(ctx.fleet, ctx.prop, ctx.cal, cfg).run()
    _, log = run_engine(ctx, cfg, rec)
    seen = set()
    for t, alerts in zip(log.t, log.alerts):
        for a in alerts:
            key = (a["id"], a["severity"])
            if key in seen or a["severity"] == "info" and not args.all:
                continue
            seen.add(key)
            when = f"in {(a['event_t'] - t) / 60:5.1f} min" if a["event_t"] and a["event_t"] > t else "now        "
            print(f"{iso(t)[11:19]}  {a['severity']:8s} {when}  {a['title']}")
            if args.verbose:
                print(f"           {a['detail']}\n           Do: {a['action']}")


def cmd_backtest(args) -> None:
    from .site import build_bundle
    from .session import SCORECARD_PATH
    bundle, res = build_bundle(args.scenario)
    sc = bundle["scorecard"]
    os.makedirs(os.path.dirname(SCORECARD_PATH), exist_ok=True)
    json.dump(sc, open(SCORECARD_PATH, "w", encoding="utf-8"), indent=1)
    s = sc["summary"]
    print(f"\n{'event':10s} {'n':>3s}  {'Fadewatch warned':>18s}  {'median lead':>11s}  {'persistence':>11s}")
    for k, v in s.items():
        print(f"{k:10s} {v['events']:3d}  {v['fadewatch_warned_60s']:>14d}/{v['events']:<3d}  "
              f"{v['fadewatch_median_lead_min']:8.1f} min  {v['persistence_warned_60s']:>8d}/{v['events']}")
    print(f"\nAlert precision: {sc['alerts']['confirmed']}/{sc['alerts']['confirmed'] + sc['alerts']['false']}")
    print("\nMAE at 5 min (dB): " + ", ".join(
        f"{r} {sc['skill']['fadewatch'][r]['5min']['mae_db']:.2f} vs {sc['skill']['persistence'][r]['5min']['mae_db']:.2f}"
        for r in ("all", "steady", "rain", "equipment") if r in sc["skill"]["fadewatch"]))
    print(f"\nwrote {SCORECARD_PATH}")


def cmd_site(args) -> None:
    from .site import WEB_DIR, build_bundle, export_site
    if args.out and os.path.abspath(args.out) != os.path.abspath(WEB_DIR):
        export_site(args.out, args.scenario)
        return
    bundle, _ = build_bundle(args.scenario)
    os.makedirs(os.path.join(WEB_DIR, "data"), exist_ok=True)
    path = os.path.join(WEB_DIR, "data", f"{args.scenario}.json")
    json.dump(bundle, open(path, "w", encoding="utf-8"), separators=(",", ":"))
    print(f"wrote {path}")


def cmd_serve(args) -> None:
    import functools
    import http.server
    from .site import WEB_DIR
    if not os.path.exists(os.path.join(WEB_DIR, "data", "demo.json")):
        args.out, args.scenario = None, "demo"
        cmd_site(args)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=WEB_DIR)
    print(f"Fadewatch dashboard on http://127.0.0.1:{args.port}  (add ?tour for the guided replay)")
    http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler).serve_forever()


def cmd_mcp(args) -> None:
    from .mcp_server import main
    main()


def cmd_live(args) -> None:
    from .live import run_live
    rep = run_live(load_demo=not args.no_load, spend_prediction=args.spend_prediction, write_back=not args.no_write)
    out = args.out or "live-report.json"
    json.dump(rep, open(out, "w", encoding="utf-8"), indent=1, default=str)
    print(f"\n{len(rep.get('alerts', []))} alerts; report in {out}")
    for a in rep.get("alerts", [])[:12]:
        print(f"  {a['severity']:8s} {a['title']}")
    sk = rep.get("skill_recorded", {})
    if sk:
        print("\nSkill on recorded telemetry (MAE dB):")
        for m, v in sk.items():
            print(f"  {m:12s} " + "  ".join(f"{h}: {x['mae_db']:.3f}" for h, x in sorted(v.items())))


def cmd_stream(args) -> None:
    from .live import stream_scenario
    from .scenarios import SCENARIOS, load_context
    from .simulate import Simulator
    ctx = load_context()
    cfg = SCENARIOS[args.scenario](ctx.fleet)
    rec = Simulator(ctx.fleet, ctx.prop, ctx.cal, cfg).run()
    stream_scenario(rec, ctx.fleet, speed=args.speed)


def main(argv: list[str] | None = None) -> None:
    _utf8()
    p = argparse.ArgumentParser(prog="fadewatch", description="Physics-informed early warning for satellite link fades.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("findings", help="check the demo fleet against SGP4 and a link budget").set_defaults(fn=cmd_findings)
    w = sub.add_parser("watch", help="run the agent over a scenario and print alerts as they appear")
    w.add_argument("--scenario", default="demo")
    w.add_argument("-v", "--verbose", action="store_true")
    w.add_argument("--all", action="store_true", help="include info-level handover notices")
    w.set_defaults(fn=cmd_watch)
    b = sub.add_parser("backtest", help="score against the persistence baseline")
    b.add_argument("--scenario", default="demo")
    b.set_defaults(fn=cmd_backtest)
    s = sub.add_parser("site", help="build the dashboard bundle")
    s.add_argument("out", nargs="?")
    s.add_argument("--scenario", default="demo")
    s.set_defaults(fn=cmd_site)
    v = sub.add_parser("serve", help="serve the dashboard")
    v.add_argument("--port", type=int, default=8765)
    v.set_defaults(fn=cmd_serve)
    sub.add_parser("mcp", help="run the MCP server (stdio)").set_defaults(fn=cmd_mcp)
    lv = sub.add_parser("live", help="run on your Constellation account")
    lv.add_argument("--no-load", action="store_true", help="skip load_demo_fleet")
    lv.add_argument("--no-write", action="store_true", help="do not write forecasts back as telemetry")
    lv.add_argument("--spend-prediction", action="store_true",
                    help="spend one GET /predictions call to compare snr-baseline-v1 with the replica")
    lv.add_argument("--out")
    lv.set_defaults(fn=cmd_live)
    st = sub.add_parser("stream", help="replay a scenario into Constellation in real time")
    st.add_argument("--scenario", default="demo")
    st.add_argument("--speed", type=float, default=1.0)
    st.set_defaults(fn=cmd_stream)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
