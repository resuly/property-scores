"""Contamination score fail-closed semantics.

Anchor case (2026-08-10 audit): an EPA register answering 503 and an Overture
outage both produced a 95 "Very Clean" that was then cached for an hour. A
failed lookup is not a clean register, and the public label must not sound
reassuring when the evidence was never retrieved.
"""
import math

import pytest
import requests

from property_scores.contamination import score as cs


_NEUTRAL_SIGNAL = {"status": "not_integrated", "score": None, "entries": []}


@pytest.fixture(autouse=True)
def _neutral_new_signals(monkeypatch):
    """Keep the 2026-08-27 signals (historical use / landfill / groundwater)
    out of legacy tests: they hit live endpoints and would make every
    contamination_score() call network-dependent. Signal-specific behaviour
    is tested with its own stubs in tests/test_contam_signals.py."""
    for name in ("_historical_use_signal", "_landfill_signal",
                 "_groundwater_signal"):
        monkeypatch.setattr(cs, name, lambda *a, **k: dict(_NEUTRAL_SIGNAL))


class _Resp:
    def __init__(self, ok, payload=None):
        self.ok = ok
        self.status_code = 200 if ok else 503
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _clear_cache():
    cs._contam_cache.clear()
    yield
    cs._contam_cache.clear()


# ---------------------------------------------------------------------------
# 1. non-2xx must be distinguishable from an empty register
# ---------------------------------------------------------------------------

EPA_FUNCS = [cs._vic_epa_sites, cs._nsw_epa_sites, cs._wa_epa_sites]


@pytest.mark.parametrize("func", EPA_FUNCS)
def test_epa_non_2xx_returns_none(monkeypatch, func):
    monkeypatch.setattr(cs.requests, "get", lambda *a, **k: _Resp(False))
    assert func(-37.8, 144.96) is None


@pytest.mark.parametrize("func", EPA_FUNCS)
def test_epa_exception_returns_none(monkeypatch, func):
    def _boom(*a, **k):
        raise requests.RequestException("connection dropped")

    monkeypatch.setattr(cs.requests, "get", _boom)
    assert func(-37.8, 144.96) is None


@pytest.mark.parametrize("func", EPA_FUNCS)
def test_epa_empty_register_returns_empty_list(monkeypatch, func):
    monkeypatch.setattr(cs.requests, "get", lambda *a, **k: _Resp(True, {"features": []}))
    assert func(-37.8, 144.96) == []


def test_search_envelope_covers_full_east_west_radius_at_melbourne_latitude():
    west, south, east, north = cs._search_envelope(-37.81, 144.99, 2000)
    assert cs._distance_m(-37.81, 144.99, -37.81, east) >= 1999
    assert cs._distance_m(-37.81, 144.99, -37.81, west) >= 1999
    assert east - 144.99 > north - (-37.81)


@pytest.mark.parametrize("lat", [-90, 90])
def test_search_envelope_rejects_poles_outside_its_australian_contract(lat):
    with pytest.raises(ValueError):
        cs._search_envelope(lat, 145, 2000)


def test_vic_query_filters_square_corner_outside_circular_radius(monkeypatch):
    lat, lng = -37.810763, 144.993306
    inside_lng = lng + 1900 / (111_320 * math.cos(math.radians(lat)))
    corner_lat = lat + 1800 / 111_320
    corner_lng = lng + 1800 / (111_320 * math.cos(math.radians(lat)))
    payload = {"features": [
        {"geometry": {"coordinates": [inside_lng, lat]},
         "properties": {"address": "inside east", "issue": "test"}},
        {"geometry": {"coordinates": [corner_lng, corner_lat]},
         "properties": {"address": "outside corner", "issue": "test"}},
    ]}
    captured = {}

    def _get(*args, **kwargs):
        captured.update(kwargs["params"])
        return _Resp(True, payload)

    monkeypatch.setattr(cs.requests, "get", _get)
    sites = cs._vic_epa_sites(lat, lng, radius_m=2000)
    assert [site["name"] for site in sites] == ["inside east"]
    assert sites[0]["distance_m"] == pytest.approx(1900, abs=2)
    west, south, east, north, _ = captured["bbox"].split(",")
    assert float(east) > inside_lng


