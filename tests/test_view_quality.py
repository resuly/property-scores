"""View elevation-advantage tests — the valley-rim directional credit.

Anchor (2026-06-12 Simon Kean round 2): "uninterrupted views north over
National Parks up the valley from all main rooms for 6km" scored as flat
terrain, because the ring MEDIANS are direction-agnostic: a ridge suburb
behind the house cancels the valley in front of it.
"""
from unittest import mock

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
