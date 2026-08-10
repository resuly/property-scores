"""Bushfire combine tests — the official-clear floor + query-failure honesty.

Anchor (2026-06-10 Simon Kean): "High bushfire risk, but outside of council
fire zone". Old code did min(overlay, satellite) so the official NSW BPL
all-clear (90) was always crushed by WorldCover fuel pessimism, and an ArcGIS
timeout was indistinguishable from an official all-clear.
"""
from unittest import mock

from property_scores.bushfire.score import (_check_layer, _combine_scores,
                                            _OFFICIAL_CLEAR_FLOOR,
                                            _SEVERITY_FLOORS)


def test_official_clear_floors_satellite():
    # officially outside BPL, fuel model says 39 (High) -> floored to Moderate
    assert _combine_scores(90, 39, True) == _OFFICIAL_CLEAR_FLOOR


def test_official_clear_keeps_safer_satellite():
    assert _combine_scores(90, 85, True) == 85


def test_zone_hit_keeps_min():
    # in a mapped zone: official severity and satellite both count, min wins
    assert _combine_scores(22, 39, False) == 22
    assert _combine_scores(48, 15, False) == 15


def test_unavailable_overlay_satellite_decides():
    assert _combine_scores(None, 39, False) == 39


def test_no_data_default():
    assert _combine_scores(None, None, False) == 85


def test_overlay_only():
    assert _combine_scores(22, None, False) == 22


def test_buffer_hit_floors_at_severity_band():
    # Anchor (2026-06-12 Simon Kean round 2, Ku-ring-gai buffer edge):
    # "Vegetation Buffer" is the mildest official category (band 50-65) yet
    # min(58, sat 20) scored it 20 while the lot outside the polygon floored
    # at 55. The severity floor caps that cliff at the band floor.
    assert _combine_scores(58, 20, False, "low") == _SEVERITY_FLOORS["low"]


def test_severity_floor_keeps_official_order_at_polygon_edge():
    # same satellite pessimism in-buffer vs officially outside: the step must
    # stay one label, not Moderate-to-High
    in_buffer = _combine_scores(58, 20, False, "low")
    outside = _combine_scores(90, 20, True)
    assert outside - in_buffer <= 10


def test_severity_floor_does_not_lift_extreme():
    # Category 1: satellite may still read below the band midpoint freely
    assert _combine_scores(10, 8, False, "extreme") == 8


def test_severity_floor_moderate_band():
    assert _combine_scores(40, 12, False, "moderate") == _SEVERITY_FLOORS["moderate"]


def test_zone_hit_without_severity_keeps_min():
    # callers that pass no severity (or unknown severity) keep legacy min()
    assert _combine_scores(48, 15, False, None) == 15


def test_satellite_can_position_within_band():
    # satellite milder than the floor: min() result above floor is untouched
    assert _combine_scores(58, 52, False, "low") == 52


def test_check_layer_failure_is_not_clear():
    with mock.patch("property_scores.bushfire.score._query_arcgis", return_value=None):
        sev, detail, ok = _check_layer("NSW", "BPL", "http://x", "high", -33.7, 151.1)
    assert ok is False and sev is None


def test_check_layer_empty_features_is_clear():
    with mock.patch("property_scores.bushfire.score._query_arcgis",
                    return_value={"features": []}):
        sev, detail, ok = _check_layer("NSW", "BPL", "http://x", "high", -33.7, 151.1)
    assert ok is True and sev is None


def test_check_layer_nsw_category_mapping():
    data = {"features": [{"attributes": {"d_Category": "Vegetation Category 1"}}]}
    with mock.patch("property_scores.bushfire.score._query_arcgis", return_value=data):
        sev, detail, ok = _check_layer("NSW", "BPL", "http://x", "high", -33.7, 151.1)
    assert (sev, detail, ok) == ("extreme", "Vegetation Category 1", True)


# --- ACT Bushfire_Prone_Area_Details_2026.Hazard_Category ------------------
# The predecessor service (SBMP_BPA_current) was withdrawn upstream and every
# ACT address fell back to official_zone_status "unavailable". The replacement
# classifies, so the parser must read Hazard_Category rather than return a
# fixed "high" for any hit.

def _act_layer(attrs):
    data = {"features": [{"attributes": attrs}]}
    with mock.patch("property_scores.bushfire.score._query_arcgis", return_value=data):
        return _check_layer("ACT", "Bushfire Prone Area (ACT BPA 2026)",
                            "http://x", "high", -35.30, 149.10)


def test_check_layer_act_category_mapping():
    assert _act_layer({"Hazard_Category": "1"}) == ("extreme", "Hazard Category 1", True)
    assert _act_layer({"Hazard_Category": "2"}) == ("moderate", "Hazard Category 2", True)
    assert _act_layer({"Hazard_Category": "3"}) == ("low", "Hazard Category 3", True)


def test_check_layer_act_buffer():
    assert _act_layer({"Hazard_Category": "Buffer"}) == ("low", "Buffer", True)


def test_check_layer_act_unknown_category_stays_severe():
    # a class the publisher adds later must not read as the mildest band
    assert _act_layer({"Hazard_Category": "4"}) == ("high", "Hazard Category 4", True)
    sev, detail, ok = _act_layer({"OBJECTID": 1})
    assert (sev, ok) == ("high", True)
