"""Engineering contracts for the internal preliminary BAL v2 shadow."""

from unittest import mock

from property_scores.bal_prescreen.v2 import (
    building_point_from_professional_report,
    effective_slope_under_vegetation,
    preliminary_bal_v2,
    scan_vegetation_observations,
)


def _grid(classes, *, west=150.0, south=-34.004, east=150.004, north=-34.0):
    return {
        "classes": classes,
        "nrows": len(classes), "ncols": len(classes[0]),
        "bbox": [west, south, east, north],
    }


def _identity(lat=-34.0, lng=150.0):
    return building_point_from_professional_report(
        lat, lng, report_url="https://example.test/professional-report.pdf",
        coordinate_evidence="coordinates printed in report")


def test_v2_refuses_unverified_or_non_building_points():
    missing = preliminary_bal_v2(-34.002, 150.002, subject_identity=None,
                                 state="NSW")
    assert missing["status"] == "identity_required"
    assert missing["preliminary_bal"] is None

    parcel = preliminary_bal_v2(
        -34.002, 150.002,
        subject_identity={"kind": "parcel_point", "verified": True,
                          "source": "cadastre"}, state="NSW")
    assert parcel["status"] == "identity_required"

    mismatch = preliminary_bal_v2(
        -34.002, 150.002,
        subject_identity=_identity(-34.0, 150.0), state="NSW")
    assert mismatch["status"] == "identity_required"
    assert "does not match" in mismatch["reason"]

    nan_coordinate = preliminary_bal_v2(
        float("nan"), 150.0, subject_identity=_identity(), state="NSW")
    assert nan_coordinate["status"] == "identity_required"
    assert nan_coordinate["preliminary_bal"] is None


def test_mixed_tree_and_shrub_jointly_qualify_one_hectare():
    classes = [[50 for _ in range(40)] for _ in range(40)]
    # Adjacent 6x10 tree + 6x10 shrub. Neither class is 1 ha alone, but the
    # connected combustible patch is roughly 1.2 ha at this grid size.
    for row in range(12, 18):
        for col in range(10, 20):
            classes[row][col] = 10
        for col in range(20, 30):
            classes[row][col] = 20
    scan = scan_vegetation_observations(-34.002, 150.002,
                                       grid=_grid(classes))
    assert scan["status"] == "ok"
    assert {item["wc_class"] for item in scan["observations"]} == {10, 20}
    assert {item["component_id"] for item in scan["observations"]} == {1}
    assert all(all(pixel["wc_class"] == item["wc_class"]
                   for pixel in item["_component_pixels"])
               for item in scan["observations"])
    assert scan["components"][0]["qualification"] == "observed_ge_1ha"


def test_one_component_can_produce_observations_in_multiple_directions():
    classes = [[50 for _ in range(40)] for _ in range(40)]
    # One U-shaped tree component wraps around the subject on both sides.
    for col in range(10, 30):
        classes[5][col] = 10
    for row in range(5, 23):
        for col in (10, 29):
            classes[row][col] = 10
    # Thicken enough to exceed one hectare while preserving the U shape.
    for row in range(6, 10):
        for col in range(10, 30):
            classes[row][col] = 10
    scan = scan_vegetation_observations(-34.002, 150.002,
                                       grid=_grid(classes))
    sectors = {item["sector"] for item in scan["observations"]
               if item["wc_class"] == 10}
    assert "E" in sectors and "W" in sectors


def test_edge_component_is_retained_but_marked_truncated():
    classes = [[50 for _ in range(40)] for _ in range(40)]
    for row in range(15, 20):
        for col in range(0, 5):
            classes[row][col] = 10
    scan = scan_vegetation_observations(-34.002, 150.0008,
                                       grid=_grid(classes))
    assert scan["observations"]
    assert scan["observations"][0]["component_edge_truncated"] is True
    assert scan["components"][0]["qualification"] == "edge_continuation_assumed"


