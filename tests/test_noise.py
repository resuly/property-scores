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


# --- loud-end score re-anchor (shared _lden_to_score helper) ---

def test_lden_to_score_midrange_calibrated():
    from property_scores.noise.score import _lden_to_score
    assert _lden_to_score(40) == 100
    assert _lden_to_score(65) == 29  # (75-65)/35*100, the calibrated band
    assert _lden_to_score(70) == 14


def test_lden_to_score_loud_tail_rank_orders():
    from property_scores.noise.score import _lden_to_score
    # >70 dB must spread so 75 vs 88 rank-order instead of all collapsing to 0
    assert _lden_to_score(75) > _lden_to_score(80) > _lden_to_score(85)
    assert _lden_to_score(88) == 0


def test_lden_to_score_continuous_at_70():
    from property_scores.noise.score import _lden_to_score
    # both branches meet at 70 dB (no discontinuity)
    assert _lden_to_score(70) == 14


# --- AADT volume post-adjustment (transfer flattening fix) ---

def test_aadt_adjustment_quiet_class_pulls_down():
    from property_scores.noise.score import _aadt_adjustment
    # road much quieter than its class implies (5k actual vs 30k class) -> pull DOWN
    adj = _aadt_adjustment(dom_aadt=5_000, exp_aadt=30_000, k=4.0, max_db=12.0)
    assert adj < 0


def test_aadt_adjustment_busy_road_untouched():
    from property_scores.noise.score import _aadt_adjustment
    # road at or above class expectation (genuine arterial/freeway) -> no change
    assert _aadt_adjustment(dom_aadt=40_000, exp_aadt=30_000, k=4.0, max_db=12.0) == 0.0
    assert _aadt_adjustment(dom_aadt=30_000, exp_aadt=30_000, k=4.0, max_db=12.0) == 0.0


def test_aadt_adjustment_clamped():
    from property_scores.noise.score import _aadt_adjustment
    # extreme misclass (tiny actual vs huge expected) is clamped to -max_db, never explodes
    adj = _aadt_adjustment(dom_aadt=50, exp_aadt=50_000, k=4.0, max_db=12.0)
    assert adj == -12.0


def test_aadt_adjustment_no_data_is_noop():
    from property_scores.noise.score import _aadt_adjustment
    assert _aadt_adjustment(dom_aadt=0, exp_aadt=30_000) == 0.0


def test_score_label_matches_bands():
    from property_scores.noise.score import _score_label
    assert _score_label(85) == "Very Quiet"
    assert _score_label(70) == "Quiet"
    assert _score_label(50) == "Moderate"
    assert _score_label(31) == "Loud"      # the cell_score consistency case
    assert _score_label(10) == "Very Loud"


# --- defence ANEF MultiPolygon handling (Darwin slivers fix) ---

def _square_ring(clat, clng, half):
    return [[clng - half, clat - half], [clng + half, clat - half],
            [clng + half, clat + half], [clng - half, clat + half],
            [clng - half, clat - half]]


def test_defence_query_handles_multipolygon():
    """A point inside the SECOND sub-polygon of a MultiPolygon must be found —
    proves the Darwin fix (old code only saw the first/sliver piece)."""
    from property_scores.noise import aircraft
    feat = {
        "properties": {"anef_min": 30, "airfield": "Test Base"},
        "geometry": {"type": "MultiPolygon", "coordinates": [
            [_square_ring(-12.0, 130.0, 0.001)],   # tiny first piece (a sliver)
            [_square_ring(-12.5, 130.5, 0.02)],    # real second piece
        ]},
    }
    aircraft._defence_loaded = True
    aircraft._defence_features = [feat]
    try:
        hit = aircraft._query_defence(-12.5, 130.5)   # inside 2nd piece
        assert hit is not None and hit["anef_min"] == 30
        assert aircraft._query_defence(-20.0, 140.0) is None  # outside both
    finally:
        aircraft._defence_loaded = False
        aircraft._defence_features = []
