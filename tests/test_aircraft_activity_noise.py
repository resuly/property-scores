"""noise_score with the aircraft-activity flag: off is a no-op, show adds only
the counts block, estimate fills the aircraft term only where no official
contour applies.

Road/rail/buildings are stubbed out (clean checkout), the aircraft gate is the
real aircraft_noise_penalty with only its remote VicPlan query stubbed, and
the overflight data is the real-data fixture (tests/test_aircraft_activity.py).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from property_scores.noise import aircraft

FIX = Path(__file__).parent / "fixtures" / "aircraft_activity_20261128"
KEILOR = (-37.7184, 144.847)
HOBART = (-42.88, 147.33)
MAEO_HIT = {"anef_min": 20, "zone_code": "MAEO2", "airfield": "Melbourne Airport",
            "source": "vicplan", "lga": "BRIMBANK"}


@pytest.fixture
def ns(monkeypatch, request):
    from property_scores.noise import score as ns
    for name in ("aadt_near", "nfdh_near", "roads_near", "rail_near",
                 "gtfs_rail_near", "buildings_in_radius"):
        monkeypatch.setattr(ns, name, lambda *a, **k: [])
    monkeypatch.setattr(ns, "get_db", lambda *a, **k: object())
    monkeypatch.setattr(ns, "buildings_to_arrays", lambda *a, **k: None)
    monkeypatch.setattr(ns, "barrier_attenuation", lambda *a, **k: 0.0)
    monkeypatch.setattr(ns, "terrain_attenuation", lambda *a, **k: 0.0)
    monkeypatch.setattr(ns, "_cache_get", lambda *a, **k: None)
    monkeypatch.setattr(ns, "_cache_put", lambda *a, **k: None)
    monkeypatch.setattr(aircraft, "_query_defence", lambda lat, lng: None)
    monkeypatch.setenv("AIRCRAFT_ACTIVITY_DIR", str(FIX))
    aircraft._aircraft_cached.cache_clear()
    request.addfinalizer(aircraft._aircraft_cached.cache_clear)
    return ns


def _vic(monkeypatch, hit):
    monkeypatch.setattr(aircraft, "_query_vic", lambda lat, lng: hit)
    aircraft._aircraft_cached.cache_clear()


def test_off_never_touches_the_data(monkeypatch, ns):
    from property_scores.aircraft_activity import noise as aan
    monkeypatch.setattr(ns, "_AA_MODE", "off")

    def boom(*a, **k):
        raise AssertionError("activity_at must not run with the flag off")
    monkeypatch.setattr(aan, "activity_at", boom)
    _vic(monkeypatch, None)
    r = ns.noise_score(*KEILOR, 500)
    assert "aircraft_activity" not in r and "aircraft_estimate" not in r
    assert "aircraft_db" not in r


def test_off_signatures_are_the_pre_change_strings():
    """Default-config strings must equal what the code produced before this
    feature, or every downstream cache would flush on deploy."""
    from property_scores.noise import score as ns
    if ns._AA_MODE != "off":
        pytest.skip("suite run with NOISE_AIRCRAFT_ACTIVITY set")
    assert ":aa-" not in ns._CONFIG_SIG
    assert "-aa1" not in ns.NOISE_MODEL_VERSION
    assert ns._CONFIG_SIG.split(":")[1:3] == ["src2", "air2"]
    assert len(ns._CONFIG_SIG.split(":")) == 8


def test_show_adds_block_and_moves_nothing(monkeypatch, ns):
    _vic(monkeypatch, None)
    monkeypatch.setattr(ns, "_AA_MODE", "off")
    off = ns.noise_score(*KEILOR, 500)
    monkeypatch.setattr(ns, "_AA_MODE", "show")
    show = ns.noise_score(*KEILOR, 500)
    block = show.pop("aircraft_activity")
    assert show == off
    assert block["status"] == "ok" and block["flights_per_day"] > 100
    assert block["affects_score"] is False


def test_estimate_fills_only_without_official_contour(monkeypatch, ns):
    monkeypatch.setattr(ns, "_AA_MODE", "off")
    _vic(monkeypatch, None)
    off = ns.noise_score(*KEILOR, 500)
    monkeypatch.setattr(ns, "_AA_MODE", "estimate")
    est = ns.noise_score(*KEILOR, 500)
    assert est["aircraft_activity"]["affects_score"] is False  # block never moves a number
    e = est["aircraft_estimate"]
    assert est["aircraft_db"] == e["leq_db"]
    # the three period Leqs recombine into the estimate's Lden
    import math
    lden = 10 * math.log10((12 * 10 ** (e["leq_day_db"] / 10)
                            + 4 * 10 ** ((e["leq_evening_db"] + 5) / 10)
                            + 8 * 10 ** ((e["leq_night_db"] + 10) / 10)) / 24)
    assert lden == pytest.approx(e["lden_db"], abs=0.1)
    # no road or rail here: each period level is the estimate's own period
    # Leq (floored at ambient), not one 24 h Leq copied into all three
    assert est["leq_night_db"] == pytest.approx(max(e["leq_night_db"], 35.0), abs=0.2)
    assert est["leq_day_db"] == pytest.approx(max(e["leq_day_db"], 35.0), abs=0.2)
    assert e["leq_day_db"] != e["leq_night_db"]
    assert est["lden_db"] > off["lden_db"] and est["score"] <= off["score"]
    assert est["dominant_source"] == "aircraft"
    assert "observed overflights" in est["confidence_note"]
    assert e["licence"] == "ODbL-1.0"

    # inside an official contour: the official term stands, estimate not used
    _vic(monkeypatch, MAEO_HIT)
    monkeypatch.setattr(ns, "_AA_MODE", "off")
    off_c = ns.noise_score(*KEILOR, 500)
    monkeypatch.setattr(ns, "_AA_MODE", "estimate")
    est_c = ns.noise_score(*KEILOR, 500)
    block = est_c.pop("aircraft_activity")
    assert "aircraft_estimate" not in est_c
    assert est_c == off_c
    # the counts block is identical on both sides of the contour edge, so it
    # cannot be used to trace a withheld contour
    assert block == est["aircraft_activity"]

    # excluded-aircraft grids never get the estimate either
    _vic(monkeypatch, None)
    ex = ns.noise_score(*KEILOR, 500, exclude_aircraft=True)
    assert "aircraft_estimate" not in ex and ex.get("aircraft_excluded") is True


def test_insufficient_city_is_untouched(monkeypatch, ns):
    monkeypatch.setattr(ns, "_AA_MODE", "off")
    off = ns.noise_score(*HOBART, 500)
    monkeypatch.setattr(ns, "_AA_MODE", "estimate")
    est = ns.noise_score(*HOBART, 500)
    block = est.pop("aircraft_activity")
    assert block["status"] == "insufficient" and block["flights_per_day"] is None
    assert est == off
