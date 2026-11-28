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
    # the excluded path too: off means no estimate whatever the caller asks
    x = ns.noise_score(*KEILOR, 500, exclude_aircraft=True)
    assert "aircraft_estimate" not in x and "aircraft_db" not in x
    assert x["confidence_note"].endswith(ns._AIRCRAFT_EXCLUDED_NOTE)


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

    # exclude_aircraft (official contours not usable): the estimate applies
    # from overflight data alone; see the test below for the edge property
    _vic(monkeypatch, None)
    ex = ns.noise_score(*KEILOR, 500, exclude_aircraft=True)
    assert ex.get("aircraft_excluded") is True
    assert ex["aircraft_estimate"]["lden_db"] == e["lden_db"]


def test_insufficient_city_is_untouched(monkeypatch, ns):
    monkeypatch.setattr(ns, "_AA_MODE", "off")
    off = ns.noise_score(*HOBART, 500)
    monkeypatch.setattr(ns, "_AA_MODE", "estimate")
    est = ns.noise_score(*HOBART, 500)
    block = est.pop("aircraft_activity")
    assert block["status"] == "insufficient" and block["flights_per_day"] is None
    assert est == off


# ---- the rules added for withheld contours (2026-11-29 review P1) ----------

def _counting_vic(monkeypatch, hit):
    calls = []

    def q(lat, lng):
        calls.append((lat, lng))
        return hit
    monkeypatch.setattr(aircraft, "_query_vic", q)
    aircraft._aircraft_cached.cache_clear()
    return calls


def test_excluded_request_is_the_same_inside_and_outside_a_contour(monkeypatch, ns):
    """With exclude_aircraft the estimate must not depend on the official
    contour at all: whether it appears and how big it is are identical for a
    point inside a contour and the same point outside it. This is what stops
    a withheld contour (Perth everywhere, Brisbane on the paid API) being
    traced from the estimate's presence, size, aircraft_db or the note."""
    monkeypatch.setattr(ns, "_AA_MODE", "estimate")
    calls = _counting_vic(monkeypatch, MAEO_HIT)
    inside = ns.noise_score(*KEILOR, 500, exclude_aircraft=True)
    calls_out = _counting_vic(monkeypatch, None)
    outside = ns.noise_score(*KEILOR, 500, exclude_aircraft=True)
    assert calls == [] and calls_out == [], "excluded path read a contour"
    assert inside == outside
    e = inside["aircraft_estimate"]
    assert e["lden_db"] >= 45 and inside["aircraft_db"] == e["leq_db"]
    assert "not used for this request" in e["basis"]
    assert inside["aircraft_excluded"] is True
    assert inside["aircraft_assessment"] == "not_assessed"
    assert "aircraft" not in inside  # no contour block
    note = inside["confidence_note"]
    assert note.endswith(ns._AIRCRAFT_EXCLUDED_ESTIMATED_NOTE)
    assert "outside" not in note and "No official" not in note

    # and the sanity half: WITHOUT the flag the same two inputs differ, so the
    # equality above is the rule working, not the stub being ignored
    _vic(monkeypatch, MAEO_HIT)
    in_default = ns.noise_score(*KEILOR, 500)
    _vic(monkeypatch, None)
    out_default = ns.noise_score(*KEILOR, 500)
    assert "aircraft_estimate" not in in_default
    assert in_default["aircraft"]["zone_code"] == "MAEO2"
    # the official term, not the estimate, is what entered the level
    assert in_default["aircraft_db"] == round(
        ns.AMBIENT_DB + in_default["aircraft"]["penalty_db"], 1)
    assert out_default["aircraft_estimate"]["lden_db"] == e["lden_db"]


def _cell_point(city, i, j):
    return (city.lat0 + i * city.dlat, city.lon0 + j * city.dlon)


def test_estimate_below_the_floor_is_not_mixed(monkeypatch, ns):
    from property_scores.aircraft_activity import noise as aan
    from property_scores.aircraft_activity import store as aas
    c = aas.Store(FIX).cities["melbourne"]
    est = aan.estimate_lden(c.cell_values(0, 0), c.coverage)
    assert est and est["lden_db"] < aan.APPLY_FLOOR_DB  # really a quiet cell
    pt = _cell_point(c, 0, 0)
    _vic(monkeypatch, None)
    for kw in ({}, {"exclude_aircraft": True}):
        monkeypatch.setattr(ns, "_AA_MODE", "off")
        off = ns.noise_score(*pt, 500, **kw)
        monkeypatch.setattr(ns, "_AA_MODE", "estimate")
        on = ns.noise_score(*pt, 500, **kw)
        assert on.pop("aircraft_activity")["status"] == "ok"
        assert on == off, kw


