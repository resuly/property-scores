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


def test_pessimistic_state_note_carries_measured_counterevidence():
    """NSW used to ship 'Lower model confidence in NSW (limited local
    calibration data)' while measured_validation right next to it said 55
    instrument points, bias -0.9 — both true, but self-contradictory to a
    customer. When measured bias is below the material threshold, the
    transfer-side pessimism must carry the measured counter-evidence.
    low_confidence and ci_db stay untouched (copy fix, not a model change)."""
    from property_scores.noise import measured_validation as mv
    from property_scores.noise.score import _apply_measured_disclosure

    nsw = mv.for_state("NSW")
    assert abs(nsw["bias_db"]) < mv.MATERIAL_BIAS_DB, \
        "premise: NSW bias is sub-material; if this moved, revisit the note logic"

    pessimistic = ("Lower model confidence in NSW (limited local "
                   "calibration data). Verify on site before relying on this estimate.")
    ci, low, note = _apply_measured_disclosure(
        "NSW", nsw, ci_db=8.0, low_confidence=True,
        confidence_note=pessimistic, state_note_from_transfer=True)
    assert "noise-logger readings" in note and "-0.9" in note, note
    assert "limited NSW sample" in note  # 校准样本小这半句也要在
    assert low is True and ci >= 8.0     # 姿态与 interval 不动

    # 反向不变量: note 不是 transfer 侧的(比如 quiet 外推 note)时不许被顶掉
    quiet_note = "Quiet, low-density area ... extrapolated. Verify on site."
    _, _, note2 = _apply_measured_disclosure(
        "NSW", nsw, ci_db=8.0, low_confidence=True,
        confidence_note=quiet_note, state_note_from_transfer=False)
    assert note2 == quiet_note

    # 材料级 bias 仍走实测降级路径(规则②未被规则③影响)
    material = dict(nsw, bias_db=9.4, note="reads +9.4 dB on average ...")
    _, low3, note3 = _apply_measured_disclosure(
        "VIC", material, ci_db=4.0, low_confidence=False,
        confidence_note=None, state_note_from_transfer=False)
    assert low3 is True and note3 == material["note"]


def test_rail_screening_factor_ramps_from_zero_to_cap():
    """0 at 0m, linear (dist/500) up to the 0.6 cap, which distance/500
    reaches at 300m -- so it's flat 0.6 from 300m onward, not 500m."""
    from property_scores.noise.score import _rail_screening_factor

    assert _rail_screening_factor(0) == 0.0
    assert round(_rail_screening_factor(250), 4) == 0.5
    assert _rail_screening_factor(300) == 0.6
    assert _rail_screening_factor(500) == 0.6
    assert _rail_screening_factor(1000) == 0.6  # capped, not 2.0


def test_debug_reuses_scores_rail_screening_factor():
    """debug.py (the /map + Inspector sources feed) must share score.py's
    rail screening ramp, not redefine it. Until 2026-08 debug.py hardcoded a
    flat 0.6 factor while score.py ramped 0 at 0m -> 0.6 at 500m, so a rail
    source 70-100m away with tall building screening between it and the
    receiver showed up to ~7dB quieter on the map than in the actual score
    (measured against Glenferrie/Richmond/Malvern/Hawthorn test points,
    2026-08 followup daleads-noise-map-vs-api-screening-mismatch). Asserting
    identity (not just equal output for one input) means a future
    reimplementation that merely matches today's numbers won't silently
    re-diverge tomorrow."""
    import property_scores.noise.debug as debug_mod
    import property_scores.noise.score as score_mod

    assert debug_mod._rail_screening_factor is score_mod._rail_screening_factor


