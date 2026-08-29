"""View elevation-advantage tests — the valley-rim directional credit.

Anchor (2026-06-12 Simon Kean round 2): "uninterrupted views north over
National Parks up the valley from all main rooms for 6km" scored as flat
terrain, because the ring MEDIANS are direction-agnostic: a ridge suburb
behind the house cancels the valley in front of it.
"""
from unittest import mock

import math

import pytest

from property_scores.view_quality import score as vs
from property_scores.view_quality.score import _elevation_advantage_factor

# _sample_elevations layout: [center, near x8, far x8]
# ring order: N, S, E, W, NE, NW, SE, SW (same order both rings)


def _factor_with(elevs):
    with mock.patch(
        "property_scores.view_quality.score._sample_elevations",
        return_value=elevs,
    ):
        return _elevation_advantage_factor(-33.7, 151.1)


def test_valley_rim_view_scores_high():
    # plateau behind (5 dirs at house level), deep valley in front (3 dirs
    # dropping 150 -> 30): medians say advantage ~0, the rim credit must not
    center = 160.0
    near = [150.0, 160.0, 150.0, 160.0, 150.0, 160.0, 160.0, 160.0]
    far = [30.0, 160.0, 30.0, 160.0, 30.0, 160.0, 160.0, 160.0]
    f = _factor_with([center] + near + far)
    assert f["rim_drop_m"] == 130.0
    assert f["advantage_m"] >= 50
    assert f["value"] == 1.0


def test_flat_terrain_unchanged():
    f = _factor_with([160.0] * 17)
    assert f["rim_drop_m"] == 0.0
    assert f["advantage_m"] == 0.0
    # 0.25 base + 0.15 absolute-elevation bonus, exactly the legacy value
    assert abs(f["value"] - 0.40) < 1e-9


def test_hilltop_still_uses_median_advantage():
    # all-around hilltop: median advantage (40m) beats the discounted rim
    # credit, behavior identical to the legacy path
    center = 100.0
    near = [80.0] * 8
    far = [60.0] * 8
    f = _factor_with([center] + near + far)
    assert f["advantage_m"] == 40.0


def test_no_rim_credit_through_intervening_ridge():
    # deep valley 2km away in 3 directions, but the near ring rises UPHILL
    # toward it (a ridge between house and valley): no credible sightline,
    # no rim credit, medians unchanged
    center = 100.0
    near = [110.0, 100.0, 110.0, 100.0, 110.0, 100.0, 100.0, 100.0]
    far = [20.0, 100.0, 20.0, 100.0, 20.0, 100.0, 100.0, 100.0]
    f = _factor_with([center] + near + far)
    assert f["rim_drop_m"] == 0.0
    assert f["advantage_m"] == 0.0


@pytest.mark.parametrize("bearing", [0, 45, 90, 135, 180, 225, 270, 315])
def test_direction_offsets_are_equal_ground_distance_at_melbourne(bearing):
    lat, lng = -37.8136, 144.9631
    out_lat, out_lng = vs._offset_point(lat, lng, 500, bearing)
    north_m = (out_lat - lat) * 111_320
    east_m = (out_lng - lng) * 111_320 * math.cos(math.radians(lat))
    assert math.hypot(north_m, east_m) == pytest.approx(500, abs=0.2)


def test_checked_clear_water_is_zero_not_a_missing_factor(monkeypatch):
    monkeypatch.setattr(vs, "_data_file_available", lambda _name: True)
    monkeypatch.setattr(vs, "water_near", lambda *_args, **_kwargs: [])

    ocean = vs._ocean_proximity_factor(object(), -37.81, 144.96)
    inland = vs._inland_water_factor(object(), -37.81, 144.96)

    assert ocean == {
        "value": 0.0, "distance_m": None, "searched_radius_m": 10_000,
        "coverage_status": "checked_clear",
    }
    assert inland == {
        "value": 0.0, "distance_m": None, "searched_radius_m": 3000,
        "coverage_status": "checked_clear",
    }


def test_any_missing_or_partial_factor_is_explicitly_degraded(monkeypatch):
    vs._vq_cache.clear()
    monkeypatch.setattr(vs, "get_db", lambda: object())
    monkeypatch.setattr(vs, "_ocean_proximity_factor",
                        lambda *_: {"value": 0.5})
    monkeypatch.setattr(vs, "_inland_water_factor", lambda *_: None)
    monkeypatch.setattr(vs, "_elevation_advantage_factor",
                        lambda *_: {"value": 0.5})
    monkeypatch.setattr(vs, "_green_space_factor",
                        lambda *_: {"value": 0.5, "signals_used": []})
    monkeypatch.setattr(vs, "_building_openness_factor",
                        lambda *_: {"value": 0.5})
    monkeypatch.setattr(vs, "_horizon_openness_factor",
                        lambda *_: {"value": 0.5, "degraded": True,
                                    "coverage_fraction": 0.5})

    out = vs.view_quality_score(-37.81, 144.96)

    assert out["product"] == "landscape_openness"
    assert out["legacy_score_key"] == "view_quality"
    assert out["missing_factors"] == ["inland_water"]
    assert out["partial_factors"] == ["horizon_openness"]
    assert out["degraded"] is True
    assert out["factor_weight_completeness"] < 1
    assert out["factor_weight_completeness"] == pytest.approx(0.796, abs=0.001)
    assert out["line_of_sight"]["modelled"] is False
    assert "Views" not in out["label"]
    assert "reweights the remaining factors" in out["caveat"]


def test_horizon_does_not_treat_missing_direction_as_open(monkeypatch):
    # Center + 8 directions x 5 samples. Only north has data; the other seven
    # directions must be marked missing, not counted as downhill/open at -90°.
    elevations = [100.0] + [100.0] * 5 + [None] * 35
    monkeypatch.setattr(vs, "_sample_elevations", lambda *_: elevations)

    out = vs._horizon_openness_factor(-37.81, 144.96)

    assert out is None


def test_partial_horizon_scales_its_weight_by_directional_coverage(monkeypatch):
    # Six clear directions and two gaps meet the minimum, but may contribute
    # only 75% of the horizon factor's nominal weight.
    elevations = [100.0] + [100.0] * 30 + [None] * 10
    monkeypatch.setattr(vs, "_sample_elevations", lambda *_: elevations)

    out = vs._horizon_openness_factor(-37.81, 144.96)

    assert out["sampled_directions"] == 6
    assert out["coverage_fraction"] == 0.75
    assert out["degraded"] is True
