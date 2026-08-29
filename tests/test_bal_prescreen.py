"""Contract tests for the preliminary BAL computation data and data guards."""

from unittest import mock

from property_scores.bal_prescreen import tables
from property_scores.bal_prescreen.prescreen import (
    _grassland_excluded,
    _nearest_vegetation,
    bal_prescreen,
)


def test_all_australian_fdi_tables_are_exact_not_substituted():
    for fdi in (100, 80, 50, 40):
        table, used, substituted = tables.table_for_fdi(fdi)
        assert table is tables.TABLES[fdi]
        assert used == fdi
        assert substituted is False


def test_ga_shrubland_and_scrub_rows_are_not_transposed():
    flat = tables.TABLES[100]["flat"]
    assert flat["C"] == (7, 9, 13, 19, 100)   # Shrubland
    assert flat["D"] == (10, 13, 19, 27, 100)  # Scrub


def test_fdi_80_and_40_use_their_own_forest_thresholds():
    assert tables.TABLES[80]["flat"]["A"] == (16, 21, 31, 42, 100)
    assert tables.TABLES[40]["flat"]["A"] == (10, 13, 20, 28, 100)
    assert tables.lookup_bal(tables.TABLES[80], "flat", "A", 17) == "BAL-40"
    assert tables.lookup_bal(tables.TABLES[40], "flat", "A", 17) == "BAL-29"


def test_lookup_boundaries_and_100m_cutoff_are_explicit():
    table = tables.TABLES[100]
    assert tables.lookup_bal(table, "flat", "A", 18.99) == "BAL-FZ"
    assert tables.lookup_bal(table, "flat", "A", 19) == "BAL-40"
    assert tables.lookup_bal(table, "flat", "A", 48) == "BAL-12.5"
    assert tables.lookup_bal(table, "flat", "A", 99.99) == "BAL-12.5"
    assert tables.lookup_bal(table, "flat", "A", 100) == "BAL-LOW"


def test_grassland_is_assessed_inside_50m_outside_fdi_50():
    assert _grassland_excluded(100, 49.9) is False
    assert _grassland_excluded(100, 50) is True
    assert _grassland_excluded(80, 50) is True
    assert _grassland_excluded(40, 75) is True
    assert _grassland_excluded(50, 99.9) is False


def _grid(classes, *, south=-34.003, north=-34.0):
    nrows = len(classes)
    ncols = len(classes[0])
    return {
        "classes": classes,
        "nrows": nrows,
        "ncols": ncols,
        "bbox": [150.0, south, 150.003, north],
    }


def test_isolated_pixels_do_not_impersonate_one_hectare_patch():
    classes = [[50 for _ in range(30)] for _ in range(30)]
    # 100 tree pixels, but separated by two built-up pixels in both axes.
    for r in range(0, 30, 3):
        for c in range(0, 30, 3):
            classes[r][c] = 10
    with mock.patch("property_scores.bushfire.score.landcover_grid",
                    return_value=_grid(classes)):
        result = _nearest_vegetation(-34.0015, 150.0015)
    assert result["patch_pixels"][10] == 100
    assert result["distance_m"] is None


def test_connected_one_hectare_patch_qualifies():
    classes = [[50 for _ in range(30)] for _ in range(30)]
    for r in range(10):
        for c in range(10):
            classes[r][c] = 10
    with mock.patch("property_scores.bushfire.score.landcover_grid",
                    return_value=_grid(classes)):
        result = _nearest_vegetation(-34.0009, 150.0009)
    assert result["distance_m"] is not None
    assert result["wc_class"] == 10
    assert result["nearest_patch_pixels"] == 100


def test_one_hectare_threshold_uses_ground_area_not_fixed_pixel_count():
    classes = [[50 for _ in range(30)] for _ in range(30)]
    for r in range(10):
        for c in range(10):
            classes[r][c] = 10
    # At Tasmania's latitude these nominal 10 m lon/lat cells are under
    # 100 m2, so a connected 100-pixel block is still less than one hectare.
    with mock.patch("property_scores.bushfire.score.landcover_grid",
                    return_value=_grid(classes, south=-43.003, north=-43.0)):
        result = _nearest_vegetation(-43.0009, 150.0009)
    assert result["min_patch_pixels"] > 100
    assert result["distance_m"] is None


def test_method_source_is_pinned_not_a_floating_upstream():
    assert len(tables.GA_BAL_TOOLBOX_COMMIT) == 40
    assert tables.GA_BAL_TOOLBOX_COMMIT in tables.GA_BAL_TOOLBOX_URL


def test_shrub_screen_uses_heavier_scrub_as_point_not_lighter_bound():
    vegetation = {
        "distance_m": 8.0, "wc_class": 20, "in_vegetation": False,
        "patch_pixels": {10: 0, 20: 130, 30: 0},
        "nearest_patch_pixels": 130, "min_patch_pixels": 120,
        "pixel_area_m2": 84.0, "veg_lat": None, "veg_lng": None,
    }
    with mock.patch(
            "property_scores.bal_prescreen.prescreen._nearest_vegetation",
            return_value=vegetation):
        result = bal_prescreen(
            -37.8, 145.0, state="VIC", elevation=100, slope_deg=0,
            overlay=(None, [], None, True, "state_service"),
        )

    assert result["indicative_bal"] == "BAL-FZ"  # D Scrub at 8 m
    assert result["bal_range"] == ["BAL-40", "BAL-FZ"]
    assert result["inputs"]["vegetation"]["as3959_class"].startswith("D")
    assert "lighter plausible = C" in \
        result["inputs"]["vegetation"]["formation_uncertainty"]