def test_nsw_query_filters_records_beyond_radius_and_accepts_string_coordinates(
    monkeypatch,
):
    lat, lng = -33.9080, 151.1950
    payload = {"features": [
        {"attributes": {"Longitude": str(lng), "Latitude": str(lat + 1500 / 111_320),
                        "SiteName": "inside", "ContaminationActivityType": "test"}},
        {"attributes": {"Longitude": str(lng), "Latitude": str(lat + 2100 / 111_320),
                        "SiteName": "outside", "ContaminationActivityType": "test"}},
    ]}
    monkeypatch.setattr(cs.requests, "get", lambda *a, **k: _Resp(True, payload))
    sites = cs._nsw_epa_sites(lat, lng, radius_m=2000)
    assert [site["name"] for site in sites] == ["inside"]
    assert sites[0]["distance_m"] == pytest.approx(1498, abs=2)


@pytest.mark.parametrize("func", EPA_FUNCS)
def test_epa_malformed_feature_fails_closed(monkeypatch, func):
    monkeypatch.setattr(cs.requests, "get", lambda *a, **k: _Resp(True, {"features": [None]}))
    assert func(-37.8, 144.96) is None


@pytest.mark.parametrize("geometry", [{}, {"rings": []}])
def test_wa_feature_without_usable_geometry_fails_closed(monkeypatch, geometry):
    payload = {"features": [{
        "attributes": {"reg_no": "missing-geometry"},
        "geometry": geometry,
    }]}
    monkeypatch.setattr(cs.requests, "get", lambda *a, **k: _Resp(True, payload))
    assert cs._wa_epa_sites(-31.95, 115.86) is None


def test_wa_polygon_uses_nearest_edge_not_nearest_vertex(monkeypatch):
    lat, lng = -31.95, 115.86
    dlat = 1000 / 111_320
    dlng = 3000 / (111_320 * math.cos(math.radians(lat)))
    ring = [[lng - dlng, lat + dlat], [lng + dlng, lat + dlat],
            [lng + dlng, lat + 2 * dlat], [lng - dlng, lat + 2 * dlat],
            [lng - dlng, lat + dlat]]
    payload = {"features": [{
        "attributes": {"sitename": "long edge"},
        "geometry": {"rings": [ring]},
    }]}
    monkeypatch.setattr(cs.requests, "get", lambda *a, **k: _Resp(True, payload))
    sites = cs._wa_epa_sites(lat, lng, radius_m=2000)
    assert len(sites) == 1
    assert sites[0]["distance_m"] == pytest.approx(1000, abs=2)
    assert sites[0]["lng"] == pytest.approx(lng, abs=0.00001)
    assert sites[0]["lat"] == pytest.approx(lat + dlat, abs=0.00001)


def test_wa_polygon_hole_is_not_treated_as_contaminated_interior(monkeypatch):
    lat, lng = -31.95, 115.86
    dlat = 500 / 111_320
    dlng = 500 / (111_320 * math.cos(math.radians(lat)))
    outer = [[lng - 4 * dlng, lat - 4 * dlat], [lng + 4 * dlng, lat - 4 * dlat],
             [lng + 4 * dlng, lat + 4 * dlat], [lng - 4 * dlng, lat + 4 * dlat],
             [lng - 4 * dlng, lat - 4 * dlat]]
    hole = [[lng - dlng, lat - dlat], [lng - dlng, lat + dlat],
            [lng + dlng, lat + dlat], [lng + dlng, lat - dlat],
            [lng - dlng, lat - dlat]]
    payload = {"features": [{
        "attributes": {"sitename": "donut"},
        "geometry": {"rings": [outer, hole]},
    }]}
    monkeypatch.setattr(cs.requests, "get", lambda *a, **k: _Resp(True, payload))
    site = cs._wa_epa_sites(lat, lng, radius_m=2000)[0]
    assert site["distance_m"] == pytest.approx(500, abs=2)
    assert cs._distance_m(lat, lng, site["lat"], site["lng"]) == pytest.approx(500, abs=2)


