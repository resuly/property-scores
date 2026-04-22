"""Unit tests for noise score formula (no network calls)."""

import math
from property_scores.noise.score import (
    _crtn_noise, _energy_sum, _lden, _estimate_aadt, _adaptive_select,
    CLASS_TO_AADT, GROUND_ABSORPTION_DB, MIN_DISTANCE_M,
)


def test_crtn_motorway_close_is_loud():
    aadt = CLASS_TO_AADT["motorway"]
    db_level = _crtn_noise(aadt, 50)
    assert db_level > 60


def test_crtn_residential_far_is_quiet():
    aadt = CLASS_TO_AADT["residential"]
    db_level = _crtn_noise(aadt, 500)
    assert db_level < 30


def test_distance_attenuates():
    aadt = CLASS_TO_AADT["primary"]
    close = _crtn_noise(aadt, 20)
    far = _crtn_noise(aadt, 200)
    assert close > far


def test_energy_summation():
    aadt = CLASS_TO_AADT["residential"]
    single = _crtn_noise(aadt, 100)
    total = _energy_sum(single, single, single)
    assert total > single
    assert total < single + 5  # 3 equal sources add ~4.8 dB


def test_lden_night_penalty():
    leq = 50.0
    lden = _lden(leq, leq, leq)
    assert lden > leq  # night +10 dB penalty raises Lden above flat Leq


def test_estimate_aadt_with_speed():
    aadt = _estimate_aadt("residential", 60)
    assert aadt == 8_000  # maps to speed bucket 60


def test_estimate_aadt_class_fallback():
    aadt = _estimate_aadt("tertiary", None)
    assert aadt == CLASS_TO_AADT["tertiary"]


def test_zero_aadt_returns_zero():
    assert _crtn_noise(0, 50) == 0.0


def test_hv_correction_increases_noise():
    base = _crtn_noise(10_000, 100)
    with_hv = _crtn_noise(10_000, 100, hv_pct=15.0, speed_kmh=60)
    assert with_hv > base


def test_hv_correction_zero_pct_no_change():
    base = _crtn_noise(10_000, 100)
    with_zero_hv = _crtn_noise(10_000, 100, hv_pct=0.0, speed_kmh=60)
    assert with_zero_hv == base


def test_hv_freight_corridor():
    base = _crtn_noise(50_000, 50)
    freight = _crtn_noise(50_000, 50, hv_pct=25.0, speed_kmh=80)
    delta = freight - base
    assert 1.0 < delta < 5.0


def test_adaptive_select_empty():
    assert _adaptive_select([]) == []


def test_adaptive_select_within_threshold():
    levels = [(70.0, {"a": 1}), (65.0, {"b": 2}), (55.0, {"c": 3})]
    result = _adaptive_select(levels)
    assert len(result) == 2  # 70 and 65 within 6dB, 55 dropped


def test_adaptive_select_single():
    levels = [(50.0, {"a": 1})]
    result = _adaptive_select(levels)
    assert len(result) == 1


def test_adaptive_select_all_close():
    levels = [(60.0, {}), (58.0, {}), (55.0, {}), (52.0, {})]
    result = _adaptive_select(levels)
    assert len(result) == 3  # 60, 58, 55 within 6dB; 52 dropped (60-52=8>6)


def test_score_range():
    pass
