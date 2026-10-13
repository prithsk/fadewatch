import json

import httpx
import pytest

from fadewatch.constellation import AmbiguousWrite, Constellation, ConstellationError, batch_records
from fadewatch.fleet import Fleet


def client(handler):
    api = Constellation("cos_test_key", client=httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(handler)))
    api.sleep = lambda s: None
    return api


def test_key_header_and_topology_params():
    seen = {}

    def h(req):
        seen["key"] = req.headers.get("x-api-key")
        seen["q"] = dict(req.url.params)
        return httpx.Response(200, json={"as_of_utc": "2026-10-10T23:07:10Z", "entities": []})

    client(h).topology("link", 900)
    assert seen["key"] == "cos_test_key"
    assert seen["q"] == {"freshness_seconds": "900", "measurement": "link"}


def test_topology_rejects_unknown_measurement_locally():
    with pytest.raises(ValueError):
        client(lambda r: httpx.Response(200, json={})).topology("pool_interval")


def test_predictions_spends_one_call_for_all_links():
    calls = []

    def h(req):
        calls.append(req.url.params.get_list("link_ids"))
        return httpx.Response(200, json={"items": [], "entity_errors": []})

    client(h).predictions(["a", "b", "a", "c"])
    assert calls == [["a", "b", "c"]]


def test_rate_limit_honours_retry_after_then_succeeds():
    n = {"i": 0}
    slept = []

    def h(req):
        n["i"] += 1
        if n["i"] == 1:
            return httpx.Response(429, json={"error": "rate_limited", "retry_after_seconds": 21}, headers={"Retry-After": "21"})
        return httpx.Response(200, json={"ok": True})

    api = client(h)
    api.sleep = slept.append
    assert api.topology() == {"ok": True}
    assert slept and slept[0] >= 21  # never shorter than the server asked


def test_auth_lockout_is_not_retried():
    n = {"i": 0}

    def h(req):
        n["i"] += 1
        return httpx.Response(429, json={"error": "auth_lockout"})

    with pytest.raises(ConstellationError) as e:
        client(h).topology()
    assert e.value.code == "auth_lockout" and n["i"] == 1


def test_auth_failure_stops_immediately():
    n = {"i": 0}

    def h(req):
        n["i"] += 1
        return httpx.Response(401, json={"error": "auth_failed"})

    with pytest.raises(ConstellationError):
        client(h).predictions(["x"])
    assert n["i"] == 1


def test_telemetry_timeout_is_ambiguous_not_replayed():
    n = {"i": 0}

    def h(req):
        n["i"] += 1
        raise httpx.ReadTimeout("timed out", request=req)

    with pytest.raises(AmbiguousWrite):
        client(h).send_telemetry([{"measurement": "link", "time": "2026-10-10T23:07:00Z",
                                   "tags": {"entity_id": "x"}, "fields": {"snr_db": 1.0}}])
    assert n["i"] == 1


def test_batching_respects_count_and_size():
    recs = [{"measurement": "link", "time": "t", "tags": {"entity_id": f"l{i}"}, "fields": {"snr_db": 1.0}} for i in range(2500)]
    batches = batch_records(recs)
    assert [len(b) for b in batches] == [1000, 1000, 500]
    big = [{"measurement": "telemetry", "time": "t", "tags": {"entity_id": "x"}, "fields": {"s": "x" * 5000}} for _ in range(400)]
    for b in batch_records(big):
        assert len(json.dumps(b)) < 1_048_576


def test_series_follows_truncation():
    pages = [
        {"series": [{"entity_id": "a", "times": ["t1"], "channels": {"snr_db": [1.0]}, "point_count": 1}],
         "truncated": True, "next_start": "t2"},
        {"series": [{"entity_id": "a", "times": ["t2"], "channels": {"snr_db": [2.0]}, "point_count": 1}],
         "truncated": False, "next_start": None},
    ]
    starts = []

    def h(req):
        starts.append(req.url.params.get("start"))
        return httpx.Response(200, json=pages[len(starts) - 1])

    out = client(h).series(["a"], "t0")
    assert starts == ["t0", "t2"]
    assert out["series"][0]["times"] == ["t1", "t2"] and out["series"][0]["channels"]["snr_db"] == [1.0, 2.0]


def test_fleet_parses_the_topology_shape():
    import os
    from fadewatch.fleet import DEMO_DIR
    topo = json.load(open(os.path.join(DEMO_DIR, "topology.json"), encoding="utf-8"))
    f = Fleet.from_topology(topo)
    assert (len(f.stations), len(f.antennas), len(f.satellites), len(f.pools)) == (6, 21, 39, 4)
    assert f.station_family("SITE-d7c79f814522") == "Globalstar"


def test_writeback_records_use_canonical_measurements():
    from fadewatch.backtest import run_engine
    from fadewatch.live import writeback_records
    from fadewatch.scenarios import load_context, quiet
    from fadewatch.simulate import Simulator
    ctx = load_context()
    cfg = quiet(ctx.fleet)
    cfg.end = cfg.replay_start + 60
    rec = Simulator(ctx.fleet, ctx.prop, ctx.cal, cfg).run()
    _, log = run_engine(ctx, cfg, rec)
    recs = writeback_records(ctx.fleet, ctx.prop, log.outputs[-1], log.t[-1])
    assert {r["measurement"] for r in recs} <= {"attenuation", "telemetry", "satellite"}
    assert all(r["tags"].get("entity_id") and r["fields"] for r in recs)
    sats = [r for r in recs if r["measurement"] == "satellite"]
    assert len(sats) == 39 and all(r["fields"]["tle_line1"].startswith("1 ") for r in sats)