def test_wa_multipart_second_exterior_contains_query_and_pins_query(monkeypatch):
    lat, lng = -31.95, 115.86
    delta = 0.002
    far = [[lng + 0.05, lat], [lng + 0.06, lat], [lng + 0.06, lat + 0.01],
           [lng + 0.05, lat + 0.01], [lng + 0.05, lat]]
    around = [[lng - delta, lat - delta], [lng + delta, lat - delta],
              [lng + delta, lat + delta], [lng - delta, lat + delta],
              [lng - delta, lat - delta]]
    payload = {"features": [{
        "attributes": {"sitename": "multipart"},
        "geometry": {"rings": [far, around]},
    }]}
    monkeypatch.setattr(cs.requests, "get", lambda *a, **k: _Resp(True, payload))
    site = cs._wa_epa_sites(lat, lng, radius_m=2000)[0]
    assert site["distance_m"] == 0
    assert site["lng"] == pytest.approx(lng)
    assert site["lat"] == pytest.approx(lat)


def test_wa_concave_polygon_marker_is_nearest_boundary_not_vertex_average(monkeypatch):
    lat, lng = -31.95, 115.86
    ring = [[lng + 0.01, lat - 0.01], [lng + 0.03, lat - 0.01],
            [lng + 0.03, lat + 0.01], [lng + 0.02, lat],
            [lng + 0.01, lat + 0.01], [lng + 0.01, lat - 0.01]]
    payload = {"features": [{
        "attributes": {"sitename": "concave"},
        "geometry": {"rings": [ring]},
    }]}
    monkeypatch.setattr(cs.requests, "get", lambda *a, **k: _Resp(True, payload))
    site = cs._wa_epa_sites(lat, lng, radius_m=2000)[0]
    assert site["distance_m"] > 0
    assert cs._distance_m(lat, lng, site["lat"], site["lng"]) == pytest.approx(
        site["distance_m"], abs=2
    )


# ArcGIS REST and GeoServer WFS report a failed query with HTTP 200 plus an
# error envelope, which is the more common outage shape than a non-2xx.
HTTP_200_FAILURE_BODIES = [
    {"error": {"code": 400, "message": "Unable to complete operation"}},   # ArcGIS
    {"error": {"code": 500, "message": "Token Required"}},                 # ArcGIS
    {"exceptions": [{"exceptionCode": "NoApplicableCode"}]},               # WFS
    # an error envelope that also carries an empty feature list: the error key
    # wins, an accompanying [] is not evidence the register was searched
    {"error": {"code": 400}, "features": []},
    {"exceptions": [{"exceptionCode": "NoApplicableCode"}], "features": []},
    {},                                                                    # no features key
    {"features": None},
    {"type": "FeatureCollection"},                                         # truncated body
    [],                                                                    # not an object
]


@pytest.mark.parametrize("func", EPA_FUNCS)
@pytest.mark.parametrize("body", HTTP_200_FAILURE_BODIES)
def test_epa_http_200_error_body_returns_none(monkeypatch, func, body):
    monkeypatch.setattr(cs.requests, "get", lambda *a, **k: _Resp(True, body))
    assert func(-37.8, 144.96) is None


def test_http_200_error_body_does_not_produce_a_clean_score(monkeypatch):
    # Botany NSW is the repo's own "very high risk" truth anchor: an ArcGIS
    # error envelope must not turn it into a clean register.
    monkeypatch.setattr(cs.requests, "get",
                        lambda *a, **k: _Resp(True, {"error": {"code": 400}}))
    _patch_industrial(monkeypatch, ok=True)
    r = cs.contamination_score(-33.9450, 151.2000)
    assert r["epa_status"] == "error"
    assert r["label"] == cs.LABEL_INCOMPLETE
    assert cs._contam_cache == {}


# ---------------------------------------------------------------------------
# 2. industrial proximity carries its own status
# ---------------------------------------------------------------------------

def test_industrial_status_ok(monkeypatch):
    monkeypatch.setattr(cs, "get_db", lambda: object())
    monkeypatch.setattr(cs, "pois_near_detailed", lambda *a, **k: [])
    ind = cs._industrial_proximity(-37.8, 144.96)
    assert ind["industrial_status"] == "ok"
    assert ind["count_500m"] == 0


