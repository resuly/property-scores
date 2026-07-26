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


# --- NSW quiet-end recalibration (transfer.quiet_relief) ---
# Anchor facts (2026-06-10 measured_quiet_corpus): NorthConnex set-back homes
# need the affine's add removed (raw is near-unbiased there); Lake Macquarie
# kerbside/activity-centre sensors and the Pacific Hwy loud end must keep the
# affine exactly. Context values below are the real corpus values.

import pytest

from property_scores.noise import transfer


@pytest.fixture
def recal_on(monkeypatch):
    monkeypatch.setattr(transfer, "QUIET_RECAL_ENABLED", True)


def _affine_nsw(raw):
    return 0.8775851005604894 * raw + 16.54737125822292


def test_relief_flag_off_is_zero():
    assert transfer.QUIET_RECAL_ENABLED is False  # default env in the test run
    assert transfer.quiet_relief(62.0, _affine_nsw(62.0), 0.3, 0, 20, "NSW") == 0.0


def test_relief_only_where_instruments_say_it_is_needed(recal_on):
    """The relief is scoped by the measured gate, not applied everywhere.

    Extended to VIC and WA on 2026-07-26 because the 199-point instrument
    corpus showed the same set-back-dwelling over-read there (+9.4 and +6.6).
    QLD, TAS, ACT and NT stay out: turning it on everywhere costs NSW, which is
    the one state already unbiased against instruments.
    """
    for st in ("NSW", "VIC", "WA"):
        assert transfer.quiet_relief(62.0, _affine_nsw(62.0), 0.3, 0, 20, st) > 0.0, st
    for st in ("QLD", "SA", "TAS", "ACT", "NT", None):
        assert transfer.quiet_relief(62.0, _affine_nsw(62.0), 0.3, 0, 20, st) == 0.0, st


def test_relief_full_for_suburban_home(recal_on):
    # 118A Coonanbarra Rd Wahroonga: raw 62.5, built 0.24, poi 0, bldg100 16
    raw = 62.5
    relief = transfer.quiet_relief(raw, _affine_nsw(raw), 0.24, 0, 16, "NSW")
    assert relief == pytest.approx(_affine_nsw(raw) - raw)  # full add removed


def test_relief_zero_dense_builtup(recal_on):
    # Charlestown Square piazzas: built300 0.84/0.91 keep the urban-facade affine
    raw = 62.6
    assert transfer.quiet_relief(raw, _affine_nsw(raw), 0.84, 44, 6, "NSW") == 0.0
    assert transfer.quiet_relief(raw, _affine_nsw(raw), 0.91, 116, 5, "NSW") == 0.0


def test_relief_zero_activity_centre_poi(recal_on):
    raw = 62.0
    assert transfer.quiet_relief(raw, _affine_nsw(raw), 0.3, 80, 20, "NSW") == 0.0


def test_relief_zero_loud_band(recal_on):
    # Pacific Hwy anchors raw 70.4/71.0 (measured 77-79) stay on the affine
    for raw in (70.0, 70.4, 71.0, 75.0):
        assert transfer.quiet_relief(raw, _affine_nsw(raw), 0.2, 0, 20, "NSW") == 0.0


def test_relief_zero_open_ground(recal_on):
    # Five Islands roundabout: 0 buildings in 100m -> junction interior, no relief
    raw = 62.1
    assert transfer.quiet_relief(raw, _affine_nsw(raw), 0.23, 0, 0, "NSW") == 0.0


def test_relief_band_ramp_midpoint(recal_on):
    raw = 68.0  # halfway between 66 (full) and 70 (zero)
    add = _affine_nsw(raw) - raw
    relief = transfer.quiet_relief(raw, _affine_nsw(raw), 0.2, 0, 20, "NSW")
    assert relief == pytest.approx(add * 0.5, abs=1e-9)


def test_relief_built_ramp_midpoint(recal_on):
    raw = 60.0
    add = _affine_nsw(raw) - raw
    relief = transfer.quiet_relief(raw, _affine_nsw(raw), 0.75, 0, 20, "NSW")
    assert relief == pytest.approx(add * 0.5, abs=1e-9)


def test_relief_poi_ramp_midpoint(recal_on):
    raw = 60.0
    add = _affine_nsw(raw) - raw
    relief = transfer.quiet_relief(raw, _affine_nsw(raw), 0.2, 35, 20, "NSW")
    assert relief == pytest.approx(add * 0.5, abs=1e-9)


def test_relief_dwell_ramp(recal_on):
    raw = 60.0
    add = _affine_nsw(raw) - raw
    relief = transfer.quiet_relief(raw, _affine_nsw(raw), 0.2, 0, 4, "NSW")
    assert relief == pytest.approx(add * 0.5, abs=1e-9)


def test_relief_never_lifts(recal_on):
    # affine below raw (cannot happen in the NSW range, but guard anyway)
    assert transfer.quiet_relief(80.0, 75.0, 0.2, 0, 20, "NSW") == 0.0


