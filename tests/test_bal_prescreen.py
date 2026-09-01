"""Contract tests for the preliminary BAL computation data and data guards."""

from unittest import mock

from property_scores.bal_prescreen import tables
from property_scores.bal_prescreen.prescreen import (
    _bal_rank,
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


def test_worst_case_far_forest_outranks_near_grassland():
    # Site pixel is (15, 15); grassland patch 2 cols east (about 18 m),
    # forest patch 3 cols west (about 28 m). Under FDI 100 flat, grass at
    # 18 m is BAL-19 while forest at 28 m is BAL-29: the screen must keep
    # the worst outcome across classes, not the nearest patch.
    classes = [[50 for _ in range(30)] for _ in range(30)]
    for r in range(11, 21):
        for c in range(17, 27):
            classes[r][c] = 30  # grassland, 100 px >= 1 ha
        for c in range(3, 13):
            classes[r][c] = 10  # forest, 100 px >= 1 ha
    with mock.patch("property_scores.bushfire.score.landcover_grid",
                    return_value=_grid(classes)), \
         mock.patch("property_scores.common.terrain.elevation",
                    return_value=None):
        result = bal_prescreen(
            -34.00155, 150.00155, state="VIC", elevation=100, slope_deg=0,
            overlay=(None, [], None, True, "state_service"),
        )

    assert result["indicative_bal"] == "BAL-29"
    veg = result["inputs"]["vegetation"]
    assert veg["as3959_class"].startswith("A")
    assert 25 < veg["distance_m"] < 35
    assessed = veg["assessed_classes"]
    assert {a["as3959_class"] for a in assessed} == {"A", "G"}
    grass = next(a for a in assessed if a["as3959_class"] == "G")
    assert grass["distance_m"] < veg["distance_m"]
    assert grass["bal"] == "BAL-19"


def test_adjacent_tree_and_shrub_form_one_qualifying_patch():
    # Tree 60 px and shrub 60 px, each under the 1 ha pixel threshold on its
    # own, but adjacent: together they are one 120 px fuel patch and qualify.
    classes = [[50 for _ in range(30)] for _ in range(30)]
    for r in range(11, 21):
        for c in range(17, 23):
            classes[r][c] = 10  # tree, 60 px
        for c in range(23, 29):
            classes[r][c] = 20  # shrub, 60 px
    with mock.patch("property_scores.bushfire.score.landcover_grid",
                    return_value=_grid(classes)):
        result = _nearest_vegetation(-34.00155, 150.00155)
    assert result["min_patch_pixels"] > 60  # each type alone is below 1 ha
    assert result["distance_m"] is not None
    assert result["wc_class"] == 10
    assert result["nearest_patch_pixels"] == 120
    # both classes qualify and each carries its own nearest distance
    dists = {}
    for pix in result["pixels"]:
        dists.setdefault(pix["wc_class"], []).append(pix["distance_m"])
    assert set(dists) == {10, 20}
    assert min(dists[10]) < min(dists[20])


def test_window_edge_truncated_patch_uses_visible_connected_area():
    # A mixed tree+shrub strip runs along the window's top edge (rows 0..3),
    # truncated by the analysis window. Its visible connected area (104 px)
    # still exceeds 1 ha, so it qualifies even though each class alone (52 px)
    # does not. Qualification is based on the visible portion only: when the
    # window cuts the strip down to 2 rows (52 px combined), it is excluded.
    site = (-34.00045, 150.00155)  # pixel (4, 15) centre, near the top edge

    classes = [[50 for _ in range(30)] for _ in range(30)]
    for r in range(0, 4):
        for c in range(3, 16):
            classes[r][c] = 10  # tree, 52 px
        for c in range(16, 29):
            classes[r][c] = 20  # shrub, 52 px
    with mock.patch("property_scores.bushfire.score.landcover_grid",
                    return_value=_grid(classes)):
        result = _nearest_vegetation(*site)
    assert result["min_patch_pixels"] > 52
    assert result["distance_m"] is not None
    assert result["wc_class"] == 10  # tree pixel one row north of the site
    assert result["nearest_patch_pixels"] == 104
    assert {pix["wc_class"] for pix in result["pixels"]} == {10, 20}

    truncated = [[50 for _ in range(30)] for _ in range(30)]
    for r in range(0, 2):
        for c in range(3, 16):
            truncated[r][c] = 10
        for c in range(16, 29):
            truncated[r][c] = 20
    with mock.patch("property_scores.bushfire.score.landcover_grid",
                    return_value=_grid(truncated)):
        result = _nearest_vegetation(*site)
    assert result["distance_m"] is None


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


def test_site_inside_small_unqualified_clump_keeps_far_patch_distance():
    # The site sits inside a 4-pixel tree clump (far below 1 ha), with a
    # qualifying 100-pixel tree patch about 46 m east. The clump must NOT
    # zero the effective distance: only a site inside a QUALIFYING patch is
    # at distance 0. The screen keeps the distance to the far patch.
    classes = [[50 for _ in range(30)] for _ in range(30)]
    for r in range(15, 17):
        for c in range(15, 17):
            classes[r][c] = 10  # clump containing the site pixel (15, 15)
    for r in range(11, 21):
        for c in range(20, 30):
            classes[r][c] = 10  # qualifying patch, 100 px, detached
    with mock.patch("property_scores.bushfire.score.landcover_grid",
                    return_value=_grid(classes)):
        result = _nearest_vegetation(-34.00155, 150.00155)
    assert result["in_vegetation"] is True
    assert result["wc_class"] == 10
    assert 40 < result["distance_m"] < 50  # far patch, not 0
    assert all(40 < pix["distance_m"] < 100 for pix in result["pixels"])


def test_pure_grassland_beyond_50m_screens_bal_low_end_to_end():
    # Only grassland in the window, nearest pixel about 55 m away, FDI 100:
    # the GA rule excludes grassland from 50 m outside FDI 50, so the whole
    # screen must come out BAL-LOW with the exclusion spelled out. This wires
    # _grassland_excluded through the candidate loop, not just the helper.
    classes = [[50 for _ in range(30)] for _ in range(30)]
    for r in range(11, 22):
        for c in range(21, 30):
            classes[r][c] = 30  # grassland, 99 px >= 1 ha
    with mock.patch("property_scores.bushfire.score.landcover_grid",
                    return_value=_grid(classes)), \
         mock.patch("property_scores.common.terrain.elevation",
                    return_value=None):
        result = bal_prescreen(
            -34.00155, 150.00155, state="VIC", elevation=100, slope_deg=0,
            overlay=(None, [], None, True, "state_service"),
        )
    assert result["inputs"]["vegetation"]["distance_m"] >= 50
    assert result["indicative_bal"] == "BAL-LOW"
    assert result["bal_range"] == ["BAL-LOW", "BAL-LOW"]
    assert any("excludes grassland from 50 m" in a for a in result["assumptions"])


def test_confidence_range_upper_bound_uses_one_steeper_slope():
    # Forest at 27 m, measured flat slope, FDI 100: point is BAL-29 (A flat),
    # but the harsh end of the confidence band assumes one-steeper slope (d5),
    # where 27 m is BAL-40. The range upper bound must be the steeper outcome.
    vegetation = {
        "distance_m": 27.0, "wc_class": 10, "in_vegetation": False,
        "patch_pixels": {10: 130, 20: 0, 30: 0},
        "nearest_patch_pixels": 130, "min_patch_pixels": 98,
        "pixel_area_m2": 102.7, "veg_lat": None, "veg_lng": None,
        "pixels": [{"wc_class": 10, "distance_m": 27.0,
                    "veg_lat": None, "veg_lng": None}],
    }
    with mock.patch(
            "property_scores.bal_prescreen.prescreen._nearest_vegetation",
            return_value=vegetation):
        result = bal_prescreen(
            -37.8, 145.0, state="VIC", elevation=100, slope_deg=0,
            overlay=(None, [], None, True, "state_service"),
        )
    assert result["indicative_bal"] == "BAL-29"
    assert result["bal_range"] == ["BAL-19", "BAL-40"]


def test_diagonal_adjacency_joins_patch_areas():
    # Two 52-pixel tree blocks touch only at a corner: 8-connectivity makes
    # them one 104-pixel patch (>= 1 ha), so vegetation 9 m east qualifies.
    classes = [[50 for _ in range(30)] for _ in range(30)]
    for r in range(11, 15):
        for c in range(3, 16):
            classes[r][c] = 10  # 52 px, corner at (14, 15)
    for r in range(15, 19):
        for c in range(16, 29):
            classes[r][c] = 10  # 52 px, corner at (15, 16)
    with mock.patch("property_scores.bushfire.score.landcover_grid",
                    return_value=_grid(classes)):
        result = _nearest_vegetation(-34.00155, 150.00155)
    assert result["min_patch_pixels"] > 52
    assert result["distance_m"] is not None
    assert result["nearest_patch_pixels"] == 104


def test_one_hectare_pixel_threshold_rounds_up():
    # At this latitude a pixel is about 102.7 m2, so 1 ha needs ceil(97.35)
    # = 98 pixels. A 97-pixel patch must not qualify; adding one pixel must.
    def _block(missing):
        classes = [[50 for _ in range(30)] for _ in range(30)]
        for r in range(11, 21):
            for c in range(17, 27):
                classes[r][c] = 10
        for r, c in missing:
            classes[r][c] = 50
        return classes

    with mock.patch("property_scores.bushfire.score.landcover_grid",
                    return_value=_grid(_block([(11, 24), (11, 25), (11, 26)]))):
        result = _nearest_vegetation(-34.00155, 150.00155)
    assert result["min_patch_pixels"] == 98
    assert result["distance_m"] is None  # 97 px is under one hectare

    with mock.patch("property_scores.bushfire.score.landcover_grid",
                    return_value=_grid(_block([(11, 25), (11, 26)]))):
        result = _nearest_vegetation(-34.00155, 150.00155)
    assert result["distance_m"] is not None  # 98 px reaches one hectare


def test_equal_bal_tie_prefers_closer_vegetation():
    # Grassland at 15 m and forest at 45 m both screen BAL-19 with identical
    # confidence-band tops; the reported basis must be the CLOSER vegetation.
    vegetation = {
        "distance_m": 15.0, "wc_class": 30, "in_vegetation": False,
        "patch_pixels": {10: 130, 20: 0, 30: 130},
        "nearest_patch_pixels": 130, "min_patch_pixels": 98,
        "pixel_area_m2": 102.7, "veg_lat": None, "veg_lng": None,
        "pixels": [
            {"wc_class": 30, "distance_m": 15.0, "veg_lat": None, "veg_lng": None},
            {"wc_class": 10, "distance_m": 45.0, "veg_lat": None, "veg_lng": None},
        ],
    }
    with mock.patch(
            "property_scores.bal_prescreen.prescreen._nearest_vegetation",
            return_value=vegetation):
        result = bal_prescreen(
            -37.8, 145.0, state="VIC", elevation=100, slope_deg=0,
            overlay=(None, [], None, True, "state_service"),
        )
    assert result["indicative_bal"] == "BAL-19"
    veg = result["inputs"]["vegetation"]
    assert veg["as3959_class"].startswith("G")
    assert veg["distance_m"] == 15.0


def test_same_class_downslope_behind_is_not_shadowed_by_nearer_upslope():
    # Forest on BOTH sides of the site: north patch nearer (about 45 m) but
    # UPSLOPE (flat band, BAL-19), south patch farther (about 56 m) but
    # DOWNSLOPE at 15 deg (d15 band, BAL-29). Per-class nearest-pixel logic
    # kept only the north pixel and reported BAL-19; every pixel must be
    # assessed with its own direction so the south side drives the result.
    site_lat, site_lng = -34.00155, 150.00155  # pixel (15, 15) centre
    classes = [[50 for _ in range(30)] for _ in range(30)]
    for r in range(3, 12):
        for c in range(11, 22):
            classes[r][c] = 10  # north forest, 99 px, nearest about 45 m
    for r in range(20, 29):
        for c in range(11, 22):
            classes[r][c] = 10  # south forest, 99 px, nearest about 56 m

    def _elev(la, ln):
        if abs(la - site_lat) < 1e-9:
            return 100.0  # site
        return 120.0 if la > site_lat else 80.0  # north above, south below

    with mock.patch("property_scores.bushfire.score.landcover_grid",
                    return_value=_grid(classes)), \
         mock.patch("property_scores.common.terrain.elevation",
                    side_effect=_elev):
        result = bal_prescreen(
            site_lat, site_lng, state="VIC", elevation=100, slope_deg=15,
            overlay=(None, [], None, True, "state_service"),
        )

    assert result["indicative_bal"] == "BAL-29"
    slope = result["inputs"]["slope"]
    assert slope["direction"] == "downslope"
    assert slope["band"] == "d15"
    assert 50 < result["inputs"]["vegetation"]["distance_m"] < 60  # south pixel
    lo, hi = result["bal_range"]
    assert _bal_rank(lo) <= _bal_rank("BAL-29") <= _bal_rank(hi)
    # the per-class summary must also carry the class's WORST pixel, not its
    # nearest: forest is reported from the south downslope pixel
    assessed = result["inputs"]["vegetation"]["assessed_classes"]
    forest = next(a for a in assessed if a["as3959_class"] == "A")
    assert forest["bal"] == "BAL-29"
    assert 50 < forest["distance_m"] < 60


def test_range_upper_bound_takes_max_across_all_candidates():
    # Shrub 12 m upslope: point BAL-40, own harsh end BAL-40 (the worst
    # candidate). Forest 49.5 m downslope 15 deg: point BAL-29 but its harsh
    # end (d20) is BAL-FZ. The range upper bound must be the maximum harsh
    # end across ALL candidates, not just the worst candidate's own.
    site_lat = -37.8
    vegetation = {
        "distance_m": 12.0, "wc_class": 20, "in_vegetation": False,
        "patch_pixels": {10: 130, 20: 130, 30: 0},
        "nearest_patch_pixels": 130, "min_patch_pixels": 98,
        "pixel_area_m2": 102.7, "veg_lat": -37.79, "veg_lng": 145.0,
        "pixels": [
            {"wc_class": 20, "distance_m": 12.0,
             "veg_lat": -37.79, "veg_lng": 145.0},   # north, upslope
            {"wc_class": 10, "distance_m": 49.5,
             "veg_lat": -37.81, "veg_lng": 145.0},   # south, downslope
        ],
    }

    def _elev(la, ln):
        if abs(la - site_lat) < 1e-9:
            return 100.0
        return 120.0 if la > site_lat else 80.0

    with mock.patch(
            "property_scores.bal_prescreen.prescreen._nearest_vegetation",
            return_value=vegetation), \
         mock.patch("property_scores.common.terrain.elevation",
                    side_effect=_elev):
        result = bal_prescreen(
            site_lat, 145.0, state="VIC", elevation=100, slope_deg=15,
            overlay=(None, [], None, True, "state_service"),
        )

    assert result["indicative_bal"] == "BAL-40"  # shrub is still the point
    assert result["bal_range"] == ["BAL-29", "BAL-FZ"]
