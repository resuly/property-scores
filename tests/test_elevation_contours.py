"""GA 5 m LiDAR contour engine: geometry, coverage honesty and interval rules.

All driven on small synthetic Int16-decimetre rasters (the same encoding as the
baked au_lidar_5m.vrt), because the real VRT only exists on the production
node. No test here may touch a live service or the real DEM.
"""

import math

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from property_scores.common import elevation_contours as ec

# ~5 m in degrees, the baked grid's real resolution.
PX = 0.0000449
NODATA = -32768
# Fixture window centre: inner Melbourne-ish, inside the real footprint's
# latitude band so the metre/degree maths is realistic.
LAT0, LNG0 = -37.80, 144.96


def _write(tmp_path, z_metres: np.ndarray, name="dem.tif") -> str:
    """Write a metres array as the baked encoding: Int16 decimetres, EPSG:4326,
    nodata -32768. NaN in the input becomes nodata."""
    data = np.where(np.isnan(z_metres), NODATA,
                    np.round(z_metres * 10)).astype("int16")
    nrows, ncols = data.shape
    west = LNG0 - ncols / 2 * PX
    north = LAT0 + nrows / 2 * PX
    path = str(tmp_path / name)
    with rasterio.open(
            path, "w", driver="GTiff", height=nrows, width=ncols, count=1,
            dtype="int16", crs="EPSG:4326", nodata=NODATA,
            transform=from_origin(west, north, PX, PX)) as dst:
        dst.write(data, 1)
    return path


def _hill(nrows=200, ncols=200, peak=100.0, slope_per_px=1.0) -> np.ndarray:
    """A radially symmetric cone: peak at the centre, falling slope_per_px
    metres per pixel, floored at 0. Known analytic contour radii."""
    r0, c0 = (nrows - 1) / 2, (ncols - 1) / 2
    rr, cc = np.mgrid[0:nrows, 0:ncols]
    dist = np.hypot(rr - r0, cc - c0)
    return np.maximum(peak - slope_per_px * dist, 0.0)


# The fixture window is ~200 px * 4.99e-3 deg across, radius must stay inside.
RADIUS = 400


def test_known_hill_produces_closed_rings_at_the_right_radii(tmp_path):
    path = _write(tmp_path, _hill())
    out = ec.contours(LAT0, LNG0, radius_m=RADIUS, interval_m=20, path=path)
    assert out is not None
    assert out["coverage"] == "full"
    assert out["interval_m"] == 20.0
    assert out["interval_source"] == "requested"
    # Cone from 0 to 100 m: closed rings at 20, 40, 60, 80 m; the 100 m level
    # is the single peak point and the 0 m level is the flat floor (no ring).
    assert set(out["levels"]) >= {20.0, 40.0, 60.0, 80.0}
    # Elevation values are METRES: the fixture stores decimetres, so a missed
    # /10 would report ~1000 here. (The peak node is ~0.7 px off the exact
    # cone apex, hence 99.3 rather than 100.0.)
    assert out["elevation_max_m"] == pytest.approx(100.0, abs=1.0)
    assert out["elevation_min_m"] == pytest.approx(0.0, abs=0.2)

    for want in (40.0, 80.0):
        rings = [f for f in out["features"]
                 if f["properties"]["elevation_m"] == want]
        assert rings, f"no ring at {want} m"
        ring = max(rings, key=lambda f: len(f["geometry"]["coordinates"]))
        coords = ring["geometry"]["coordinates"]
        # Closed: the ring ends where it starts.
        assert coords[0] == coords[-1]
        # Radius check: level L sits at dist = (100 - L) px from centre
        # (slope is 1 m per pixel).
        expect_px = 100.0 - want
        cx = sum(x for x, y in coords[:-1]) / (len(coords) - 1)
        cy = sum(y for x, y in coords[:-1]) / (len(coords) - 1)
        radii = [math.hypot((x - cx) / PX, (y - cy) / PX)
                 for x, y in coords[:-1]]
        assert min(radii) == pytest.approx(expect_px, rel=0.1)
        assert max(radii) == pytest.approx(expect_px, rel=0.1)


def test_all_nodata_window_reports_no_coverage(tmp_path):
    z = np.full((120, 120), np.nan)
    path = _write(tmp_path, z)
    assert ec.contours(LAT0, LNG0, radius_m=RADIUS, path=path) is None


