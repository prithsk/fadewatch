import math

import pytest

from fadewatch.atmosphere import (_AH, _AV, _KH, _KV, _p838, rain_coefficients, scintillation_sigma_db,
                                  slant_rain_attenuation_point)
from fadewatch.linkbudget import CLOSURE_THRESHOLD_DB, capacity_mbps, clear_sky_snr, fspl_db, modcod_for
from fadewatch.orbit import to_unix
from fadewatch.scenarios import load_context
from fadewatch.weather import RainCell, RainField


@pytest.fixture(scope="module")
def ctx():
    return load_context()


@pytest.mark.parametrize("f, kh, ah, kv, av", [
    # ITU-R P.838-3 Table 5 values
    (10, 0.01217, 1.2571, 0.01129, 1.2156),
    (20, 0.09164, 1.0568, 0.09611, 0.9847),
    (30, 0.2403, 0.9485, 0.2291, 0.9129),
])
def test_p838_matches_recommendation_table(f, kh, ah, kv, av):
    assert 10 ** _p838(_KH, f) == pytest.approx(kh, rel=2e-3)
    assert _p838(_AH, f) == pytest.approx(ah, abs=2e-3)
    assert 10 ** _p838(_KV, f) == pytest.approx(kv, rel=2e-3)
    assert _p838(_AV, f) == pytest.approx(av, abs=2e-3)


def test_rain_hurts_ka_far_more_than_c_band():
    ka = slant_rain_attenuation_point(30, 19.05, 30, 34.2, 4.8)
    c = slant_rain_attenuation_point(30, 6.96, 30, 31.8, 4.8)
    assert ka > 10 * c > 0
    assert slant_rain_attenuation_point(30, 19.05, 10, 34.2, 4.8) > ka  # longer path at low elevation


def test_circular_polarisation_sits_between_h_and_v():
    k, a = rain_coefficients(20, el_deg=30)
    assert 0.09164 < k < 0.09611


def test_scintillation_grows_toward_horizon():
    assert scintillation_sigma_db(19, 10) > 3 * scintillation_sigma_db(19, 40)


def test_fspl_reference_value():
    # 20 GHz at 1,000 km: 92.45 + 26.02 + 60 = 178.47 dB
    assert fspl_db(20, 1000) == pytest.approx(178.47, abs=0.01)


def test_o3b_pointing_matches_sgp4(ctx):
    o3b = [a for a in ctx.alignment if ctx.fleet.satellites[a.sat_id].family == "O3b"]
    assert len(o3b) == 16
    for a in o3b:
        assert a.offset_s == 0
        assert a.el_err_deg < 0.2 and a.az_err_deg < 0.25


def test_leo_segment_is_a_replay_25_5_days_old(ctx):
    leo = [a for a in ctx.alignment if ctx.fleet.satellites[a.sat_id].name.startswith(("GLOBALSTAR", "PROBA-3"))]
    assert len(leo) == 4
    for a in leo:
        assert a.offset_s == pytest.approx(-25.5 * 86400)
        assert a.matched


def test_calibrated_budget_reproduces_every_snapshot_snr(ctx):
    for lid, s in ctx.fleet.latest.items():
        ant = ctx.fleet.antennas[lid]
        pred = float(clear_sky_snr(ant, ctx.cal[ant.station_id], s.range_km, s.el_deg))
        assert pred == pytest.approx(s.snr_db, abs=0.03), lid


def test_zenith_gas_rises_with_frequency(ctx):
    by_name = {ctx.fleet.stations[c.station_id].name: c for c in ctx.cal.values()}
    assert by_name["CLFN-1"].zenith_gas_db < by_name["O3B1.WMP"].zenith_gas_db < by_name["Texas Gateway"].zenith_gas_db


def test_acm_capacity_and_closure():
    assert capacity_mbps(30, 20.1535) == pytest.approx(20.1535 / 1.05 * 4.453, rel=1e-6)
    assert capacity_mbps(CLOSURE_THRESHOLD_DB - 0.1, 100) == 0
    assert modcod_for(CLOSURE_THRESHOLD_DB + 0.01)[0] == "QPSK 1/4"


def test_rain_cell_moves_and_lives():
    t0 = to_unix("2026-10-10T23:00:00Z")
    c = RainCell(34.0, -99.0, t0, v_east_kmh=60, v_north_kmh=0, peak_mmh=40, radius_km=6, t_peak=t0, life_s=1800)
    lat, lon = c.centre(t0 + 3600)
    assert (lon + 99.0) * 111.32 * math.cos(math.radians(34)) == pytest.approx(60, rel=1e-3)
    assert c.intensity(t0) == 40 and c.intensity(t0 + 1800) == pytest.approx(0, abs=1e-9)
    f = RainField([c])
    # a path pointing into the cell sees far more rain than one pointing away
    into = f.attenuation(34.0, -99.05, 90, 30, 19.05, t0)
    away = f.attenuation(34.0, -99.05, 270, 30, 19.05, t0)
    assert into > 3 * away > 0
