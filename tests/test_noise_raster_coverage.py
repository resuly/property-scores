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


def test_unreadable_land_cover_is_an_incident_not_a_gap(perth_rasters, monkeypatch):
    """raster_sample answers {} for a file it cannot open, exactly as for a
    point outside the mosaic. A lost lc.vrt must not turn every node into a
    coverage gap: the probe refuses, the surface counts it as unexplained, and
    the input preflight fails."""
    monkeypatch.setattr(transfer, "LC", str(FX / "does_not_exist.tif"))
    with pytest.raises(OSError):
        transfer.raster_coverage_ok(*INSIDE)
    assert ns._coverage_gap(*GERALDTON) is False
    assert transfer.rasters_readable() is False


def test_unreadable_dem_is_an_incident_not_a_gap(perth_rasters, monkeypatch):
    monkeypatch.setattr(transfer, "DEM", str(FX / "does_not_exist.tif"))
    with pytest.raises(OSError):
        transfer.raster_coverage_ok(*GERALDTON)
    assert ns._coverage_gap(*GERALDTON) is False
    assert transfer.rasters_readable() is False


def test_vrt_with_a_missing_source_tile_is_not_readable(perth_rasters, monkeypatch, tmp_path):
    """The VRT opens but a tile it names is gone (a partial data sync)."""
    import shutil
    tile = tmp_path / "lc_tile.tif"
    shutil.copy(FX / "perth_lc.tif", tile)
    import rasterio
    with rasterio.open(tile) as src:
        w, h, t = src.width, src.height, src.transform
    vrt = tmp_path / "lc.vrt"
    vrt.write_text(f"""<VRTDataset rasterXSize="{w}" rasterYSize="{h}">
  <GeoTransform>{t.c}, {t.a}, 0, {t.f}, 0, {t.e}</GeoTransform>
  <VRTRasterBand dataType="Byte" band="1">
    <SimpleSource>
      <SourceFilename relativeToVRT="1">lc_tile.tif</SourceFilename>
      <SourceBand>1</SourceBand>
    </SimpleSource>
  </VRTRasterBand>
</VRTDataset>
""")
    monkeypatch.setattr(transfer, "LC", str(vrt))
    assert transfer.rasters_readable() is True
    tile.unlink()
    assert transfer.rasters_readable() is False


def test_readable_rasters_pass_the_preflight(perth_rasters):
    assert transfer.rasters_readable() is True


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
