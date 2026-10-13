import numpy as np
import pytest

from fadewatch.backtest import evaluate, run_engine, summarize
from fadewatch.forecast import PersistenceBaseline, PhysicsForecaster
from fadewatch.scenarios import demo, load_context, quiet
from fadewatch.simulate import Simulator
from fadewatch.weather import Nowcast


@pytest.fixture(scope="module")
def ctx():
    return load_context()


@pytest.fixture(scope="module")
def demo_run(ctx):
    cfg = demo(ctx.fleet)
    rec = Simulator(ctx.fleet, ctx.prop, ctx.cal, cfg).run()
    eng, log = run_engine(ctx, cfg, rec)
    return cfg, rec, eng, log


def test_persistence_replica_matches_documented_example():
    from fadewatch.fleet import LinkSample
    s = LinkSample("sat-1-gs-1", "x", 0.0, 180, 45, 1200, 12.5)
    f = PersistenceBaseline().forecast("sat-1-gs-1", [s], 30.0, np.array([60.0]))
    assert f.p50[0] == 12.5
    assert f.p10[0] == pytest.approx(12.166304, abs=5e-4)
    assert f.p90[0] == pytest.approx(12.833696, abs=5e-4)


def test_persistence_declines_without_recent_telemetry():
    from fadewatch.fleet import LinkSample
    s = LinkSample("l", "x", 0.0, 180, 45, 1200, 12.5)
    assert PersistenceBaseline().forecast("l", [s], 300.0).error == "insufficient_telemetry"


def test_physics_forecaster_predicts_loss_of_signal(ctx, demo_run):
    cfg, rec, _, _ = demo_run
    fc = PhysicsForecaster(ctx.fleet, ctx.prop, ctx.cal, Nowcast(cfg.rain))
    los = next(e for e in rec.events if e["type"] == "los" and e["link_id"] == "SITE-0da7faea0f45__ant0")
    issue = los["t"] - 600
    f = fc.forecast("SITE-0da7faea0f45__ant0", rec.history("SITE-0da7faea0f45__ant0", issue), issue)
    assert f.los_t is not None
    assert abs(f.los_t - los["t"]) < 30  # within two 15 s samples of when the antenna actually lost it


def test_alerts_raised_before_every_loss_of_signal(demo_run):
    _, rec, _, log = demo_run
    for e in rec.events:
        if e["type"] != "los" or e["t"] <= log.t[0] + 60:
            continue
        before = [i for i, t in enumerate(log.t) if t < e["t"]][-1]
        assert any(a["kind"] == "LOS" and a["link_id"] == e["link_id"] for a in log.alerts[before]), e


def test_equipment_drift_is_attributed_to_equipment(demo_run):
    _, _, eng, log = demo_run
    kinds = {(a["kind"], a["cause"]) for alerts in log.alerts for a in alerts
             if a["link_id"] == "SITE-0da7faea0f45__ant0"}
    assert ("DEGRADATION", "equipment") in kinds


def test_interference_is_a_step_not_a_drift(demo_run):
    _, _, _, log = demo_run
    ant7 = [a for alerts in log.alerts for a in alerts if a["link_id"] == "SITE-5c44ebc4a84e__ant7"]
    assert any(a["kind"] == "INTERFERENCE" for a in ant7)
    assert not any(a["kind"] == "DEGRADATION" for a in ant7)


def test_quiet_scenario_has_no_false_warnings(ctx):
    res = evaluate(ctx, quiet(ctx.fleet), closed_loop=False)
    assert res["alerts"]["false"] == []


def test_demo_backtest_headline(ctx):
    res = evaluate(ctx, demo(ctx.fleet), closed_loop=True)
    s = summarize(res)
    assert s["los"]["fadewatch_warned_60s"] == s["los"]["events"]
    assert s["los"]["persistence_warned_60s"] == 0
    assert s["closure"]["fadewatch_warned_60s"] == s["closure"]["events"]
    assert res["alerts"]["false"] == []
    fw, pb = res["skill"]["fadewatch"]["all"]["5min"], res["skill"]["persistence"]["all"]["5min"]
    assert fw["mae_db"] < 0.5 * pb["mae_db"]
    assert res["closed_loop"]["pools"]["DLLSTXX1"]["unserved_gbit"] < res["open_loop"]["pools"]["DLLSTXX1"]["unserved_gbit"]
