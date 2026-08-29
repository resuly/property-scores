from property_scores.view_quality.score import _coastal_escarpment_floor


def _factors(**overrides):
    factors = {
        "ocean_proximity": {"distance_m": 96, "value": 1.0},
        "elevation_advantage": {"advantage_m": 40.7, "value": 0.85},
        "horizon_openness": {"open_directions": 4, "value": 0.5},
    }
    for key, value in overrides.items():
        factors[key].update(value)
    return factors


def test_elevated_open_coastal_escarpment_gets_great_floor():
    assert _coastal_escarpment_floor(_factors()) == 68


def test_flat_beach_does_not_get_escarpment_floor():
    assert _coastal_escarpment_floor(_factors(
        elevation_advantage={"advantage_m": 5})) is None


def test_inland_ridge_does_not_get_coastal_floor():
    assert _coastal_escarpment_floor(_factors(
        ocean_proximity={"distance_m": 1200})) is None


def test_obstructed_coastal_site_does_not_get_floor():
    assert _coastal_escarpment_floor(_factors(
        horizon_openness={"open_directions": 3})) is None