def test_debug_rail_source_screening_matches_score_formula(monkeypatch):
    """Behavioural check on top of the identity check above: drive
    noise_debug's rail loop with synthetic inputs (no DB/network) and confirm
    the screening_db it reports for a rail source is raw_screening * the
    ramped factor -- the exact bug shape would have reported
    raw_screening * 0.6 regardless of distance."""
    import property_scores.noise.debug as debug_mod

    dist_m = 100.0  # ramp factor here is 0.2, nowhere near the old flat 0.6
    raw_screening_db = 15.0
    peak_svc, offpeak_svc = 6.0, 3.0

    monkeypatch.setattr(debug_mod, "get_db", lambda: object())
    monkeypatch.setattr(debug_mod, "noise_score", lambda *a, **k: {})
    monkeypatch.setattr(debug_mod, "aadt_near", lambda *a, **k: [])
    monkeypatch.setattr(debug_mod, "nfdh_near", lambda *a, **k: [])
    monkeypatch.setattr(debug_mod, "rail_near", lambda *a, **k: [])
    monkeypatch.setattr(debug_mod, "roads_near", lambda *a, **k: [])
    monkeypatch.setattr(debug_mod, "buildings_in_radius", lambda *a, **k: [])
    monkeypatch.setattr(debug_mod, "barrier_attenuation", lambda *a, **k: raw_screening_db)
    monkeypatch.setattr(debug_mod, "_nearest_stop_name", lambda *a, **k: "")
    monkeypatch.setattr(debug_mod, "_rail_shapes_near", lambda *a, **k: [])
    monkeypatch.setattr(
        debug_mod, "gtfs_rail_near",
        lambda *a, **k: [(1, "Test Line", dist_m, peak_svc, offpeak_svc, 145.0, -37.8)],
    )

    result = debug_mod.noise_debug(-37.8, 145.0, radius_m=500, include_overture_roads=False)
    rail_sources = result["sources"]["rail"]
    assert len(rail_sources) == 1
    got_screening = rail_sources[0]["screening_db"]
    expected_factor = dist_m / 500  # 0.2, well under the 0.6 cap
    assert round(got_screening, 2) == round(raw_screening_db * expected_factor, 2)
    assert got_screening != round(raw_screening_db * 0.6, 2), \
        "this is exactly the old bug: flat 0.6 regardless of distance"


def test_result_cache_key_carries_model_config():
    """Cache guard for the 2026-08-03 incident: an env-less CLI probe wrote a
    physics row into the shared result cache and a NOISE_TRANSFER=1 export read
    it back. The key must differ across every env that changes noise numbers.

    Since 2026-08-18 NOISE_MODEL_VERSION carries those envs too (it gates the
    precomputed grids, and used not to), so this signature and that string now
    overlap. This test still owns the result-cache half: it asserts the key
    reaches _ck, which the version string alone does not."""
    import os
    import subprocess
    import sys as _sys

    def sig(env_pairs):
        sets = "".join(f"os.environ['{k}']='{v}'; " for k, v in env_pairs)
        code = ("import os; " + sets +
                "from property_scores.noise.score import _CONFIG_SIG; "
                "print(_CONFIG_SIG)")
        # A runner with NOISE_* already exported would make base == variant for
        # the inherited flag; strip them so the child sees only env_pairs.
        clean = {k: v for k, v in os.environ.items()
                 if not k.startswith("NOISE_")}
        out = subprocess.run([_sys.executable, "-c", code], capture_output=True,
                             text=True, timeout=120, env=clean)
        assert out.returncode == 0, out.stderr
        return out.stdout.strip()

    base = sig([])
    for pairs in ([("NOISE_TRANSFER", "1")],
                  [("NOISE_ML_CORRECTION", "1")],
                  [("NOISE_RAIL_RECAL", "1")],
                  [("NOISE_RAIL_RECAL", "1"), ("NOISE_RAIL_RECAL_DB", "5.0")],
                  [("NOISE_MODEL_ID", "some-other-model")]):
        assert sig(pairs) != base, pairs
    # and the sig actually reaches the key
    import inspect
    from property_scores.noise import score as _s
    src = inspect.getsource(_s.noise_score)
    assert "_CONFIG_SIG" in src.split("_cache_get")[0], \
        "config signature must be part of _ck before the cache lookup"


# --- Grid-cache configuration guard (2026-08-18) -----------------------------
# The result cache has carried the noise envs in _CONFIG_SIG since 2026-08-03
# (test_result_cache_key_carries_model_config above). The PRECOMPUTED GRIDS did
# not: NOISE_MODEL_VERSION carried only the aadt/quiet/rail on-off state, so a
# grid baked under one configuration was served unchallenged by a process
# running another. These tests pin the fold.