def test_large_aircraft_only_city_is_not_estimated(monkeypatch, ns):
    """Brisbane's receivers miss light aircraft, so no estimate enters a
    level there even where the jets-only figure is above the floor."""
    from property_scores.aircraft_activity import noise as aan
    from property_scores.aircraft_activity import store as aas
    c = aas.Store(FIX).cities["brisbane"]
    assert c.coverage == aas.COVERAGE_LARGE_ONLY
    best = max(((aan.estimate_lden(c.cell_values(i, j), c.coverage)["lden_db"], i, j)
                for i in range(c.ny) for j in range(c.nx)))
    assert best[0] >= aan.APPLY_FLOOR_DB  # would have applied under coverage ok
    pt = _cell_point(c, best[1], best[2])
    monkeypatch.setattr(aircraft, "_query_qld", lambda lat, lng: None)
    monkeypatch.setattr(aircraft, "_query_gold_coast", lambda lat, lng: None)
    aircraft._aircraft_cached.cache_clear()
    for kw in ({}, {"exclude_aircraft": True}):
        monkeypatch.setattr(ns, "_AA_MODE", "off")
        off = ns.noise_score(*pt, 500, **kw)
        monkeypatch.setattr(ns, "_AA_MODE", "estimate")
        on = ns.noise_score(*pt, 500, **kw)
        block = on.pop("aircraft_activity")
        assert block["status"] == "large_aircraft_only"
        assert on == off, kw


def test_insufficient_city_is_untouched_when_excluded(monkeypatch, ns):
    monkeypatch.setattr(ns, "_AA_MODE", "off")
    off = ns.noise_score(*HOBART, 500, exclude_aircraft=True)
    monkeypatch.setattr(ns, "_AA_MODE", "estimate")
    on = ns.noise_score(*HOBART, 500, exclude_aircraft=True)
    assert on.pop("aircraft_activity")["status"] == "insufficient"
    assert on == off


def test_helicopters_never_become_db(monkeypatch, ns):
    """A cell of helicopters only (jets and light aircraft zeroed) gives no
    estimate, so no level moves however many helicopters pass."""
    from property_scores.aircraft_activity import noise as aan
    from property_scores.aircraft_activity import store as aas
    st = aas.Store(FIX)
    real = aan.activity_at

    def heli_only(lat, lng, store=None):
        block, _ = real(lat, lng, store=st)
        c = st.city_for(lat, lng)
        cell = c.cell_values(*c.cell_index(lat, lng))
        m = dict(cell["metrics"], jet_per_day=0.0, light_per_day=0.0,
                 heli_per_day=500.0)
        return block, aan.estimate_lden(dict(cell, metrics=m), c.coverage)
    monkeypatch.setattr(aan, "activity_at", heli_only)
    _vic(monkeypatch, None)
    monkeypatch.setattr(ns, "_AA_MODE", "off")
    off = ns.noise_score(*KEILOR, 500, exclude_aircraft=True)
    monkeypatch.setattr(ns, "_AA_MODE", "estimate")
    on = ns.noise_score(*KEILOR, 500, exclude_aircraft=True)
    on.pop("aircraft_activity")
    assert on == off


def test_estimate_for_level_reads_overflight_data_only():
    from property_scores.aircraft_activity import noise as aan
    est = {"lden_db": 50.0}
    assert aan.estimate_for_level({"coverage": "ok"}, est) is est
    assert aan.estimate_for_level({"coverage": "large_aircraft_only"}, est) is None
    assert aan.estimate_for_level({"coverage": "insufficient"}, est) is None
    assert aan.estimate_for_level({"coverage": "ok"}, {"lden_db": 44.9}) is None
    assert aan.estimate_for_level({"coverage": "ok"}, {"lden_db": 45.0})
    assert aan.estimate_for_level({"coverage": "ok"}, None) is None
    assert aan.estimate_for_level(None, est) is None