def test_missing_raster_reports_no_coverage(tmp_path):
    assert ec.contours(LAT0, LNG0, radius_m=RADIUS,
                       path=str(tmp_path / "absent.vrt")) is None
    assert not ec.lidar_available(str(tmp_path / "absent.vrt"))


def test_partial_nodata_draws_no_line_into_the_gap(tmp_path):
    """East half of the window is outside the footprint. Lines must stop at
    the boundary, not be interpolated across it."""
    z = _hill()
    z[:, 100:] = np.nan
    path = _write(tmp_path, z)
    out = ec.contours(LAT0, LNG0, radius_m=RADIUS, interval_m=20, path=path)
    assert out is not None
    assert out["coverage"] == "partial"
    assert out["covered_fraction"] == pytest.approx(0.5, abs=0.05)
    assert out["features"], "covered half still has contours"
    # No vertex east of the last covered column. Column 99 is the last finite
    # node; cells using column 100 are skipped, so crossings can sit at most
    # on column 99. One pixel of slack for rounding.
    max_lng = LNG0 - 100 * PX + (99 + 0.5) * PX + PX
    for f in out["features"]:
        for x, y in f["geometry"]["coordinates"]:
            assert x <= max_lng, f"vertex at {x} crosses into nodata"


def test_interval_below_one_metre_is_raised_and_labelled(tmp_path):
    path = _write(tmp_path, _hill())
    out = ec.contours(LAT0, LNG0, radius_m=RADIUS, interval_m=0.5, path=path)
    assert out["interval_m"] == 1.0
    assert out["interval_source"] == "raised_to_floor"
    assert out["interval_floor_m"] == 1.0
    # And really drawn at 1 m: consecutive levels 1 m apart.
    lv = out["levels"]
    assert lv and all(b - a == pytest.approx(1.0) for a, b in zip(lv, lv[1:]))


def test_auto_interval_defaults_to_five_metres_on_gentle_relief(tmp_path):
    # 40 m of relief: relief/20 = 2 m, but the auto floor is the 5 m default.
    path = _write(tmp_path, _hill(peak=40.0, slope_per_px=0.16))
    out = ec.contours(LAT0, LNG0, radius_m=RADIUS, path=path)
    assert out["interval_m"] == 5.0
    assert out["interval_source"] == "auto"


def test_auto_interval_widens_over_steep_relief():
    # 700 m of relief (Katoomba-like): 700/20 = 35 -> next nice step 50.
    assert ec._pick_interval(700.0) == 50.0
    assert ec._pick_interval(0.0) == 5.0


def test_values_are_decimetres_divided_by_ten(tmp_path):
    """The baked grid stores Int16 decimetres. A flat 12.3 m plane must come
    back as 12.3, not 123."""
    z = np.full((120, 120), 12.3)
    path = _write(tmp_path, z)
    out = ec.contours(LAT0, LNG0, radius_m=RADIUS, path=path)
    assert out["elevation_min_m"] == pytest.approx(12.3, abs=0.01)
    assert out["elevation_max_m"] == pytest.approx(12.3, abs=0.01)
    # A flat plane has no contour lines, and that is reported, not invented.
    assert out["features"] == []
    assert out["levels"] == []


def test_endpoint_wires_404_and_503(tmp_path, monkeypatch):
    """The API face: outside-footprint is 404, missing VRT is 503."""
    from fastapi.testclient import TestClient
    from property_scores.api import main as api_main

    client = TestClient(api_main.app)

    # VRT missing entirely -> 503 outage.
    monkeypatch.setattr(ec, "LIDAR_VRT", str(tmp_path / "gone.vrt"))
    r = client.get("/scores/elevation/contours",
                   params={"lat": LAT0, "lng": LNG0})
    assert r.status_code == 503

    # VRT present, window all nodata -> 404 no coverage.
    path = _write(tmp_path, np.full((120, 120), np.nan))
    monkeypatch.setattr(ec, "LIDAR_VRT", path)
    r = client.get("/scores/elevation/contours",
                   params={"lat": LAT0, "lng": LNG0, "radius": RADIUS})
    assert r.status_code == 404

    # And a real hill -> 200 with features.
    path2 = _write(tmp_path, _hill(), name="hill.tif")
    monkeypatch.setattr(ec, "LIDAR_VRT", path2)
    r = client.get("/scores/elevation/contours",
                   params={"lat": LAT0, "lng": LNG0, "radius": RADIUS,
                           "interval_m": 20})
    assert r.status_code == 200
    body = r.json()
    assert body["feature_count"] > 0
    assert body["grid_m"] == 5


