"""Grid geometry and the model-path assertion for /scores/noise/surface.

`noise_score` is stubbed throughout: the point model has its own tests, and
what needs proving here is that the grid lands where it says it does and that
a run on the wrong model path is visible instead of merely uniform.
"""

import math
import pathlib

import pytest

from property_scores.noise import surface as ns


def _stub(monkeypatch, value=60.0, path="transfer", fail_at=None):
    """Record every node the grid asks for and answer it."""
    calls = []

    def fake(lat, lng):
        calls.append((lat, lng))
        if fail_at is not None and len(calls) in fail_at:
            raise RuntimeError("node blew up")
        return {"lden_db": value, "score": 50, "lden_source": path}

    monkeypatch.setattr(ns, "noise_score", fake)
    return calls


def test_grid_is_square_and_centred_on_the_subject(monkeypatch):
    calls = _stub(monkeypatch)
    out = ns.noise_surface(-37.8, 144.96, radius_m=1500, cells=7)

    assert out["nrows"] == out["ncols"] == 7
    assert len(calls) == 49
    assert out["cells_missing"] == 0 and out["partial"] is False
    # The subject must be sampled exactly, not approached.
    assert (-37.8, 144.96) in [(round(a, 9), round(b, 9)) for a, b in calls]


def test_row_zero_is_north_and_column_zero_is_west(monkeypatch):
    """Documented orientation, and the one a customer paints by. Getting it
    upside down produces a picture that looks fine and is mirrored."""
    # Each node reports its OWN coordinate back, so the returned grid can be
    # read as a map of which cell was sampled where. Asserting only on the
    # bbox and the set of sampled coordinates does not do this: flipping the
    # rows leaves both identical and the picture silently mirrored.
    # Scaled offsets, not raw coordinates: the grid rounds its values to one
    # decimal, so returning `lat` verbatim collapses every row of a 3 km window
    # to the same -37.8 and the assertions below pass no matter how the rows
    # are ordered. (That is how the first version of this test missed a
    # deliberate north/south flip.)
    CENTRE_LAT, CENTRE_LNG = -37.8, 144.96

    def fake(lat, lng):
        return {"lden_db": (lat - CENTRE_LAT) * 100_000, "score": 50,
                "lden_source": "transfer"}

    monkeypatch.setattr(ns, "noise_score", fake)
    out = ns.noise_surface(-37.8, 144.96, radius_m=1500, cells=5)
    w, s, e, n = out["bbox"]
    assert n > s and e > w

    lat_grid = out["lden_db"]
    # Row 0 is the northern edge: its latitude is the largest, and latitude
    # decreases as the row index grows.
    row_lats = [CENTRE_LAT + row[0] / 100_000 for row in lat_grid]
    assert row_lats == sorted(row_lats, reverse=True)
    assert row_lats[0] == pytest.approx(n, abs=1e-5)
    assert row_lats[-1] == pytest.approx(s, abs=1e-5)
    # Every column in a row shares that row's latitude.
    for row in lat_grid:
        assert len(set(row)) == 1

    # Column 0 is the western edge: capture longitudes the same way.
    lngs_by_call = []

    def fake_lng(lat, lng):
        lngs_by_call.append((lat, lng))
        return {"lden_db": (lng - CENTRE_LNG) * 100_000, "score": 50,
                "lden_source": "transfer"}

    monkeypatch.setattr(ns, "noise_score", fake_lng)
    out2 = ns.noise_surface(-37.8, 144.96, radius_m=1500, cells=5)
    col_lngs = [CENTRE_LNG + v / 100_000 for v in out2["lden_db"][0]]
    assert col_lngs == sorted(col_lngs)
    assert col_lngs[0] == pytest.approx(w, abs=1e-5)
    assert col_lngs[-1] == pytest.approx(e, abs=1e-5)