def _model_version(env_pairs):
    """NOISE_MODEL_VERSION as computed by a fresh process under env_pairs.

    Subprocess because the module reads os.environ once at import. NOISE_* is
    stripped from the inherited environment so a runner that already exports a
    flag cannot make base == variant.
    """
    import os
    import subprocess
    import sys as _sys

    sets = "".join(f"os.environ['{k}']='{v}'; " for k, v in env_pairs)
    code = ("import os; " + sets +
            "from property_scores.noise.score import NOISE_MODEL_VERSION; "
            "print(NOISE_MODEL_VERSION)")
    clean = {k: v for k, v in os.environ.items() if not k.startswith("NOISE_")}
    out = subprocess.run([_sys.executable, "-c", code], capture_output=True,
                         text=True, timeout=180, env=clean)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_every_number_moving_env_changes_the_grid_version():
    """Each env that changes the scores must change the grid stamp.

    Without this, cache.py's version guard compares equal across two
    configurations that produce different numbers, which is precisely how the
    melbourne-inner grid shadowed the live model for six weeks (2026-08-04).
    """
    base = _model_version([])
    variants = {
        "transfer": [("NOISE_TRANSFER", "1")],
        "ml_correction": [("NOISE_ML_CORRECTION", "1")],
        "rail_recal": [("NOISE_RAIL_RECAL", "1")],
        "aadt_adjust": [("NOISE_AADT_ADJUST", "1")],
        "quiet_recal": [("NOISE_QUIET_RECAL", "1")],
    }
    for name, pairs in variants.items():
        assert _model_version(pairs) != base, f"{name} does not move the version"

    # Combinations must not collide with each other either: two different
    # configurations sharing a stamp is the same failure as a config sharing a
    # stamp with the default.
    combos = [[], [("NOISE_TRANSFER", "1")], [("NOISE_ML_CORRECTION", "1")],
              [("NOISE_TRANSFER", "1"), ("NOISE_ML_CORRECTION", "1")]]
    seen = [_model_version(c) for c in combos]
    assert len(set(seen)) == len(seen), seen


def test_value_tunables_are_folded_as_values_not_as_flags():
    """NOISE_RAIL_RECAL_DB=8 and =5 are different scores from the same flag.

    A boolean suffix would let a grid baked at 8 dB be served by a process
    running 5 dB. Same for the AADT adjustment strength K.
    """
    rail_8 = _model_version([("NOISE_RAIL_RECAL", "1"),
                             ("NOISE_RAIL_RECAL_DB", "8.0")])
    rail_5 = _model_version([("NOISE_RAIL_RECAL", "1"),
                             ("NOISE_RAIL_RECAL_DB", "5.0")])
    assert rail_8 != rail_5, (rail_8, rail_5)

    k4 = _model_version([("NOISE_AADT_ADJUST", "1"),
                         ("NOISE_AADT_ADJUST_K", "4.0")])
    k2 = _model_version([("NOISE_AADT_ADJUST", "1"),
                         ("NOISE_AADT_ADJUST_K", "2.0")])
    assert k4 != k2, (k4, k2)


def test_default_off_version_is_unchanged_so_existing_grids_stay_valid():
    """Every suffix is empty when its flag is off.

    The fold must not invalidate the grids of a deployment whose configuration
    did not change. A deliberate bump of the DATE TOKEN is expected to update
    this literal (that is the sanctioned half of the process rule, and it comes
    with a re-bake). A new SUFFIX turning up here is not: it means some default
    is now leaking into the string, and every default-configured region silently
    loses its grid.
    """
    assert _model_version([]) == "2026-06-09-quincunx"