def test_endpoints_forward_exclude_aircraft_and_skip_the_grid(monkeypatch):
    from fastapi.testclient import TestClient
    from property_scores.api import main
    seen = []

    def fake_score(lat, lng, radius_m=500, **kw):
        seen.append(kw)
        return {"score": 50, **({"aircraft_excluded": True}
                                if kw.get("exclude_aircraft") else {})}

    def no_grid(*a, **k):
        raise AssertionError("excluded request must not read the baked grid")
    monkeypatch.setattr(main, "noise_score", fake_score)
    monkeypatch.setattr(main, "noise_cache_lookup", no_grid)
    monkeypatch.setattr(main, "_check_rate", lambda *a, **k: True)
    client = TestClient(main.app)
    r = client.get("/scores/noise", params={"lat": KEILOR[0], "lng": KEILOR[1],
                                            "exclude_aircraft": "true"})
    assert r.status_code == 200 and r.json()["aircraft_excluded"] is True
    assert seen[-1].get("exclude_aircraft") is True
    # batch component: forwarded only when set, default call unchanged
    main._noise_for_batch(*KEILOR, exclude_aircraft=True)
    assert seen[-1].get("exclude_aircraft") is True
    main._noise_for_batch(*KEILOR)
    assert "exclude_aircraft" not in seen[-1]


def test_surface_counts_estimate_nodes_only_when_present(monkeypatch):
    from property_scores.noise import surface as nsurf

    def fake(lat, lng, exclude_aircraft=False):
        return {"lden_db": 55.0, "score": 50, "lden_source": "transfer",
                **({"aircraft_estimate": {"lden_db": 50.0}}
                   if exclude_aircraft else {})}
    monkeypatch.setattr(nsurf, "noise_score", fake)
    monkeypatch.setattr(nsurf, "_coverage_gap", lambda lat, lng: False)
    on = nsurf.noise_surface(*KEILOR, radius_m=1500, cells=5,
                             require_path="transfer", exclude_aircraft=True)
    assert on["aircraft_estimate_nodes"] == 25
    off = nsurf.noise_surface(*KEILOR, radius_m=1500, cells=5,
                              require_path="transfer")
    assert "aircraft_estimate_nodes" not in off


def test_batch_with_exclude_reads_no_contour_in_any_component(monkeypatch):
    """/scores?exclude_aircraft=true: the aircraft_noise component must not
    report a contour hit either, or a consumer that strips noise.aircraft_db
    on a withheld hit (DA Leads) would re-draw the edge from it."""
    from fastapi.testclient import TestClient
    from property_scores.api import main
    hits = []

    def contour(lat, lng):
        hits.append((lat, lng))
        return {"assessment": None, "zone_code": "30-35 Zone", "source": "wa_slip"}
    monkeypatch.setattr(main, "aircraft_noise_penalty", contour)
    noise_kw = []
    monkeypatch.setattr(main, "_noise_for_batch",
                        lambda lat, lng, **kw: noise_kw.append(kw) or {"score": 50})
    for name in ("walkability_score", "_solar_with_footprint", "flood_score",
                 "bushfire_score", "heat_island_score", "view_quality_score",
                 "contamination_score"):
        monkeypatch.setattr(main, name, lambda *a, **k: {"score": 1})
    client = TestClient(main.app)
    r = client.get("/scores", params={"lat": -31.938, "lng": 115.944,
                                      "exclude_aircraft": "true"})
    body = r.json()
    assert r.status_code == 200 and hits == []
    assert body["aircraft_noise"] == main._EXCLUDED_AIRCRAFT_NOISE
    assert noise_kw[-1].get("exclude_aircraft") is True
    r = client.get("/scores", params={"lat": -31.938, "lng": 115.944})
    assert hits and r.json()["aircraft_noise"]["source"] == "wa_slip"
    assert noise_kw[-1].get("exclude_aircraft") is False


# ---- aircraft_estimate=false: no overflight feature for this request --------
# (DA Leads in WA: Perth contour withheld, numbers must not move; 2026-11-29)

@pytest.mark.parametrize("kw", [{}, {"exclude_aircraft": True}],
                         ids=["default", "exclude_aircraft"])
@pytest.mark.parametrize("mode", ["estimate", "show"])
def test_no_estimate_request_equals_the_flag_off(monkeypatch, ns, kw, mode):
    _vic(monkeypatch, None)
    monkeypatch.setattr(ns, "_AA_MODE", "off")
    off = ns.noise_score(*KEILOR, 500, **kw)
    monkeypatch.setattr(ns, "_AA_MODE", mode)
    on = ns.noise_score(*KEILOR, 500, **kw)
    # sanity: at this point the flag really changes the result
    assert "aircraft_activity" in on
    if mode == "estimate":
        assert "aircraft_estimate" in on and on["aircraft_db"] > 0
    got = ns.noise_score(*KEILOR, 500, aircraft_estimate=False, **kw)
    assert got == off