def test_window_is_measured_per_axis_not_in_shared_degrees(monkeypatch):
    """A degree of longitude covers cos(lat) less ground. One shared scale
    factor makes the window an ellipse that under-covers the promised radius
    east-west, which is the bug the 2026-07-25 distance audit found elsewhere."""
    _stub(monkeypatch)
    lat = -37.8
    out = ns.noise_surface(lat, 144.96, radius_m=1500, cells=5)
    w, s, e, n = out["bbox"]

    half_ns_m = (n - s) / 2 * 111_320
    half_ew_m = (e - w) / 2 * 111_320 * math.cos(math.radians(lat))
    assert half_ns_m == pytest.approx(1500, rel=0.01)
    assert half_ew_m == pytest.approx(1500, rel=0.01)
    # And therefore the window is NOT square in degrees.
    assert (e - w) > (n - s)


@pytest.mark.parametrize("asked,used", [(5, 5), (7, 7), (9, 9), (4, 7), (100, 7)])
def test_cells_clamped_to_the_allowed_set(monkeypatch, asked, used):
    _stub(monkeypatch)
    out = ns.noise_surface(-37.8, 144.96, cells=asked)
    assert out["nrows"] == used


def test_radius_clamped(monkeypatch):
    _stub(monkeypatch)
    assert ns.noise_surface(-37.8, 144.96, radius_m=99)["radius_m"] == ns.RADIUS_MIN_M
    assert ns.noise_surface(-37.8, 144.96, radius_m=99999)["radius_m"] == ns.RADIUS_MAX_M


def test_cell_size_matches_the_spacing_actually_sampled(monkeypatch):
    _stub(monkeypatch)
    out = ns.noise_surface(-37.8, 144.96, radius_m=1500, cells=7)
    # 7 nodes across a +/-1500 m window is 3 steps from centre to edge.
    assert out["cell_size_m"] == 500
    assert ns.noise_surface(-37.8, 144.96, radius_m=1500, cells=9)["cell_size_m"] == 375


def test_failed_nodes_become_null_and_mark_the_grid_partial(monkeypatch):
    _stub(monkeypatch, fail_at={3, 4, 5})
    out = ns.noise_surface(-37.8, 144.96, cells=5)
    flat = [v for row in out["lden_db"] for v in row]
    assert flat.count(None) == 3
    assert out["cells_missing"] == 3
    assert out["partial"] is True


def test_no_nodes_at_all_returns_none(monkeypatch):
    monkeypatch.setattr(ns, "noise_score", lambda lat, lng: None)
    assert ns.noise_surface(-37.8, 144.96) is None


def test_caller_supplied_path_is_the_expectation(monkeypatch):
    _stub(monkeypatch, path="transfer")
    monkeypatch.setattr(ns, "transfer_inputs_ok", lambda: True)
    out = ns.noise_surface(-37.8, 144.96, cells=5, require_path="transfer")
    assert out["model_path"] == {"transfer": 25}
    assert out["model_path_expected"] == "transfer"
    assert out["model_path_expected_source"] == "caller"
    assert out["model_path_as_configured"] is True


def test_unconfigured_deployment_is_caught_because_the_caller_pins_the_path(monkeypatch):
    """★ The hole in the first version of this guard.

    It derived its expectation from this process's own NOISE_TRANSFER, so with
    the variable simply unset it expected physics, got physics, and returned a
    fully green grid: model_path_uniform true, model_path_as_configured true,
    every honesty field corroborating every other one and all of them wrong. A
    check that reads its own configuration to decide whether its configuration
    is right is not a check. The expectation has to come from outside.
    """
    _stub(monkeypatch, path="physics")
    monkeypatch.setattr(ns, "_ENV_PATH", "physics")     # NOISE_TRANSFER unset
    monkeypatch.setattr(ns, "transfer_inputs_ok", lambda: True)

    # Self-certifying: agrees with itself, and is wrong.
    loose = ns.noise_surface(-37.8, 144.96, cells=5)
    assert loose["model_path_uniform"] is True
    assert loose["model_path_as_configured"] is True

    # The caller states what production runs, and the same grid fails.
    pinned = ns.noise_surface(-37.8, 144.96, cells=5, require_path="transfer")
    assert pinned["model_path_uniform"] is True          # still looks clean
    assert pinned["model_path_as_configured"] is False   # and is caught
    assert pinned["model_path_expected_source"] == "caller"
    # transfer_inputs_ok separates "switched off" from "cannot read its data".
    assert pinned["transfer_inputs_ok"] is True


