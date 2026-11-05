"""Raster coverage rule of the transfer model, on real production bytes.

The fixture is a small crop of the production DEM and ESA WorldCover mosaics
(tests/fixtures/noise_rasters_perth_20261106/README.md). The point inside it
must reproduce what the production code computed from the full VRTs, and a
point outside it (Geraldton, beyond the mosaic) must be reported as outside
coverage: that is the case the noise surface now treats as a coverage gap
instead of a deployment incident.
"""

import json
import pathlib

import pytest

from property_scores.noise import surface as ns
from property_scores.noise import transfer

FX = pathlib.Path(__file__).parent / "fixtures" / "noise_rasters_perth_20261106"
EXPECTED = json.loads((FX / "expected_raster_feats.json").read_text())
INSIDE = (EXPECTED["lat"], EXPECTED["lng"])
GERALDTON = (-28.77768337, 114.60542876)


@pytest.fixture
def perth_rasters(monkeypatch):
    monkeypatch.setattr(transfer, "DEM", str(FX / "perth_dem.tif"))
    monkeypatch.setattr(transfer, "LC", str(FX / "perth_lc.tif"))


def test_crop_reproduces_production_features(perth_rasters):
    feats, ok = transfer.raster_feats(*INSIDE)
    assert ok is True
    for key, want in EXPECTED["features"].items():
        assert feats[key] == pytest.approx(want, abs=1e-6), key


def test_point_outside_the_mosaic_is_not_covered(perth_rasters):
    feats, ok = transfer.raster_feats(*GERALDTON)
    assert ok is False
    assert transfer.raster_coverage_ok(*GERALDTON) is False
    assert transfer.raster_coverage_ok(*INSIDE) is True


def test_missing_land_cover_alone_is_a_coverage_gap(perth_rasters, monkeypatch):
    """Geraldton on the server: DEM answers (0.0, not NaN), land cover does
    not. The land-cover miss on its own must be enough."""
    monkeypatch.setattr(transfer, "LC", str(FX / "does_not_exist.tif"))
    assert transfer.raster_coverage_ok(*INSIDE) is False


def test_transfer_feats_uses_the_same_rule(perth_rasters, monkeypatch):
    """transfer_feats and the coverage probe must not drift apart."""
    class _DB:
        def execute(self, sql):
            return self

        def fetchall(self):
            return []

    for point in (INSIDE, GERALDTON):
        feats, ok = transfer.transfer_feats(_DB(), *point)
        assert ok is transfer.raster_coverage_ok(*point)
    feats, _ = transfer.transfer_feats(_DB(), *INSIDE)
    for key, want in EXPECTED["features"].items():
        assert feats[key] == pytest.approx(want, abs=1e-6), key


def test_surface_probe_reads_the_rasters(perth_rasters):
    assert ns._coverage_gap(*GERALDTON) is True
    assert ns._coverage_gap(*INSIDE) is False
