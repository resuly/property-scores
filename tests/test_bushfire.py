"""Bushfire combine tests — the official-clear floor + query-failure honesty.

Anchor (2026-06-10 Simon Kean): "High bushfire risk, but outside of council
fire zone". Old code did min(overlay, satellite) so the official NSW BPL
all-clear (90) was always crushed by WorldCover fuel pessimism, and an ArcGIS
timeout was indistinguishable from an official all-clear.
"""
from unittest import mock

from property_scores.bushfire.score import (_check_layer, _combine_scores,
                                            _OFFICIAL_CLEAR_FLOOR)


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
