"""Noise without the aircraft term, for grids a caller may not redistribute.

DA Leads may not pass on the Brisbane (Brisbane + Archerfield) or Perth
Airport ANEF contours through its paid API (Bo 2026-11-24). A 9x9 noise grid
around an address near either airport steps up wherever a node falls inside a
contour, so the grid traces the contour's shape even with every contour field
withheld. The fix is to compute those grids with no aircraft term at all.

The aircraft gate below is the REAL aircraft_noise_penalty with only its
remote queries stubbed (the Brisbane and Perth sources are live web services),
so a hit genuinely flows through the whole noise_score chain. Coordinates are
real points inside each airport's contour extent.
"""
import pytest

from property_scores.noise import aircraft
from property_scores.noise import surface as nsurf

# Hendra, beside Brisbane Airport's runway (inside the BCC ANEF 25 band
# extent), and Redcliffe WA, beside Perth Airport (inside SPP 5.1 extent).
BNE_POINT = (-27.4187, 153.0700)
PER_POINT = (-31.9380, 115.9440)

_HITS = {
    "qld": {"anef_min": 25, "zone_code": "ANEF 25-30",
            "airfield": "Brisbane Airport", "source": "qld_bcc"},
    "wa": {"anef_min": 30, "zone_code": "30-35 Zone",
           "airfield": "Perth Airport", "source": "wa_slip"},
}


def _stub_inputs(monkeypatch, request, calls):
    """No road/rail/buildings (clean checkout), real aircraft gate with the
    two contour services answering a hit."""
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

    def q(key):
        def _q(lat, lng):
            calls.append((key, lat, lng))
            return _HITS[key]
        return _q

    monkeypatch.setattr(aircraft, "_query_qld", q("qld"))
    monkeypatch.setattr(aircraft, "_query_wa", q("wa"))
    monkeypatch.setattr(aircraft, "_query_gold_coast", lambda lat, lng: None)
    monkeypatch.setattr(aircraft, "_query_defence", lambda lat, lng: None)
    aircraft._aircraft_cached.cache_clear()
    request.addfinalizer(aircraft._aircraft_cached.cache_clear)
    return ns


@pytest.mark.parametrize("point", [BNE_POINT, PER_POINT],
                         ids=["brisbane", "perth"])
def test_excluded_level_drops_the_contour_penalty(monkeypatch, request, point):
    calls = []
    ns = _stub_inputs(monkeypatch, request, calls)
    with_air = ns.noise_score(*point, 500)
    # Sanity: the hit really reaches the default level, otherwise the
    # comparison below proves nothing.
    assert with_air.get("aircraft_db", 0) > 0, with_air
    assert with_air["aircraft"]["zone_code"], with_air
    assert calls, "default path must query the contour service"

    calls.clear()
    no_air = ns.noise_score(*point, 500, exclude_aircraft=True)
    assert calls == [], "excluded path must not query any ANEF source"
    assert no_air["lden_db"] < with_air["lden_db"], (no_air, with_air)
    assert "aircraft" not in no_air and "aircraft_db" not in no_air, no_air
    assert no_air.get("aircraft_excluded") is True, no_air
    assert no_air.get("aircraft_assessment") == "not_assessed", no_air
    assert no_air.get("dominant_source") != "aircraft", no_air
    assert "Aircraft noise is not included" in no_air["confidence_note"]


def test_default_path_and_cache_key_unchanged(monkeypatch, request):
    """exclude_aircraft=False must be the old call: same key, no marker."""
    calls = []
    ns = _stub_inputs(monkeypatch, request, calls)
    keys = []
    monkeypatch.setattr(ns, "_cache_get",
                        lambda db, key, ttl: keys.append(key) or None)
    r = ns.noise_score(*BNE_POINT, 500)
    ns.noise_score(*BNE_POINT, 500, exclude_aircraft=True)
    assert "aircraft_excluded" not in r
    assert not keys[0].endswith(":noair")
    assert keys[1] == keys[0] + ":noair"


def _node_stub(monkeypatch):
    """Grid nodes answer a level that depends on whether aircraft is in."""
    seen = []

    def fake(lat, lng, exclude_aircraft=False):
        seen.append(exclude_aircraft)
        return {"lden_db": 55.0 if exclude_aircraft else 66.0, "score": 50,
                "lden_source": "transfer"}

    monkeypatch.setattr(nsurf, "noise_score", fake)
    monkeypatch.setattr(nsurf, "_coverage_gap", lambda lat, lng: False)
    return seen


def test_surface_excludes_aircraft_on_every_node(monkeypatch):
    seen = _node_stub(monkeypatch)
    out = nsurf.noise_surface(*BNE_POINT, radius_m=1500, cells=9,
                              require_path="transfer", exclude_aircraft=True)
    assert len(seen) == 81 and all(seen), seen
    assert {v for row in out["lden_db"] for v in row} == {55.0}
    assert out["aircraft_excluded"] is True
    assert out["aircraft_assessment"] == "not_assessed"
    assert out["model_path_as_configured"] is True


def test_surface_default_keeps_aircraft(monkeypatch):
    seen = _node_stub(monkeypatch)
    out = nsurf.noise_surface(*BNE_POINT, radius_m=1500, cells=5,
                              require_path="transfer")
    assert seen and not any(seen)
    assert out["aircraft_excluded"] is False
    assert "aircraft_assessment" not in out


def test_endpoint_forwards_and_echoes_the_flag(monkeypatch):
    from fastapi.testclient import TestClient
    from property_scores.api import main
    seen = _node_stub(monkeypatch)
    monkeypatch.setattr(main, "_check_rate", lambda *a, **k: True)
    client = TestClient(main.app)
    r = client.get("/scores/noise/surface",
                   params={"lat": PER_POINT[0], "lng": PER_POINT[1],
                           "cells": 5, "require_path": "transfer",
                           "exclude_aircraft": "true"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["aircraft_excluded"] is True and all(seen)
    seen.clear()
    r = client.get("/scores/noise/surface",
                   params={"lat": PER_POINT[0], "lng": PER_POINT[1],
                           "cells": 5, "require_path": "transfer"})
    assert r.json()["aircraft_excluded"] is False and not any(seen)