def test_industrial_status_error_on_failure(monkeypatch):
    def _boom():
        raise RuntimeError("overture parquet unreachable")

    monkeypatch.setattr(cs, "get_db", _boom)
    ind = cs._industrial_proximity(-37.8, 144.96)
    assert ind["industrial_status"] == "error"
    # legacy keys stay for backward compatibility
    assert ind["count_500m"] == 0
    assert ind["nearest_m"] is None
    assert ind["sites"] == []


# ---------------------------------------------------------------------------
# 3. label gate
# ---------------------------------------------------------------------------

def test_label_bands_unchanged_when_everything_ok():
    assert cs._contamination_label(95, epa_status="ok", ind_failed=False) == "No Mapped Red Flag"
    assert cs._contamination_label(80, epa_status="ok", ind_failed=False) == "Lower Mapped Risk"
    assert cs._contamination_label(55, epa_status="ok", ind_failed=False) == "Mapped Risk - Review"
    assert cs._contamination_label(35, epa_status="ok", ind_failed=False) == "Elevated Mapped Risk"
    assert cs._contamination_label(20, epa_status="ok", ind_failed=False) == "High Mapped Risk"
    assert cs._contamination_label(5, epa_status="ok", ind_failed=False) == "Very High Mapped Risk"


@pytest.mark.parametrize("score", [70, 80, 95, 100])
def test_reassuring_label_blocked_on_epa_error(score):
    assert cs._contamination_label(score, epa_status="error", ind_failed=False) == \
        cs.LABEL_INCOMPLETE


@pytest.mark.parametrize("score", [70, 80, 95, 100])
def test_reassuring_label_blocked_on_industrial_error(score):
    assert cs._contamination_label(score, epa_status="ok", ind_failed=True) == \
        cs.LABEL_INCOMPLETE


@pytest.mark.parametrize("score", [70, 80, 95, 100])
def test_reassuring_label_blocked_when_register_not_integrated(score):
    label = cs._contamination_label(score, epa_status="not_integrated", ind_failed=False)
    assert label == cs.LABEL_REGISTER_NOT_CHECKED
    assert "clean" not in label.lower()


@pytest.mark.parametrize("score", [70, 80, 95, 100])
def test_evidence_context_beats_register_not_integrated_label(score):
    label = cs._contamination_label(
        score,
        epa_status="not_integrated",
        ind_failed=False,
        context_flagged=True,
    )
    assert label == cs.LABEL_MAPPED_CONTEXT
    assert "clean" not in label.lower()


def test_nearby_register_evidence_blocks_clean_headline(monkeypatch):
    monkeypatch.setattr(cs, "_detect_state", lambda *a: "NSW")
    monkeypatch.setattr(cs, "_nsw_epa_sites", lambda *a, **k: [{
        "distance_m": 500,
        "geom": "point",
        "issue": "Contamination currently regulated under CLM Act",
        "source": "NSW EPA",
    }])
    monkeypatch.setattr(cs, "_industrial_proximity", lambda *a: {
        "count_500m": 0, "nearest_m": None, "nearest_type": None,
        "sites": [], "industrial_status": "ok",
    })

    result = cs.contamination_score(-33.86, 151.20)

    assert result["score"] == 75
    assert result["on_site"]["epa_active"] is False
    assert result["label"] == cs.LABEL_MAPPED_CONTEXT


def test_nearby_industrial_context_blocks_clean_headline(monkeypatch):
    from property_scores.contamination.sources import act_register

    monkeypatch.setattr(cs, "_detect_state", lambda *a: "ACT")
    monkeypatch.setattr(act_register, "sites_at", lambda *a: [])
    monkeypatch.setattr(cs, "_industrial_proximity", lambda *a: {
        "count_500m": 1, "nearest_m": 300, "nearest_type": "dry cleaning",
        "sites": [{"type": "dry cleaning", "distance_m": 300}],
        "industrial_status": "ok",
    })

    result = cs.contamination_score(-35.28, 149.13)

    assert result["score"] == 85
    assert result["label"] == cs.LABEL_MAPPED_CONTEXT


@pytest.mark.parametrize("score", [10, 25, 45, 65])
def test_risk_labels_survive_degradation(score):
    # a bad band is still useful information, only reassurance is withheld
    assert cs._contamination_label(score, epa_status="error", ind_failed=True) == \
        cs._score_band_label(score)