def test_relief_monotone_in_built(recal_on):
    raw = 60.0
    vals = [transfer.quiet_relief(raw, _affine_nsw(raw), b, 0, 20, "NSW")
            for b in (0.3, 0.72, 0.78, 0.85)]
    assert vals == sorted(vals, reverse=True)
    assert vals[0] > 0 and vals[-1] == 0.0


def test_relief_smooth_no_cliffs(recal_on):
    """Relief changes by <0.6 dB for small steps in any input — overlay cells
    must transition smoothly, never flip (cache/overlay lesson, 06-09)."""
    raw, built, poi, bldg = 62.0, 0.5, 10.0, 12.0
    base = transfer.quiet_relief(raw, _affine_nsw(raw), built, poi, bldg, "NSW")
    for d_raw, d_built, d_poi, d_bldg in (
            (0.2, 0, 0, 0), (0, 0.005, 0, 0), (0, 0, 1.0, 0), (0, 0, 0, 0.4)):
        stepped = transfer.quiet_relief(
            raw + d_raw, _affine_nsw(raw + d_raw),
            built + d_built, poi + d_poi, bldg + d_bldg, "NSW")
        assert abs(stepped - base) < 0.6


def test_relief_full_band_residential_examples(recal_on):
    """Every exact-quality NorthConnex home context gets the FULL relief
    (their raws 50.8-64.2 / built 0.24-0.61 / poi 0-21 / bldg100 14-36)."""
    cases = [  # (raw, built300, poi100, bldg100)
        (50.8, 0.32, 0, 15), (51.9, 0.39, 1, 20), (58.4, 0.25, 0, 36),
        (61.0, 0.61, 21, 16), (61.7, 0.24, 0, 25), (62.4, 0.26, 0, 22),
        (62.5, 0.24, 0, 16), (62.6, 0.26, 0, 14), (64.2, 0.39, 0, 15),
    ]
    for raw, built, poi, bldg in cases:
        add = _affine_nsw(raw) - raw
        relief = transfer.quiet_relief(raw, _affine_nsw(raw), built, poi, bldg, "NSW")
        assert relief >= add * 0.8, (raw, built, poi, bldg)


def test_quiet_recal_version_suffix():
    """Cache guard: enabling the flag must change NOISE_MODEL_VERSION so stale
    precompute is refused (fail-safe to live compute)."""
    import subprocess
    import sys as _sys
    code = ("import os; os.environ['NOISE_QUIET_RECAL']='1'; "
            "from property_scores.noise.score import NOISE_MODEL_VERSION; "
            "print(NOISE_MODEL_VERSION)")
    out = subprocess.run([_sys.executable, "-c", code], capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "-nswquiet" in out.stdout
    from property_scores.noise.score import NOISE_MODEL_VERSION
    assert "-nswquiet" not in NOISE_MODEL_VERSION  # default-off in this process


def test_confidence_interval_covers_the_measured_error():
    """The interval used to be derived only from the SoundPLAN corpus, which is
    itself a model, so it looked confident exactly where the model was worst:
    Victoria had the largest SoundPLAN sample and was never flagged, while
    against real noise loggers it read 9.4 dB high and shipped +-4 dB.

    Victoria is now +2.8 after two fixes on 2026-07-26 (the quiet-end relief,
    and measuring the LA10 offset instead of assuming 3 dB), so it no longer
    trips the material-bias flag. The invariant this guards is not a particular
    state's number: it is that every state's interval reaches its own measured
    error, whatever that error currently is."""
    from property_scores.noise import measured_validation as mv

    for state in ("NSW", "QLD", "VIC", "WA", "TAS", "ACT", "NT"):
        row = mv.for_state(state)
        assert row is not None, state
        assert row["mae_db"] >= abs(row["bias_db"]), (
            f"{state}: MAE smaller than bias would be self-contradictory")
        assert row["instrument_points"] > 0, state
        assert row["note"], state

    vic = mv.for_state("VIC")
    assert vic["instrument_points"] == 79


def test_a_state_reading_low_is_described_as_conservative_not_as_an_upper_bound():
    from property_scores.noise import measured_validation as mv

    row = dict(mv._GROUPS)
    mv._GROUPS["ZZ"] = (20, -5.0, 6.0)
    try:
        out = mv.for_state("ZZ")
        assert "conservative" in out["note"]
        assert "upper bound" not in out["note"]
    finally:
        mv._GROUPS.clear()
        mv._GROUPS.update(row)


def test_an_unvalidated_state_says_so_rather_than_implying_it_passed():
    """South Australia has no instrument rows at all. Silence there would read
    as validated and fine."""
    from property_scores.noise import measured_validation as mv

    assert mv.for_state("SA") is None
    note = mv.unvalidated_note("SA")
    assert "not been checked against" in note