def test_effective_slope_uses_two_points_inside_vegetation():
    observation = {
        "distance_m": 20.0, "bearing_deg": 90.0,
        "veg_lat": -34.0, "veg_lng": 150.0002,
        "_near_cell": (0, 0),
        "_component_pixels": [
            {"distance_m": 20.0, "bearing_deg": 90.0,
             "lat": -34.0, "lng": 150.0002, "row": 0, "col": 0,
             "wc_class": 10},
            {"distance_m": 30.0, "bearing_deg": 90.0,
             "lat": -34.0, "lng": 150.0003, "row": 0, "col": 1,
             "wc_class": 10},
            {"distance_m": 40.0, "bearing_deg": 90.0,
             "lat": -34.0, "lng": 150.0004, "row": 0, "col": 2,
             "wc_class": 10},
            {"distance_m": 50.0, "bearing_deg": 90.0,
             "lat": -34.0, "lng": 150.0005, "row": 0, "col": 3,
             "wc_class": 10},
        ],
    }

    def elevation(_lat, lng):
        return 100.0 if lng < 150.0003 else 95.0

    slope = effective_slope_under_vegetation(
        -34.0, 150.0, observation, elevation_fn=elevation)
    assert slope["status"] == "ok"
    assert slope["direction"] == "downslope"
    assert slope["run_m"] >= 18
    assert slope["band"] in {"d5", "d10", "d15", "d20"}


def test_effective_slope_rejects_a_profile_crossing_a_class_gap():
    observation = {
        "distance_m": 20.0, "bearing_deg": 90.0,
        "veg_lat": -34.0, "veg_lng": 150.0001, "_near_cell": (0, 0),
        "_component_pixels": [
            {"distance_m": 20.0, "bearing_deg": 90.0,
             "lat": -34.0, "lng": 150.0001, "row": 0, "col": 0},
            {"distance_m": 40.0, "bearing_deg": 90.0,
             "lat": -34.0, "lng": 150.0004, "row": 0, "col": 2},
        ],
    }
    slope = effective_slope_under_vegetation(
        -34.0, 150.0, observation, elevation_fn=lambda *_: 100.0)
    assert slope["status"] == "unavailable"
    assert slope["reason"] == "insufficient vegetation run"


def test_effective_slope_uses_steepest_continuous_candidate():
    observation = {
        "distance_m": 20.0, "bearing_deg": 90.0,
        "veg_lat": -34.0, "veg_lng": 150.0001, "_near_cell": (0, 0),
        "_component_pixels": [
            {"distance_m": 20.0, "bearing_deg": 90.0,
             "lat": -34.0, "lng": 150.0001, "row": 0, "col": 0},
            {"distance_m": 30.0, "bearing_deg": 90.0,
             "lat": -34.0, "lng": 150.00022, "row": 0, "col": 1},
            {"distance_m": 30.1, "bearing_deg": 92.0,
             "lat": -34.00002, "lng": 150.00022, "row": 1, "col": 1},
        ],
    }

    def elevation(lat, _lng):
        return 90.0 if lat < -34.00001 else 100.0

    slope = effective_slope_under_vegetation(
        -34.0, 150.0, observation, elevation_fn=elevation)
    assert slope["status"] == "method1_inapplicable"
    assert slope["profile_points_sampled"] == 2


def test_effective_slope_rejects_nonfinite_terrain():
    observation = {
        "distance_m": 20.0, "bearing_deg": 90.0,
        "veg_lat": -34.0, "veg_lng": 150.0002, "_near_cell": (0, 0),
        "_component_pixels": [
            {"distance_m": 20.0, "bearing_deg": 90.0,
             "lat": -34.0, "lng": 150.0002, "row": 0, "col": 0},
            {"distance_m": 40.0, "bearing_deg": 90.0,
             "lat": -34.0, "lng": 150.0004, "row": 0, "col": 1},
        ],
    }
    slope = effective_slope_under_vegetation(
        -34.0, 150.0, observation, elevation_fn=lambda *_: float("nan"))
    assert slope["status"] == "unavailable"
    assert slope["reason"] == "terrain elevation unavailable"