def test_no_signal_at_all_is_check_unavailable():
    assert cs._contamination_label(None, epa_status="error", ind_failed=True) == \
        cs.LABEL_CHECK_UNAVAILABLE


def test_no_reassuring_label_reachable_while_degraded():
    for score in range(0, 101):
        for epa_status in ("ok", "error", "not_integrated"):
            for ind_failed in (False, True):
                label = cs._contamination_label(score, epa_status, ind_failed)
                degraded = epa_status != "ok" or ind_failed
                if degraded:
                    assert label not in cs._REASSURING_LABELS


# ---------------------------------------------------------------------------
# 4. end to end through contamination_score
# ---------------------------------------------------------------------------

MELB = (-37.8136, 144.9631)   # VIC, register integrated
BRIS = (-27.4698, 153.0251)   # QLD, no register integrated
PERTH = (-31.9505, 115.8605)  # WA, DWER licence pending


def _patch_epa(monkeypatch, mode):
    """mode: 'ok' (empty register), 'http_error', 'exception'."""
    if mode == "ok":
        monkeypatch.setattr(cs.requests, "get",
                            lambda *a, **k: _Resp(True, {"features": []}))
    elif mode == "http_error":
        monkeypatch.setattr(cs.requests, "get", lambda *a, **k: _Resp(False))
    else:
        def _boom(*a, **k):
            raise requests.RequestException("dropped")
        monkeypatch.setattr(cs.requests, "get", _boom)


def _patch_industrial(monkeypatch, ok=True):
    if ok:
        monkeypatch.setattr(cs, "get_db", lambda: object())
        monkeypatch.setattr(cs, "pois_near_detailed", lambda *a, **k: [])
    else:
        def _boom():
            raise RuntimeError("overture down")
        monkeypatch.setattr(cs, "get_db", _boom)


def test_happy_path_still_very_clean(monkeypatch):
    _patch_epa(monkeypatch, "ok")
    _patch_industrial(monkeypatch, ok=True)
    r = cs.contamination_score(*MELB)
    assert r["score"] == 95
    assert r["label"] == "No Mapped Red Flag"
    assert r["epa_status"] == "ok"
    assert r["industrial_status"] == "ok"
    assert "note" not in r


def test_public_count_and_score_drop_any_adapter_record_beyond_two_km(monkeypatch):
    monkeypatch.setattr(cs, "_vic_epa_sites", lambda *a, **k: [
        {"name": "inside", "distance_m": 1900, "lng": 144.98, "lat": -37.8},
        {"name": "outside", "distance_m": 2100, "lng": 144.99, "lat": -37.8},
    ])
    _patch_industrial(monkeypatch, ok=True)
    r = cs.contamination_score(*MELB)
    assert r["epa_sites_count"] == 1
    assert [site["name"] for site in r["epa_sites"]] == ["inside"]
    assert all(site["distance_m"] <= 2000 for site in r["epa_sites"])


@pytest.mark.parametrize("bad_distance", ["unknown", float("nan"), float("inf"), -1])
def test_public_adapter_guard_fails_closed_on_invalid_distance(monkeypatch, bad_distance):
    monkeypatch.setattr(cs, "_vic_epa_sites", lambda *a, **k: [
        {"name": "bad", "distance_m": bad_distance, "lng": 144.98, "lat": -37.8},
    ])
    _patch_industrial(monkeypatch, ok=True)
    r = cs.contamination_score(*MELB)
    assert r["epa_status"] == "error"
    assert r["epa_sites_count"] == 0
    assert r["label"] == cs.LABEL_INCOMPLETE


@pytest.mark.parametrize("mode", ["http_error", "exception"])
def test_epa_outage_is_not_clean_and_is_not_cached(monkeypatch, mode):
    _patch_epa(monkeypatch, mode)
    _patch_industrial(monkeypatch, ok=True)
    r = cs.contamination_score(*MELB)
    assert r["epa_status"] == "error"
    assert r["industrial_status"] == "ok"
    assert r["label"] == cs.LABEL_INCOMPLETE
    assert "clean" not in r["label"].lower()
    # The numeric score is withheld, so the note states unavailability
    # rather than asking a caller to interpret an optimistic partial score.
    assert "EPA register could not be reached" in r["note"]
    assert "No score could be produced" in r["note"]
    assert cs._contam_cache == {}