def test_grid_baked_under_another_config_is_refused_by_the_reader(tmp_path,
                                                                 monkeypatch):
    """End to end through cache.lookup: the stamp actually gates the serving.

    A version string that differs is worthless if the loader still hands the
    rows out, so this drives the real loader over a real parquet rather than
    comparing strings.
    """
    import pandas as pd
    from property_scores.noise import cache as noise_cache
    from property_scores.noise.score import NOISE_MODEL_VERSION

    # The loader memoises into module globals, so restore them: leaving
    # _loaded=True with an empty _cache would make every later test in the
    # session see no grids at all, which is a silence that looks like a pass.
    monkeypatch.setattr(noise_cache, "_cache", dict(noise_cache._cache))
    monkeypatch.setattr(noise_cache, "_loaded", noise_cache._loaded)

    def bake(version):
        df = pd.DataFrame([{
            "lat": -37.8100, "lng": 144.9600, "score": 42,
            "estimated_db": 61.0, "road_db": 60.0, "rail_db": 40.0,
            "label": "moderate", "dominant_source": "main road",
            "model_version": version,
        }])
        path = tmp_path / "noise_cache_testregion.parquet"
        df.to_parquet(path, index=False)
        monkeypatch.setattr(noise_cache, "DATA_DIR", tmp_path)
        noise_cache._cache = {}
        noise_cache._loaded = False
        return noise_cache.lookup(-37.8100, 144.9600)

    # Same stamp as this process: served.
    hit = bake(NOISE_MODEL_VERSION)
    assert hit is not None and hit["score"] == 42, hit

    # Baked by a transfer-configured process, read by this (default) one. The
    # 2026-08-04 shape of the bug, and the reason the env has to be in here.
    assert bake(NOISE_MODEL_VERSION + "-transfer") is None
    # And the value-tunable shape of it.
    assert bake(NOISE_MODEL_VERSION + "-nswrail5") is None
    # A grid with no stamp at all (pre-versioning) is refused too.
    assert bake(None) is None


def _fake_model_dir(tmp_path, ids, active):
    """Minimal on-disk model registry: the loader only checks these two files."""
    import json

    root = tmp_path / "models" / "noise"
    for mid in ids:
        d = root / mid
        d.mkdir(parents=True, exist_ok=True)
        (d / "rf.pkl").write_bytes(b"not a real model")
        (d / "calibration.json").write_text("{}")
    root.mkdir(parents=True, exist_ok=True)
    (root / "registry.json").write_text(json.dumps(
        {"active": active, "versions": {m: {"status": "active"} for m in ids}}))
    return root


def test_swapping_the_transfer_model_changes_the_grid_version(tmp_path):
    """The model swap is the number-moving change NO env variable can see.

    `noise_model.py activate` edits registry.json and restarts; nothing in the
    environment differs afterwards. Before 2026-08-18 the grids baked by the
    model that was just rolled back FROM kept being served by the process
    running the model rolled back TO.
    """
    _fake_model_dir(tmp_path, ["mdl-a", "mdl-b"], active="mdl-a")
    data_dir = [("DATA_DIR", str(tmp_path))]

    # (a) explicit rollback via the env var
    by_env_a = _model_version(data_dir + [("NOISE_TRANSFER", "1"),
                                          ("NOISE_MODEL_ID", "mdl-a")])
    by_env_b = _model_version(data_dir + [("NOISE_TRANSFER", "1"),
                                          ("NOISE_MODEL_ID", "mdl-b")])
    assert by_env_a != by_env_b, (by_env_a, by_env_b)

    # (b) swap via the registry, with the environment held IDENTICAL. This is
    # the case an env-only token cannot catch.
    from_registry_a = _model_version(data_dir + [("NOISE_TRANSFER", "1")])
    assert from_registry_a == by_env_a, "registry and env must resolve alike"
    _fake_model_dir(tmp_path, ["mdl-a", "mdl-b"], active="mdl-b")
    from_registry_b = _model_version(data_dir + [("NOISE_TRANSFER", "1")])
    assert from_registry_b != from_registry_a, (from_registry_a, from_registry_b)


def test_unresolvable_model_does_not_share_a_stamp_with_a_healthy_one(tmp_path):
    """A typo'd NOISE_MODEL_ID makes model_registry raise, and the process then
    serves physics. It must not stamp its grids like a working transfer box."""
    _fake_model_dir(tmp_path, ["mdl-a"], active="mdl-a")
    healthy = _model_version([("DATA_DIR", str(tmp_path)),
                              ("NOISE_TRANSFER", "1")])
    broken = _model_version([("DATA_DIR", str(tmp_path)),
                             ("NOISE_TRANSFER", "1"),
                             ("NOISE_MODEL_ID", "typo-not-a-model")])
    assert broken != healthy, (broken, healthy)
    assert "mdlerr" in broken, broken