def test_v2_fails_closed_when_method1_slope_is_unavailable():
    observation = {
        "component_id": 1, "wc_class": 10, "distance_m": 20.0,
        "bearing_deg": 90.0, "sector": "E", "veg_lat": -34.0,
        "veg_lng": 150.0002, "component_area_m2": 12_000,
        "component_edge_truncated": False, "_near_cell": (0, 0),
        "_component_pixels": [],
    }
    with mock.patch(
            "property_scores.bal_prescreen.v2.scan_vegetation_observations",
            return_value={"status": "ok", "observations": [observation]}):
        result = preliminary_bal_v2(
            -34.0, 150.0, subject_identity=_identity(), state="NSW",
            overlay=(None, [], None, True, "state_service"),
            elevation_fn=lambda *_: 100.0)
    assert result["status"] == "professional_assessment_required"
    assert result["preliminary_bal"] is None
    assert result["blockers"][0]["reason"] == "insufficient vegetation run"


def test_v2_takes_worst_observation_not_nearest():
    near_grass = {
        "component_id": 1, "wc_class": 30, "distance_m": 10.0,
        "bearing_deg": 90.0, "sector": "E", "veg_lat": -34.0,
        "veg_lng": 150.0001, "component_area_m2": 12_000,
        "component_edge_truncated": False, "_near_cell": (0, 0),
        "_component_pixels": [
            {"distance_m": 10.0, "bearing_deg": 90.0,
             "lat": -34.0, "lng": 150.0001, "row": 0, "col": 0,
             "wc_class": 30},
            {"distance_m": 20.0, "bearing_deg": 90.0,
             "lat": -34.0, "lng": 150.0002, "row": 0, "col": 1,
             "wc_class": 30},
            {"distance_m": 30.0, "bearing_deg": 90.0,
             "lat": -34.0, "lng": 150.0003, "row": 0, "col": 2,
             "wc_class": 30},
        ],
    }
    farther_forest = {
        "component_id": 2, "wc_class": 10, "distance_m": 15.0,
        "bearing_deg": 270.0, "sector": "W", "veg_lat": -34.0,
        "veg_lng": 149.99985, "component_area_m2": 12_000,
        "component_edge_truncated": False, "_near_cell": (0, 0),
        "_component_pixels": [
            {"distance_m": 15.0, "bearing_deg": 270.0,
             "lat": -34.0, "lng": 149.99985, "row": 0, "col": 0,
             "wc_class": 10},
            {"distance_m": 27.0, "bearing_deg": 270.0,
             "lat": -34.0, "lng": 149.99972, "row": 0, "col": 1,
             "wc_class": 10},
            {"distance_m": 40.0, "bearing_deg": 270.0,
             "lat": -34.0, "lng": 149.9996, "row": 0, "col": 2,
             "wc_class": 10},
        ],
    }
    with mock.patch(
            "property_scores.bal_prescreen.v2.scan_vegetation_observations",
            return_value={"status": "ok",
                          "observations": [near_grass, farther_forest]}):
        result = preliminary_bal_v2(
            -34.0, 150.0, subject_identity=_identity(), state="VIC",
            overlay=(None, [], None, True, "state_service"),
            elevation_fn=lambda *_: 100.0)
    assert result["status"] == "ok"
    assert result["limiting_observation"]["wc_class"] == 10
    assert result["limiting_observation"]["sector"] == "W"
    assert result["directions_assessed"] == ["E", "W"]
    assert result["bal_range"][0] == "BAL-LOW"
    assert result["uncertainty"]["low_threat_status"] == \
        "unresolved_from_worldcover"