def test_industrial_outage_is_not_clean_and_is_not_cached(monkeypatch):
    _patch_epa(monkeypatch, "ok")
    _patch_industrial(monkeypatch, ok=False)
    r = cs.contamination_score(*MELB)
    assert r["epa_status"] == "ok"
    assert r["industrial_status"] == "error"
    assert r["label"] == cs.LABEL_INCOMPLETE
    assert cs._contam_cache == {}


def test_both_signals_down_gives_no_score(monkeypatch):
    _patch_epa(monkeypatch, "http_error")
    _patch_industrial(monkeypatch, ok=False)
    r = cs.contamination_score(*MELB)
    assert r["score"] is None
    assert r["label"] == cs.LABEL_CHECK_UNAVAILABLE
    assert cs._contam_cache == {}


def test_not_integrated_state_drops_reassuring_score_and_names_coverage(monkeypatch):
    _patch_epa(monkeypatch, "ok")
    _patch_industrial(monkeypatch, ok=True)
    r = cs.contamination_score(*BRIS)
    assert r["state"] == "QLD"
    assert r["score"] is None
    assert r["score_status"] == "unavailable_incomplete_coverage"
    assert r["epa_status"] == "not_integrated"
    assert r["industrial_status"] == "ok"
    assert r["label"] == cs.LABEL_REGISTER_NOT_CHECKED
    assert "clean" not in r["label"].lower()
    assert "No QLD EPA register is integrated" in r["note"]
    # a missing register is a stable fact, not an outage: still cacheable
    assert len(cs._contam_cache) == 1


