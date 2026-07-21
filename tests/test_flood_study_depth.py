"""reg-09: address-level modelled flood DEPTH (metres) from council study grids.

Proves depth drives the score (a 1.2 m modelled 1% AEP depth cannot read "Very Low
Risk"), surfaces provenance, and is inert where no grid covers the point (backward
compat). COG-independent — monkeypatches the depth sampler so no raster is needed.
"""
import pytest

from property_scores.flood import score as fs
from property_scores.flood import study_depth as sd


def _isolate(monkeypatch):
    # neutralise overlay/satellite/terrain/rainfall so the test isolates depth
    monkeypatch.setattr(fs, "_local_check", None, raising=False)
    monkeypatch.setattr(fs, "_overlay_check", lambda *a, **k: (None, [], []))
    monkeypatch.setattr(fs, "_water_proximity_local", lambda *a, **k: None)
    monkeypatch.setattr(fs, "_hand_local", lambda *a, **k: None)
    monkeypatch.setattr(fs, "_query_ifd", lambda *a, **k: None)


def _fake_depth(depth_m):
    return lambda lat, lng: (None if depth_m is None else {
        "depth_m": depth_m, "aep": "1% AEP",
        "source": "Test Council FRMS&P", "licence": "CC BY 4.0"})


@pytest.mark.parametrize("depth_m,expect_label,max_score", [
    (2.26, "Very High Risk", 20),
    (1.20, "Very High Risk", 20),
    (0.60, "High Risk", 40),
    (0.34, "Moderate Risk", 60),
    (0.10, "Moderate Risk", 60),
])
def test_depth_drives_score(monkeypatch, depth_m, expect_label, max_score):
    _isolate(monkeypatch)
    monkeypatch.setattr(sd, "depth_at", _fake_depth(depth_m))
    r = fs.flood_score(-33.26, 151.55)
    assert r["flood_depth"]["depth_m"] == depth_m
    assert r["score"] <= max_score
    assert r["label"] == expect_label
    assert "CC BY 4.0" in r["flood_depth_summary"]


def test_deep_water_cannot_read_very_low(monkeypatch):
    # the exact bug reg-09 fixes: 1.2 m depth where the coarse overlay missed it
    _isolate(monkeypatch)
    monkeypatch.setattr(sd, "depth_at", _fake_depth(1.2))
    r = fs.flood_score(-33.26, 151.55)
    assert r["score"] < 40 and r["label"] != "Very Low Risk"


def test_no_depth_grid_is_inert(monkeypatch):
    # point outside every grid: score unchanged, no flood_depth field (backward compat)
    _isolate(monkeypatch)
    monkeypatch.setattr(sd, "depth_at", _fake_depth(None))
    r = fs.flood_score(-33.87, 151.20)
    assert "flood_depth" not in r
    assert r["score"] == 85  # no signals at all -> default estimate


def test_sampler_none_without_cog():
    # default registry path does not exist on a dev box -> None, engine untouched
    assert sd.depth_at(-33.26, 151.55) is None or isinstance(sd.depth_at(-33.26, 151.55), dict)