def test_no_estimate_request_never_reads_the_overflight_data(monkeypatch, ns):
    from property_scores.aircraft_activity import noise as aan

    def boom(*a, **k):
        raise AssertionError("aircraft_estimate=False must not read the data")
    monkeypatch.setattr(aan, "activity_at", boom)
    monkeypatch.setattr(ns, "_AA_MODE", "estimate")
    _vic(monkeypatch, None)
    ns.noise_score(*KEILOR, 500, aircraft_estimate=False)
    ns.noise_score(*KEILOR, 500, aircraft_estimate=False, exclude_aircraft=True)


def test_cache_keys_separate_the_no_estimate_request(monkeypatch, ns):
    keys = []
    monkeypatch.setattr(ns, "_cache_get", lambda db, key, ttl: keys.append(key) or None)
    _vic(monkeypatch, None)
    monkeypatch.setattr(ns, "_AA_MODE", "estimate")
    for kw in ({}, {"aircraft_estimate": False}, {"exclude_aircraft": True},
               {"exclude_aircraft": True, "aircraft_estimate": False}):
        ns.noise_score(*KEILOR, 500, **kw)
    assert len(set(keys)) == 4, keys
    assert keys[1] == keys[0] + ":noaa" and keys[3] == keys[2] + ":noaa"
    # flag off: the parameter changes nothing, so the key is the old one
    keys.clear()
    monkeypatch.setattr(ns, "_AA_MODE", "off")
    ns.noise_score(*KEILOR, 500)
    ns.noise_score(*KEILOR, 500, aircraft_estimate=False)
    assert keys[0] == keys[1] and not keys[0].endswith(":noaa")


def test_result_cache_never_crosses_the_two_requests(monkeypatch, ns):
    store = {}
    monkeypatch.setattr(ns, "_cache_get", lambda db, key, ttl: store.get(key))
    monkeypatch.setattr(ns, "_cache_put", lambda db, key, val: store.__setitem__(key, val))
    _vic(monkeypatch, None)
    monkeypatch.setattr(ns, "_AA_MODE", "estimate")
    a = ns.noise_score(*KEILOR, 500)                          # with estimate, cached
    b = ns.noise_score(*KEILOR, 500, aircraft_estimate=False)
    assert "aircraft_estimate" in a and "aircraft_estimate" not in b
    assert "aircraft_activity" not in b and "aircraft_db" not in b
    a2 = ns.noise_score(*KEILOR, 500)
    b2 = ns.noise_score(*KEILOR, 500, aircraft_estimate=False)
    assert a2.get("cached") and b2.get("cached")
    assert "aircraft_estimate" in a2 and "aircraft_estimate" not in b2


def test_endpoints_forward_aircraft_estimate_and_skip_the_grid(monkeypatch):
    from fastapi.testclient import TestClient
    from property_scores.api import main
    seen = []

    def fake_score(lat, lng, radius_m=500, **kw):
        seen.append(kw)
        return {"score": 50}

    def no_grid(*a, **k):
        raise AssertionError("aircraft_estimate=false must not read the grid")
    from property_scores.noise import score as ns
    monkeypatch.setattr(ns, "_AA_MODE", "estimate")
    monkeypatch.setattr(main, "noise_score", fake_score)
    monkeypatch.setattr(main, "noise_cache_lookup", no_grid)
    monkeypatch.setattr(main, "_check_rate", lambda *a, **k: True)
    client = TestClient(main.app)
    r = client.get("/scores/noise", params={"lat": KEILOR[0], "lng": KEILOR[1],
                                            "aircraft_estimate": "false"})
    assert r.status_code == 200
    assert seen[-1] == {"source": None, "aircraft_estimate": False}
    r = client.get("/scores/noise", params={"lat": KEILOR[0], "lng": KEILOR[1],
                                            "aircraft_estimate": "false",
                                            "exclude_aircraft": "true"})
    assert seen[-1] == {"source": None, "aircraft_estimate": False,
                        "exclude_aircraft": True}
    main._noise_for_batch(*KEILOR, aircraft_estimate=False)
    assert seen[-1].get("aircraft_estimate") is False
    main._noise_for_batch(*KEILOR)
    assert "aircraft_estimate" not in seen[-1]