def test_wa_register_is_not_queried_before_dwer_permission(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("DWER-059 must remain disabled pending permission")

    monkeypatch.setattr(cs, "_wa_epa_sites", forbidden)
    _patch_industrial(monkeypatch, ok=True)
    result = cs.contamination_score(*PERTH)
    assert result["state"] == "WA"
    assert result["epa_status"] == "not_integrated"
    assert result["score"] is None
    assert result["score_status"] == "unavailable_incomplete_coverage"
    assert result["label"] == cs.LABEL_REGISTER_NOT_CHECKED


def test_act_official_register_positive_is_on_site_and_attributed(monkeypatch):
    from property_scores.contamination.sources import act_register

    monkeypatch.setattr(cs, "_detect_state", lambda *a: "ACT")
    monkeypatch.setattr(act_register, "sites_at", lambda *a: [{
        "site_id": "42",
        "name": "Active BP Service Station",
        "issue": "ACT Register of contaminated sites, notified under section 76A(1)",
        "activity_type": "ACT contaminated sites register",
        "management_class": "76A(1)",
        "distance_m": 0,
        "geom": "polygon",
        "source": "ACT EPA Register of contaminated sites",
    }])
    _patch_industrial(monkeypatch, ok=True)

    result = cs.contamination_score(-35.27582, 149.13277)

    assert result["epa_status"] == "ok"
    assert result["score"] == 10
    assert result["label"] == "Very High Mapped Risk"
    assert result["on_site"]["epa_active"] is True
    assert result["attribution"] == [{
        "source": "ACT EPA Register of contaminated sites",
        "attribution": "Register of contaminated sites © Australian Capital Territory",
        "licence": "CC BY 4.0",
        "licence_url": "https://creativecommons.org/licenses/by/4.0/",
    }]


def test_act_empty_and_failure_remain_distinguishable(monkeypatch):
    from property_scores.contamination.sources import act_register

    monkeypatch.setattr(cs, "_detect_state", lambda *a: "ACT")
    _patch_industrial(monkeypatch, ok=True)
    monkeypatch.setattr(act_register, "sites_at", lambda *a: [])
    clear = cs.contamination_score(-35.28, 149.13)
    assert clear["epa_status"] == "ok"
    assert clear["score_status"] == "available"
    assert clear["epa_sites"] == []
    assert clear["label"] == "No Mapped Red Flag"

    cs._contam_cache.clear()
    monkeypatch.setattr(act_register, "sites_at", lambda *a: None)
    failed = cs.contamination_score(-35.28, 149.13)
    assert failed["epa_status"] == "error"
    assert failed["score_status"] == "unavailable_incomplete_coverage"
    assert failed["label"] == cs.LABEL_INCOMPLETE
    assert cs._contam_cache == {}


def test_industrial_outage_note_names_the_source(monkeypatch):
    _patch_epa(monkeypatch, "ok")
    _patch_industrial(monkeypatch, ok=False)
    r = cs.contamination_score(*MELB)
    assert "industrial land use data could not be reached" in r["note"]
    assert "No score could be produced" in r["note"]


def test_healthy_result_is_cached(monkeypatch):
    _patch_epa(monkeypatch, "ok")
    _patch_industrial(monkeypatch, ok=True)
    cs.contamination_score(*MELB)
    assert len(cs._contam_cache) == 1
    assert cs.contamination_score(*MELB)["cached"] is True


# ---------------------------------------------------------------------------
# 5. cache key resolution
# ---------------------------------------------------------------------------

def test_cache_key_does_not_merge_neighbouring_parcels(monkeypatch):
    _patch_epa(monkeypatch, "ok")
    _patch_industrial(monkeypatch, ok=True)
    lng = MELB[1]
    # Both latitudes land in the same round(3) cell but are ~109m apart, which
    # straddles the 100m EPA distance band: under the old key one parcel was
    # served the other one's answer.
    lat_a, lat_b = -37.81251, -37.81349
    assert round(lat_a, 3) == round(lat_b, 3)
    assert round(lat_a, 4) != round(lat_b, 4)

    cs.contamination_score(lat_a, lng)
    assert cs.contamination_score(lat_b, lng).get("cached") is not True
    assert len(cs._contam_cache) == 2


def test_cache_key_still_merges_identical_coordinates(monkeypatch):
    _patch_epa(monkeypatch, "ok")
    _patch_industrial(monkeypatch, ok=True)
    lat, lng = MELB
    cs.contamination_score(lat, lng)
    # ~1m apart: below the round(4) grid, still one entry
    assert cs.contamination_score(lat + 0.000001, lng)["cached"] is True
    assert len(cs._contam_cache) == 1


# ---------------------------------------------------------------------------
# 6. the truth-probe harness must not go quiet when the score disappears
# ---------------------------------------------------------------------------

def _load_probes():
    import importlib.util
    import pathlib
    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "score_truth_probes.py"
    spec = importlib.util.spec_from_file_location("score_truth_probes", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_probe_scoreless_payload_fails_not_manual():
    # score None is newly reachable in Australia (both signal layers down).
    # MANUAL never reaches new_failures, so a dead anchor would alert nobody.
    probes = _load_probes()
    unavailable = {"score": None, "label": cs.LABEL_CHECK_UNAVAILABLE}
    assert probes.evaluate("flag_high_risk", unavailable)[0] == "FAIL"
    assert probes.evaluate("score<=40", unavailable)[0] == "FAIL"
    assert probes.evaluate("score>=80", unavailable)[0] == "FAIL"


def test_probe_still_evaluates_real_scores():
    probes = _load_probes()
    assert probes.evaluate("flag_high_risk", {"score": 10})[0] == "PASS"
    assert probes.evaluate("flag_high_risk", {"score": 95})[0] == "FAIL"
    assert probes.evaluate("score<=40", {"score": 10})[0] == "PASS"
    assert probes.evaluate("score<=40", {"score": 95})[0] == "FAIL"
    assert probes.evaluate("score>=80", {"score": 95})[0] == "PASS"


# ---------------------------------------------------------------------------
# On-site vs proximity semantics (2026-08-26 rework)
# The pre-rework scorer was pure distance decay, so a clean lot scored 10
# because its NEIGHBOUR 80m away is on the register. Contamination stays with
# the land that produced it unless groundwater carries it; these tests pin
# the on-site-first contract so a silent return to distance decay goes red.
# ---------------------------------------------------------------------------

def _site(dist, issue="clean up notice"):
    return {"name": f"site@{dist}", "distance_m": dist, "issue": issue,
            "lng": 144.98, "lat": -37.8}


def test_active_on_site_register_entry_scores_10():
    assert cs._epa_to_score([_site(30)]) == 10


def test_remediated_on_site_entry_is_history_not_active_risk():
    s = cs._epa_to_score([_site(30, issue="remediated")])
    assert s == 55


def test_active_neighbour_90m_is_context_not_site_risk():
    # THE reviewer scenario: a register entry near a POINT geometry must not
    # drag this lot to 10. But a point 90m out may still be THIS parcel's
    # register pin (big industrial lots), so it stays a caution band (45),
    # never the on-site band and never "Low Risk" reassurance.
    s = cs._epa_to_score([_site(90)])
    assert 30 <= s < 50


def test_active_polygon_neighbour_is_pure_context():
    # WA gives true boundary distances: 40m to a NEIGHBOURING polygon is a
    # different parcel, full stop. Context band only.
    s = cs._epa_to_score([_site(40) | {"geom": "polygon"}])
    assert s >= 70


def test_inside_wa_polygon_is_on_site():
    s = cs._epa_to_score([_site(0) | {"geom": "polygon"}])
    assert s == 10


def test_nearby_active_entries_never_beat_on_site_floor():
    # Any pile of nearby-only active entries stays above the on-site band.
    s = cs._epa_to_score([_site(90), _site(150), _site(200), _site(400)])
    assert s > 10 and s >= 40


def test_nearby_remediated_only_is_nearly_clean():
    assert cs._epa_to_score([_site(200, issue="remediated")]) >= 85


def test_on_site_active_beats_surrounding_remediated():
    s = cs._epa_to_score([_site(200, issue="remediated"), _site(40)])
    assert s == 10


def test_industrial_on_site_outweighs_neighbourhood():
    on_site = cs._industrial_to_score({"count_500m": 1, "nearest_m": 30})
    neighbours = cs._industrial_to_score({"count_500m": 6, "nearest_m": 200})
    assert on_site < neighbours
    assert neighbours >= 60  # a working precinct next door is not High Risk


def test_result_carries_on_site_block(monkeypatch):
    monkeypatch.setattr(cs, "_vic_epa_sites", lambda *a, **k: [_site(30)])
    _patch_industrial(monkeypatch, ok=True)
    cs._contam_cache.clear()
    r = cs.contamination_score(*MELB)
    assert r["on_site"]["epa_active"] is True
    assert r["on_site"]["epa_remediated"] is False
    assert r["on_site"]["industrial"] is False
    assert r["score"] == 10


def test_result_on_site_false_for_neighbour_only(monkeypatch):
    monkeypatch.setattr(cs, "_vic_epa_sites", lambda *a, **k: [_site(90)])
    _patch_industrial(monkeypatch, ok=True)
    cs._contam_cache.clear()
    r = cs.contamination_score(*MELB)
    assert r["on_site"]["epa_active"] is False
    assert r["score"] >= 40


def test_former_with_active_notice_is_active():
    # Live VIC PSR wording: "former" describes the historical land use while
    # the notice itself is ACTIVE. Misreading it as remediated sent an active
    # clean-up site to a Clean band (review finding, hit on real data).
    live_issue = "Former petroleum storage site. Requires assessment and/or clean up"
    assert cs._epa_to_score([_site(30, issue=live_issue)]) == 10
    assert cs._epa_to_score([_site(200, issue=live_issue)]) < 50


def test_polygon_neighbour_never_reported_as_on_site(monkeypatch):
    # A neighbouring WA polygon 40m away must not produce
    # on_site.epa_active/epa_remediated True for THIS address.
    monkeypatch.setattr(cs, "_vic_epa_sites",
                        lambda *a, **k: [_site(40) | {"geom": "polygon"}])
    _patch_industrial(monkeypatch, ok=True)
    cs._contam_cache.clear()
    r = cs.contamination_score(*MELB)
    assert r["on_site"]["epa_active"] is False
    assert r["on_site"]["epa_remediated"] is False


def test_disclaimer_owns_the_groundwater_gap(monkeypatch):
    monkeypatch.setattr(cs, "_vic_epa_sites", lambda *a, **k: [])
    _patch_industrial(monkeypatch, ok=True)
    cs._contam_cache.clear()
    r = cs.contamination_score(*MELB)
    assert "groundwater" in r["disclaimer"]
    assert "clean screen is not a clean site" in r["disclaimer"]
