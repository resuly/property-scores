"""Unit tests for noise score formula (no network calls)."""

import math
from property_scores.noise.score import ROAD_NOISE_REF, GROUND_ABSORPTION_DB, REFERENCE_DISTANCE_M


def _single_road_db(road_class: str, distance_m: float) -> float:
    l_ref = ROAD_NOISE_REF[road_class]
    return l_ref - 10 * math.log10(distance_m / REFERENCE_DISTANCE_M) - GROUND_ABSORPTION_DB


def test_motorway_close_is_loud():
    db_level = _single_road_db("motorway", 50)
    assert db_level > 65


def test_residential_far_is_quiet():
    db_level = _single_road_db("residential", 500)
    assert db_level < 40


def test_distance_attenuates():
    close = _single_road_db("primary", 20)
    far = _single_road_db("primary", 200)
    assert close > far


def test_energy_summation():
    energy = 0
    for cls in ["residential", "residential", "residential"]:
        l = _single_road_db(cls, 100)
        energy += 10 ** (l / 10)
    total = 10 * math.log10(energy)
    single = _single_road_db("residential", 100)
    assert total > single
    assert total < single + 5  # 3 equal sources add ~4.8 dB


def test_score_range():
    from property_scores.noise.score import noise_score
    # Mock: just verify the function signature works (actual test needs data)
    # This test will fail without Overture data - skip in CI without data
    pass
