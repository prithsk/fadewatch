from fadewatch.fleet import Fleet
from fadewatch.site import short_alert


def test_hud_labels_are_short_and_specific():
    fleet = Fleet.load_demo()
    los = {"kind": "LOS", "cause": "geometry", "link_id": "SITE-0da7faea0f45__ant0", "pool_id": "TMPEAZX1",
           "evidence": {"plan": {"mode": "break-before-make", "successor_name": "O3B-MPOWER-F2", "gap_s": 75.0}}}
    assert short_alert(los, fleet) == ("PHX·A0", "LOS", "slew → O3B-MPOWER-F2  75 s gap")
    mbb = dict(los, link_id="SITE-5c44ebc4a84e__ant7",
               evidence={"plan": {"mode": "make-before-break", "antenna": "SITE-5c44ebc4a84e__ant1", "successor_name": "O3B-FM15"}})
    assert short_alert(mbb, fleet) == ("WMP·A7", "LOS", "MBB  A1 → O3B-FM15")
    cap = {"kind": "CAPACITY", "cause": "capacity", "link_id": None, "pool_id": "DLLSTXX1",
           "evidence": {"min_capacity_mbps": 62.4, "demand_p99_mbps": 124.3, "diversity": None}}
    assert short_alert(cap, fleet) == ("DLLSTXX1", "CAPACITY", "62 / 124 Mbps")
    rain = {"kind": "FADE", "cause": "rain", "link_id": "SITE-7576272aee77__ant1", "pool_id": "DLLSTXX1",
            "evidence": {"rain_db": 22.4, "capacity_mbps": [85, 43]}}
    code, tag, act = short_alert(rain, fleet)
    assert (code, tag) == ("TXG·A1", "RAIN FADE") and act.startswith("−22 dB")
    for label in (code, tag, act):
        assert len(label) <= 40