def test_overlay_hit_does_not_classify_each_worldcover_patch_as_hazard():
    forest = {
        "component_id": 1, "wc_class": 10, "distance_m": 30.0,
        "bearing_deg": 90.0, "sector": "E", "veg_lat": -34.0,
        "veg_lng": 150.0003, "component_area_m2": 12_000,
        "component_edge_truncated": False, "_near_cell": (0, 0),
        "_component_pixels": [
            {"distance_m": 30.0, "bearing_deg": 90.0,
             "lat": -34.0, "lng": 150.0003, "row": 0, "col": 0,
             "wc_class": 10},
            {"distance_m": 40.0, "bearing_deg": 90.0,
             "lat": -34.0, "lng": 150.0004, "row": 0, "col": 1,
             "wc_class": 10},
            {"distance_m": 50.0, "bearing_deg": 90.0,
             "lat": -34.0, "lng": 150.0005, "row": 0, "col": 2,
             "wc_class": 10},
            {"distance_m": 60.0, "bearing_deg": 90.0,
             "lat": -34.0, "lng": 150.0006, "row": 0, "col": 3,
             "wc_class": 10},
        ],
    }
    with mock.patch(
            "property_scores.bal_prescreen.v2.scan_vegetation_observations",
            return_value={"status": "ok", "observations": [forest]}):
        result = preliminary_bal_v2(
            -34.0, 150.0, subject_identity=_identity(), state="VIC",
            overlay=("high", ["BMO"], "high", True, "state_service"),
            elevation_fn=lambda *_: 100.0)
    assert result["official_overlay"]["status"] == "in_zone"
    assert result["bal_range"][0] == "BAL-LOW"
    assert result["confidence"] == "low"


def test_overlay_hit_with_only_excluded_grass_returns_no_bal():
    grass = {
        "component_id": 1, "wc_class": 30, "distance_m": 60.0,
        "bearing_deg": 90.0, "sector": "E", "veg_lat": -34.0,
        "veg_lng": 150.0006, "component_area_m2": 12_000,
        "component_edge_truncated": False, "_near_cell": (0, 0),
        "_component_pixels": [],
    }
    with mock.patch(
            "property_scores.bal_prescreen.v2.scan_vegetation_observations",
            return_value={"status": "ok", "observations": [grass]}):
        result = preliminary_bal_v2(
            -34.0, 150.0, subject_identity=_identity(), state="VIC",
            overlay=("high", ["BMO"], "high", True, "state_service"),
            elevation_fn=lambda *_: 100.0)
    assert result["status"] == "professional_assessment_required"
    assert result["preliminary_bal"] is None


def test_v2_method1_over_20_degrees_returns_no_bal():
    observation = {
        "component_id": 1, "wc_class": 10, "distance_m": 20.0,
        "bearing_deg": 90.0, "sector": "E", "veg_lat": -34.0,
        "veg_lng": 150.0002, "component_area_m2": 12_000,
        "component_edge_truncated": False, "_near_cell": (0, 0),
        "_component_pixels": [
            {"distance_m": 20.0, "bearing_deg": 90.0,
             "lat": -34.0, "lng": 150.0002, "row": 0, "col": 0,
             "wc_class": 10},
            {"distance_m": 30.0, "bearing_deg": 90.0,
             "lat": -34.0, "lng": 150.0003, "row": 0, "col": 1,
             "wc_class": 10},
            {"distance_m": 40.0, "bearing_deg": 90.0,
             "lat": -34.0, "lng": 150.0004, "row": 0, "col": 2,
             "wc_class": 10},
            {"distance_m": 50.0, "bearing_deg": 90.0,
             "lat": -34.0, "lng": 150.0005, "row": 0, "col": 3,
             "wc_class": 10},
        ],
    }

    def steep(_lat, lng):
        return 120.0 if lng < 150.0003 else 100.0

    with mock.patch(
            "property_scores.bal_prescreen.v2.scan_vegetation_observations",
            return_value={"status": "ok", "observations": [observation]}):
        result = preliminary_bal_v2(
            -34.0, 150.0, subject_identity=_identity(), state="VIC",
            overlay=(None, [], None, True, "state_service"), elevation_fn=steep)
    assert result["status"] == "professional_assessment_required"
    assert result["preliminary_bal"] is None
    assert "exceeds 20 degrees" in result["blockers"][0]["reason"]