# ---------------------------------------------------------------------------
# review probes adopted 2026-08-15: saddle direction and absolute placement.
# Both survived every earlier test as mutations (branch-inverted saddles still
# produce simple non-crossing lines; a half-cell shift cancels out of
# centroid-based radius assertions), so each needs its own dedicated pin.
# ---------------------------------------------------------------------------

def _isolated_corner_values(features, level, z):
    """For a single-cell raster, which corner values do the contour segments
    at `level` isolate? Each segment's midpoint sits nearer the corner it cuts
    off; classify by nearest corner and return that corner's elevation."""
    nr, nc = z.shape
    west = LNG0 - nc / 2 * PX
    north = LAT0 + nr / 2 * PX
    corners = {(r, c): z[r, c] for r in (0, nr - 1) for c in (0, nc - 1)}
    out = []
    for f in features:
        xs = [p[0] for p in f["geometry"]["coordinates"]]
        ys = [p[1] for p in f["geometry"]["coordinates"]]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        best = min(corners, key=lambda rc: math.hypot(
            (west + (rc[1] + 0.5) * PX) - mx,
            (north - (rc[0] + 0.5) * PX) - my))
        out.append(corners[best])
    return sorted(out)


def test_saddle_cell_isolates_the_correct_corners(tmp_path):
    """Marching squares cases 5/10: the centre-mean rule decides which
    diagonal pair the two segments cut off. Inverting the branch isolates the
    wrong corners while still drawing simple, non-crossing lines, which is
    exactly why no crossing-based assertion can catch it."""
    z = np.array([[45.0, 55.0],
                  [55.0, 45.0]])
    path = _write(tmp_path, z)
    # Level 49: centre mean is 50 (above), so the two segments must isolate
    # the BELOW corners, the 45s.
    out = ec.contours(LAT0, LNG0, radius_m=20, interval_m=1, path=path)
    assert out is not None
    lines_49 = [f for f in out["features"]
                if abs(f["properties"]["elevation_m"] - 49.0) < 1e-9]
    assert len(lines_49) == 2, "a saddle cell must produce two segments"
    assert _isolated_corner_values(lines_49, 49.0, z) == [45.0, 45.0], (
        "saddle disambiguation isolated the wrong diagonal at a level below "
        "the centre mean")
    lines_51 = [f for f in out["features"]
                if abs(f["properties"]["elevation_m"] - 51.0) < 1e-9]
    assert len(lines_51) == 2
    assert _isolated_corner_values(lines_51, 51.0, z) == [55.0, 55.0], (
        "saddle disambiguation isolated the wrong diagonal at a level above "
        "the centre mean")


def test_contour_falls_at_the_absolute_slope_position(tmp_path):
    """An east-facing linear ramp has one analytic answer for where each
    contour lies. A half-cell indexing shift moves every vertex by ~2.5 m and
    cancels out of any centroid-radius assertion; only an absolute-position
    check sees it."""
    ncols = nrows = 41
    cc = np.mgrid[0:nrows, 0:ncols][1]
    z = cc.astype(float)  # 1 m per pixel, rising east
    path = _write(tmp_path, z)
    out = ec.contours(LAT0, LNG0, radius_m=80, interval_m=5, path=path)
    assert out is not None and out["features"], "ramp produced no contours"
    west = LNG0 - ncols / 2 * PX
    for f in out["features"]:
        lvl = f["properties"]["elevation_m"]
        # z equals the column index at node centres: elevation `lvl` sits at
        # column `lvl`, whose node longitude is west + (lvl + 0.5) * PX.
        want_lng = west + (lvl + 0.5) * PX
        for x, _y in f["geometry"]["coordinates"]:
            assert abs(x - want_lng) < PX * 0.05, (
                f"contour {lvl} m sits {abs(x - want_lng) / PX:.2f} px off "
                "its analytic position: half-cell offset")