def test_expected_path_derivation_without_a_caller(monkeypatch):
    """Covers the fallback itself. Every other test pins require_path, which is
    precisely how the derivation escaped scrutiny the first time round."""
    monkeypatch.setattr(ns, "_ENV_PATH", "transfer")
    _stub(monkeypatch, path="physics")
    monkeypatch.setattr(ns, "transfer_inputs_ok", lambda: False)
    out = ns.noise_surface(-37.8, 144.96, cells=5)
    assert out["model_path_expected"] == "transfer"
    assert out["model_path_expected_source"] == "process_env"
    assert out["model_path_as_configured"] is False
    assert out["transfer_inputs_ok"] is False


def test_unknown_require_path_falls_back_rather_than_trusting_it(monkeypatch):
    monkeypatch.setattr(ns, "_ENV_PATH", "transfer")
    _stub(monkeypatch, path="transfer")
    monkeypatch.setattr(ns, "transfer_inputs_ok", lambda: True)
    out = ns.noise_surface(-37.8, 144.96, cells=5, require_path="wishful")
    assert out["model_path_expected"] == "transfer"
    assert out["model_path_expected_source"] == "process_env"


def test_transfer_inputs_probe_reads_files_not_the_environment(monkeypatch, tmp_path):
    """The 2026-08-05 failure was a readable env and an unreadable data dir, so
    a probe that consults NOISE_TRANSFER would have reported all clear."""
    from property_scores.noise import transfer

    # A data dir that DOES hold a road parquet, so each assertion below isolates
    # one failure mode. Pointing both cases at a missing directory (the first
    # version) made the model-load check untestable: the path check failed
    # first and the test passed with that check deleted.
    good = tmp_path / "data"
    good.mkdir()
    (good / "overture_roads.parquet").write_bytes(b"not empty")

    # Model loads, data dir unreachable -> the 2026-08-05 failure exactly.
    monkeypatch.setattr(transfer, "_load", lambda: True)
    monkeypatch.setattr(transfer, "_DATA_DIR", pathlib.Path("/nonexistent/data"))
    assert ns.transfer_inputs_ok() is False

    # Data dir fine, model will not load.
    monkeypatch.setattr(transfer, "_DATA_DIR", good)
    monkeypatch.setattr(transfer, "_load", lambda: False)
    assert ns.transfer_inputs_ok() is False

    # An empty parquet is not a usable input either.
    (good / "overture_roads.parquet").write_bytes(b"")
    monkeypatch.setattr(transfer, "_load", lambda: True)
    assert ns.transfer_inputs_ok() is False

    # Everything present -> True, so the probe is not just always False.
    (good / "overture_roads.parquet").write_bytes(b"not empty")
    assert ns.transfer_inputs_ok() is True


def test_partial_fallback_is_reported_without_condemning_the_grid(monkeypatch):
    """Nodes outside DEM or land-cover coverage legitimately fall back, so a
    mixed grid is a caveat with counts, not a failed run."""
    seq = iter(["transfer"] * 20 + ["physics"] * 5)

    def fake(lat, lng):
        return {"lden_db": 60.0, "score": 50, "lden_source": next(seq)}

    monkeypatch.setattr(ns, "noise_score", fake)
    monkeypatch.setattr(ns, "transfer_inputs_ok", lambda: True)
    out = ns.noise_surface(-37.8, 144.96, cells=5, require_path="transfer")
    assert out["model_path"] == {"transfer": 20, "physics": 5}
    assert out["model_path_uniform"] is False
    assert out["model_path_as_configured"] is False


def test_estimated_db_is_used_when_lden_db_is_absent(monkeypatch):
    def fake(lat, lng):
        return {"estimated_db": 58.2, "score": 55, "lden_source": "physics"}

    monkeypatch.setattr(ns, "noise_score", fake)
    out = ns.noise_surface(-37.8, 144.96, cells=5)
    assert out["lden_db"][0][0] == 58.2