def test_model_id_does_not_touch_the_version_on_the_physics_path(tmp_path):
    """Transfer off means the RF is never loaded, so which model is 'active' has
    no effect on the numbers. Folding it in anyway would invalidate every
    physics grid on a model swap those grids cannot see."""
    _fake_model_dir(tmp_path, ["mdl-a", "mdl-b"], active="mdl-b")
    plain = _model_version([("DATA_DIR", str(tmp_path))])
    with_id = _model_version([("DATA_DIR", str(tmp_path)),
                              ("NOISE_MODEL_ID", "mdl-a")])
    from property_scores.noise.score import _MODEL_DATE
    assert plain == with_id == _MODEL_DATE, (plain, with_id)


def test_the_fold_does_not_flush_the_result_cache_or_the_model_stamp():
    """The grid stamp tightened on 2026-08-18; the result-cache key must not.

    The fold changes no numbers. If it reached _CONFIG_SIG it would change the
    sqlite result-cache key AND, through api/stamp.py, the model stamp DA Leads
    keys its per-parcel score cache on, flushing both estate-wide to recompute
    identical values.

    The expectation is the PRE-FOLD RECIPE (date + boolean flags), rebuilt from
    _MODEL_DATE rather than frozen as a literal. A frozen literal here would
    make the sanctioned action -- bumping the date for a real scoring change,
    which must flush these caches -- look like a regression, and the previous
    version of this test did exactly that.
    """
    import os
    import subprocess
    import sys as _sys

    def probe(env_pairs):
        sets = "".join(f"os.environ['{k}']='{v}'; " for k, v in env_pairs)
        code = ("import os; " + sets +
                "from property_scores.noise import score as s; "
                "print(s._CONFIG_SIG); print(s._MODEL_DATE)")
        clean = {k: v for k, v in os.environ.items()
                 if not k.startswith("NOISE_")}
        out = subprocess.run([_sys.executable, "-c", code], capture_output=True,
                             text=True, timeout=180, env=clean)
        assert out.returncode == 0, out.stderr
        sig, date = out.stdout.strip().splitlines()
        return sig, date

    def expected_first_field(date, aadt=False, quiet=False, rail=False):
        return (date + ("-aadt" if aadt else "") + ("-nswquiet" if quiet else "")
                + ("-nswrail" if rail else ""))

    sig, date = probe([])
    assert sig.split(":")[0] == expected_first_field(date)
    assert sig == f"{date}:src2:t0:m0:r-:k-:"

    # Production's configuration. The flags must still show in the t/m/r tokens
    # (that is the 2026-08-03 guard) while the first field stays on the pre-fold
    # recipe: no rail dB value, no -transfer, no model id.
    sig, date = probe([("NOISE_TRANSFER", "1"), ("NOISE_QUIET_RECAL", "1"),
                       ("NOISE_RAIL_RECAL", "1")])
    assert sig.split(":")[0] == expected_first_field(date, quiet=True, rail=True)
    assert sig == f"{date}-nswquiet-nswrail:src2:t1:m0:r8:k-:"

    # And the date really is shared, so a bump cannot reach one string only.
    from property_scores.noise.score import _MODEL_DATE, NOISE_MODEL_VERSION
    from property_scores.noise.score import _CONFIG_SIG_VERSION
    assert NOISE_MODEL_VERSION.startswith(_MODEL_DATE)
    assert _CONFIG_SIG_VERSION.startswith(_MODEL_DATE)
    import inspect
    from property_scores.noise import score as _s
    src = inspect.getsource(_s)
    assert src.count(f'"{_MODEL_DATE}"') == 1, \
        "the date must exist once, as _MODEL_DATE; a second copy can drift"


def test_two_model_ids_that_differ_only_in_punctuation_get_different_stamps(
        tmp_path):
    """The suffix scheme is dash-delimited, so the id has to be sanitised, and
    a naive sanitiser collapses "eu-transfer-v1" onto "eutransferv1". The live
    production id is "eu-transfer-v1", so that collision is one plausible
    upload away from letting one model serve the other's grids."""
    _fake_model_dir(tmp_path, ["eu-transfer-v1", "eutransferv1"],
                    active="eu-transfer-v1")
    dashed = _model_version([("DATA_DIR", str(tmp_path)),
                             ("NOISE_TRANSFER", "1"),
                             ("NOISE_MODEL_ID", "eu-transfer-v1")])
    squashed = _model_version([("DATA_DIR", str(tmp_path)),
                               ("NOISE_TRANSFER", "1"),
                               ("NOISE_MODEL_ID", "eutransferv1")])
    assert dashed != squashed, (dashed, squashed)