def test_batch_endpoint_forwards_aircraft_estimate(monkeypatch):
    from fastapi.testclient import TestClient
    from property_scores.api import main
    noise_kw = []
    monkeypatch.setattr(main, "_noise_for_batch",
                        lambda lat, lng, **kw: noise_kw.append(kw) or {"score": 50})
    monkeypatch.setattr(main, "aircraft_noise_penalty", lambda lat, lng: {"assessment": "no_overlay"})
    for name in ("walkability_score", "_solar_with_footprint", "flood_score",
                 "bushfire_score", "heat_island_score", "view_quality_score",
                 "contamination_score"):
        monkeypatch.setattr(main, name, lambda *a, **k: {"score": 1})
    client = TestClient(main.app)
    client.get("/scores", params={"lat": -31.938, "lng": 115.944,
                                  "aircraft_estimate": "false"})
    assert noise_kw[-1]["aircraft_estimate"] is False
    client.get("/scores", params={"lat": -31.938, "lng": 115.944})
    assert noise_kw[-1]["aircraft_estimate"] is True


def test_surface_forwards_aircraft_estimate(monkeypatch):
    from property_scores.noise import surface as nsurf
    seen = []

    def fake(lat, lng, **kw):
        seen.append(kw)
        return {"lden_db": 55.0, "score": 50, "lden_source": "transfer"}
    monkeypatch.setattr(nsurf, "noise_score", fake)
    monkeypatch.setattr(nsurf, "_coverage_gap", lambda lat, lng: False)
    nsurf.noise_surface(*KEILOR, radius_m=1500, cells=5, require_path="transfer",
                        exclude_aircraft=True, aircraft_estimate=False)
    assert len(seen) == 25 and all(k == {"exclude_aircraft": True,
                                         "aircraft_estimate": False} for k in seen)
    seen.clear()
    nsurf.noise_surface(*KEILOR, radius_m=1500, cells=5, require_path="transfer")
    assert all(k == {} for k in seen)


def test_flag_off_no_estimate_request_still_reads_the_grid(monkeypatch):
    """With the overflight flag off, aircraft_estimate=false changes nothing,
    so /scores/noise takes the master path: the precomputed grid first."""
    from fastapi.testclient import TestClient
    from property_scores.api import main
    from property_scores.noise import score as ns
    monkeypatch.setattr(ns, "_AA_MODE", "off")
    grid_reads = []
    monkeypatch.setattr(main, "noise_cache_lookup",
                        lambda lat, lng: grid_reads.append((lat, lng)) or {"score": 77, "grid": True})
    monkeypatch.setattr(main, "noise_score",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("grid hit expected")))
    monkeypatch.setattr(main, "_check_rate", lambda *a, **k: True)
    client = TestClient(main.app)
    r = client.get("/scores/noise", params={"lat": KEILOR[0], "lng": KEILOR[1],
                                            "aircraft_estimate": "false"})
    assert r.status_code == 200 and r.json() == {"score": 77, "grid": True}
    assert len(grid_reads) == 1


def _stub_debug(monkeypatch):
    """noise_debug with its data reads stubbed; records noise_score kwargs."""
    from property_scores.noise import debug
    seen = []

    def fake_score(lat, lng, radius_m=500, **kw):
        seen.append(kw)
        return {"score": 50, "lden_db": 55.0}
    monkeypatch.setattr(debug, "noise_score", fake_score)
    monkeypatch.setattr(debug, "get_db", lambda *a, **k: object())
    for name in ("aadt_near", "nfdh_near", "gtfs_rail_near", "rail_near",
                 "roads_near", "buildings_in_radius", "_rail_shapes_near"):
        if hasattr(debug, name):
            monkeypatch.setattr(debug, name, lambda *a, **k: [])
    monkeypatch.setattr(debug, "barrier_attenuation", lambda *a, **k: 0.0)
    return seen


def test_detail_paths_forward_both_flags_to_noise_score(monkeypatch):
    """The website panel (/scores/noise?detail=true) and the paid point query
    (/scores?noise_detail=true) both end in noise_debug."""
    from fastapi.testclient import TestClient
    from property_scores.api import main
    from property_scores.noise import score as ns
    monkeypatch.setattr(ns, "_AA_MODE", "estimate")
    seen = _stub_debug(monkeypatch)
    monkeypatch.setattr(main, "_check_rate", lambda *a, **k: True)
    client = TestClient(main.app)
    r = client.get("/scores/noise", params={"lat": KEILOR[0], "lng": KEILOR[1],
                                            "detail": "true",
                                            "aircraft_estimate": "false",
                                            "exclude_aircraft": "true"})
    assert r.status_code == 200, r.text
    assert seen[-1] == {"aircraft_estimate": False, "exclude_aircraft": True}
    main._noise_for_batch(*KEILOR, detail=True, aircraft_estimate=False,
                          exclude_aircraft=True)
    assert seen[-1] == {"aircraft_estimate": False, "exclude_aircraft": True}
    main._noise_for_batch(*KEILOR, detail=True)
    assert seen[-1] == {}
