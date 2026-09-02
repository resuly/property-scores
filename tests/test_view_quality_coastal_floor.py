"""Coastal escarpment floor: gated on the seaward arc, not on 4 of 8 sectors.

2026-09-03: the 30 m -> 5 m DEM change moved Dover Heights' S sector from
below to 3.5 degrees, open_directions went 4 -> 3, and the floor vanished on a
0.5 degree margin that has nothing to do with the sea in front of the site.
"""
from property_scores.view_quality.score import (
    COASTAL_FLOOR_SCORE, _coastal_escarpment_basis, _coastal_escarpment_floor,
    _seaward_sectors)

# Dover Heights, production 2026-09-03 on 5 m LiDAR.
_DOVER_ANGLES = {"N": 3.8, "NE": -1.1, "E": -1.1, "SE": -1.1,
                 "S": 3.5, "SW": 5.7, "W": 5.2, "NW": 9.1}


def _factors(**overrides):
    factors = {
        "ocean_proximity": {"distance_m": 96, "bearing_deg": 88.0, "value": 1.0},
        "elevation_advantage": {"advantage_m": 34.6, "value": 0.85},
        "horizon_openness": {"open_directions": 3, "value": 0.375,
                             "horizon_angles": dict(_DOVER_ANGLES)},
    }
    for key, value in overrides.items():
        factors[key].update(value)
    return factors


def test_seaward_sectors_are_the_facing_sector_and_its_neighbours():
    assert _seaward_sectors(88.0) == ["NE", "E", "SE"]
    assert _seaward_sectors(0.0) == ["NW", "N", "NE"]
    assert _seaward_sectors(350.0) == ["NW", "N", "NE"]
    assert _seaward_sectors(200.0) == ["SE", "S", "SW"]


def test_dover_heights_with_only_three_open_sectors_keeps_the_floor():
    assert _coastal_escarpment_floor(_factors()) == COASTAL_FLOOR_SCORE == 68
    basis = _coastal_escarpment_basis(_factors())
    assert basis["seaward_sectors"] == ["NE", "E", "SE"]
    assert basis["seaward_horizon_deg"] == {"NE": -1.1, "E": -1.1, "SE": -1.1}
    assert basis["ocean_bearing_deg"] == 88.0


def test_flat_beach_does_not_get_escarpment_floor():
    assert _coastal_escarpment_floor(_factors(
        elevation_advantage={"advantage_m": 5})) is None


def test_inland_ridge_does_not_get_coastal_floor():
    assert _coastal_escarpment_floor(_factors(
        ocean_proximity={"distance_m": 1200})) is None


def test_ridge_between_site_and_sea_blocks_the_floor():
    angles = dict(_DOVER_ANGLES, E=6.0)
    assert _coastal_escarpment_floor(_factors(
        horizon_openness={"horizon_angles": angles})) is None


def test_open_inland_sectors_do_not_substitute_for_the_seaward_arc():
    # Seven open sectors but the one facing the sea is a ridge.
    angles = {k: -1.0 for k in _DOVER_ANGLES}
    angles["E"] = 4.0
    assert _coastal_escarpment_floor(_factors(
        horizon_openness={"open_directions": 7, "horizon_angles": angles})) is None


def test_missing_bearing_or_missing_seaward_sample_gives_no_floor():
    assert _coastal_escarpment_floor(_factors(
        ocean_proximity={"bearing_deg": None})) is None
    angles = dict(_DOVER_ANGLES, SE=None)
    assert _coastal_escarpment_floor(_factors(
        horizon_openness={"horizon_angles": angles})) is None


def test_open_threshold_is_strict():
    angles = dict(_DOVER_ANGLES, E=3.0)
    assert _coastal_escarpment_floor(_factors(
        horizon_openness={"horizon_angles": angles})) is None
    angles["E"] = 2.9
    assert _coastal_escarpment_floor(_factors(
        horizon_openness={"horizon_angles": angles})) == 68
