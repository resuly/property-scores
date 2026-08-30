"""Walkability tests — GTFS transit-stop merge (the missing-bus-stop fix).

Anchor (2026-06-10 Simon Kean verification): zero bus stops within 1500 m of
Turramurra station's bus interchange because Overture places lack AU public
transport stops. GTFS stops now feed the tram_bus scenario.
"""
import os
import tempfile

import pandas as pd
import pytest

from property_scores.common.overture import get_db, rail_stops_near, transit_stops_near
from property_scores.walkability.score import _match_category


@pytest.fixture
def stops_parquet(tmp_path):
    df = pd.DataFrame([
        {"stop_id": "nsw:1", "stop_name": "Ku-ring-gai Ave at Pacific Hwy",
         "lat": -33.7270, "lng": 151.1330, "mode": "bus", "state": "nsw"},
        {"stop_id": "vic:2", "stop_name": "Collins St Tram",
         "lat": -37.8160, "lng": 144.9640, "mode": "tram", "state": "vic"},
        {"stop_id": "nsw:3", "stop_name": "Far Away Stop",
         "lat": -34.5000, "lng": 151.1330, "mode": "bus", "state": "nsw"},
    ])
    p = tmp_path / "stops.parquet"
    df.to_parquet(p, index=False)
    return str(p)


def test_transit_stops_near_finds_bus(stops_parquet):
    rows = transit_stops_near(get_db(), -33.7269, 151.1334, 1500, source=stops_parquet)
    cats = [r[0] for r in rows]
    assert "bus_stop" in cats
    # 5-tuple shape matches the POI stream: (category, dist_m, lng, lat, name)
    cat, dist, lng, lat, name = rows[0]
    assert dist < 200 and name.startswith("Ku-ring-gai")


def test_transit_stops_near_radius_excludes_far(stops_parquet):
    rows = transit_stops_near(get_db(), -33.7269, 151.1334, 1500, source=stops_parquet)
    assert all("Far Away" not in r[4] for r in rows)


def test_transit_stops_near_tram_category(stops_parquet):
    rows = transit_stops_near(get_db(), -37.8161, 144.9645, 800, source=stops_parquet)
    assert rows and rows[0][0] == "tram_stop"


def test_transit_stops_missing_file_graceful():
    assert transit_stops_near(
        get_db(), -33.7, 151.1, 1500,
        source="/tmp/nope_missing.parquet") is None


def test_bus_stop_category_maps_to_tram_bus_scenario():
    assert _match_category("bus_stop", "Somewhere St at Other St") == "tram_bus"
    assert _match_category("tram_stop", "Stop 12") == "tram_bus"


def test_rail_replacement_bus_stop_is_not_a_train_station(tmp_path):
    frame = pd.DataFrame([
        {"stop_id": "vic:replacement",
         "stop_name": "Parkville Railway Station Rail Replacement Bus Stop",
         "lat": -37.799946, "lng": 144.959552, "state": "vic"},
        {"stop_id": "vic:station", "stop_name": "Parkville Railway Station",
         "lat": -37.799874, "lng": 144.959542, "state": "vic"},
        {"stop_id": "tas:bus", "stop_name": "Cygnet Bus Station",
         "lat": -37.799800, "lng": 144.959500, "state": "tas"},
        {"stop_id": "vic:street", "stop_name": "Station St/Rutland Rd",
         "lat": -37.799700, "lng": 144.959400, "state": "vic"},
        {"stop_id": "sa:tram", "stop_name": "Adelaide Railway Station Tram Stop",
         "lat": -37.799600, "lng": 144.959300, "state": "sa"},
    ])
    path = tmp_path / "rail.parquet"
    frame.to_parquet(path, index=False)

    rows = rail_stops_near(
        get_db(), -37.8005, 144.9634, 1500, source=str(path))

    assert [row[4] for row in rows] == ["Parkville Railway Station"]


def test_overture_day_care_preschool_maps_to_childcare():
    assert _match_category("day_care_preschool", "Rosny Child Care Centre") == "childcare"


def test_exact_primary_taxonomy_does_not_require_school_word_in_name():
    assert _match_category("primary_school", "The Springfield Anglican College") == "primary_school"
    assert _match_category("elementary_school", "Greenfields Campus") == "primary_school"


@pytest.fixture
def fields_parquet(tmp_path):
    df = pd.DataFrame([
        {"name": "Golden Jubilee Field", "lat": -33.7041, "lng": 151.1378,
         "leisure": "pitch", "sport": "cricket", "state": "nsw"},
        {"name": "Far Field", "lat": -35.0, "lng": 151.0,
         "leisure": "pitch", "sport": "", "state": "nsw"},
    ])
    p = tmp_path / "fields.parquet"
    df.to_parquet(p, index=False)
    return str(p)


def test_sports_fields_near_finds_oval(fields_parquet):
    from property_scores.common.overture import sports_fields_near
    rows = sports_fields_near(get_db(), -33.7047, 151.1368, 1500, source=fields_parquet)
    assert rows and rows[0][0] == "sports_and_recreation_venue"
    assert rows[0][4] == "Golden Jubilee Field"
    assert all(r[4] != "Far Field" for r in rows)


def test_sports_fields_missing_file_graceful():
    from property_scores.common.overture import sports_fields_near
    assert sports_fields_near(
        get_db(), -33.7, 151.1, 1500,
        source="/tmp/nope2.parquet") is None


def test_sports_category_maps_to_sports_scenario():
    assert _match_category("sports_and_recreation_venue", "Golden Jubilee Field") == "sports"


def test_school_options_are_complete_while_other_categories_stay_bounded(monkeypatch):
    """The licensed score must not say eight schools and name only three."""
    from property_scores.walkability import score as walk

    rows = []
    for i in range(8):
        rows.append(("primary_school", 100 + i * 20,
                     144.9600 + i * 0.0001, -37.8100,
                     f"Primary School {i}"))
    for i in range(5):
        rows.append(("secondary_school", 200 + i * 20,
                     144.9700 + i * 0.0001, -37.8110,
                     f"Secondary School {i}"))
    for i in range(7):
        rows.append(("restaurant", 50 + i * 10,
                     144.9800 + i * 0.0001, -37.8120,
                     f"Restaurant {i}"))

    monkeypatch.setattr(walk, "get_db", lambda: object())
    monkeypatch.setattr(walk, "pois_near_detailed", lambda *a, **k: rows)
    for name in ("transit_stops_near", "sports_fields_near",
                 "osm_amenities_near", "rail_stops_near", "walking_trails_near"):
        monkeypatch.setattr(walk, name, lambda *a, **k: [])
    monkeypatch.setattr(walk, "road_crossings", lambda *a, **k: set())
    monkeypatch.setattr(walk, "water_crossings", lambda *a, **k: set())
    monkeypatch.setattr(walk, "_slope_penalty", lambda *a, **k: 1.0)

    result = walk.walkability_score(-37.8100, 144.9600)
    categories = result["category_scores"]

    for scenario, expected in (("primary_school", 8),
                               ("secondary_school", 5)):
        school = categories[scenario]
        assert school["count"] == expected
        assert len(school["options"]) == expected
        assert school["count"] == len(school["options"])

    restaurant = categories["restaurant"]
    assert restaurant["count"] == 7
    assert len(restaurant["options"]) == 3


def test_school_option_dedup_does_not_move_score_baseline(monkeypatch):
    """Disclosure count may shrink to distinct options; scoring count must not.

    Before the options fix, density decay used every matched source row.  The
    response now deduplicates same-named school options, but that disclosure
    change must not silently recalibrate customer scores.
    """
    from property_scores.walkability import score as walk

    rows = [
        ("primary_school", distance, 144.9600 + i * 0.0001, -37.8100,
         "One Primary School")
        for i, distance in enumerate((100, 120, 140))
    ] + [
        ("secondary_school", distance, 144.9700 + i * 0.0001, -37.8110,
         "One Secondary School")
        for i, distance in enumerate((200, 220, 240))
    ]

    monkeypatch.setattr(walk, "get_db", lambda: object())
    monkeypatch.setattr(walk, "pois_near_detailed", lambda *a, **k: rows)
    for name in ("transit_stops_near", "sports_fields_near",
                 "osm_amenities_near", "rail_stops_near", "walking_trails_near"):
        monkeypatch.setattr(walk, name, lambda *a, **k: [])
    monkeypatch.setattr(walk, "road_crossings", lambda *a, **k: set())
    monkeypatch.setattr(walk, "water_crossings", lambda *a, **k: set())
    monkeypatch.setattr(walk, "_slope_penalty", lambda *a, **k: 1.0)

    result = walk.walkability_score(-37.8100, 144.9600)
    primary = result["category_scores"]["primary_school"]
    secondary = result["category_scores"]["secondary_school"]

    # Three raw matches in each category avoid the <=1/<=2 density discounts,
    # exactly as before this change. The one delivered option is the distinct
    # school an integration can render.
    assert primary["count"] == len(primary["options"]) == 1
    assert secondary["count"] == len(secondary["options"]) == 1
    assert primary["decay"] == round(walk._decay(100), 2)
    assert secondary["decay"] == round(walk._decay(200), 2)

    total_weight = sum(cfg["weight"] for cfg in walk.SCENARIO_CONFIG.values())
    expected_score = round((
        walk.SCENARIO_CONFIG["primary_school"]["weight"] * walk._decay(100)
        + walk.SCENARIO_CONFIG["secondary_school"]["weight"] * walk._decay(200)
    ) / total_weight * 100)
    assert result["score"] == expected_score


def test_road_query_failure_is_conservative_and_disclosed(monkeypatch):
    from property_scores.walkability import score as walk

    rows = [("supermarket", 500, 145.0, -37.8, "Market")]
    monkeypatch.setattr(walk, "get_db", lambda: object())
    monkeypatch.setattr(walk, "pois_near_detailed", lambda *a, **k: rows)
    for name in ("transit_stops_near", "sports_fields_near",
                 "osm_amenities_near", "rail_stops_near", "walking_trails_near"):
        monkeypatch.setattr(walk, name, lambda *a, **k: [])
    monkeypatch.setattr(walk, "road_crossings", lambda *a, **k: None)
    monkeypatch.setattr(walk, "water_crossings", lambda *a, **k: set())
    monkeypatch.setattr(walk, "_slope_penalty", lambda *a, **k: 1.0)

    result = walk.walkability_score(-37.8, 145.0)
    assert result["category_scores"]["supermarket"]["barrier"] is True
    assert result["road_barrier_check"] == "unavailable_conservative"
    assert "conservatively" in result["disclaimer"]


def _one_market_walkability(monkeypatch, *, water_result=set(),
                            slope_result=(1.0, "data_returned", 1.5),
                            missing_stream: str | None = None):
    from property_scores.walkability import score as walk

    rows = [("supermarket", 300, 145.0, -37.8, "Market")]
    monkeypatch.setattr(walk, "get_db", lambda: object())
    monkeypatch.setattr(walk, "pois_near_detailed", lambda *a, **k: rows)
    for name in ("transit_stops_near", "sports_fields_near",
                 "osm_amenities_near", "rail_stops_near", "walking_trails_near"):
        value = None if name == missing_stream else []
        monkeypatch.setattr(walk, name, lambda *a, _value=value, **k: _value)
    monkeypatch.setattr(walk, "road_crossings", lambda *a, **k: set())
    monkeypatch.setattr(walk, "water_crossings", lambda *a, **k: water_result)
    monkeypatch.setattr(walk, "_slope_penalty", lambda *a, **k: slope_result)
    return walk.walkability_score(-37.8, 145.0)


def test_walkability_contract_never_claims_route_time(monkeypatch):
    result = _one_market_walkability(monkeypatch)

    contract = result["screening_contract"]
    assert contract["schema_version"] == "amenity-walkability-screening-v1"
    assert contract["distance_basis"] == "straight_line_metres"
    assert contract["route_network_time"] == "not_computed"
    assert contract["scenario_count"] == 24
    assert "named OSM trails" in contract["count_contract"]
    assert "route_type" in contract["transit_mode_boundary"]
    assert "400 m straight-line" in result["summary"]
    assert "1500 m straight-line screening radius" in result["summary"]
    assert "walking distance" not in result["summary"]
    assert "5 min walk" not in result["summary"]
    assert result["screening_label"] == "Very low amenity proximity"
    assert result["unique_facility_count"] == 1
    assert result["poi_count_basis"] == \
        "source_rows_with_named_trail_segments_deduplicated"
    assert result["amenity_source_categories"] == {
        "overture_places": ["supermarket"],
    }


def test_named_trail_segments_keep_only_the_nearest_facility_row():
    from property_scores.walkability import score as walk

    rows = [
        ("hiking_trail", 420, 145.0, -37.8, "Merri Creek Trail",
         "osm_named_trails"),
        ("hiking_trail", 180, 145.001, -37.801, "Merri Creek Trail",
         "osm_named_trails"),
        ("hiking_trail", 300, 145.002, -37.802, "Capital City Trail",
         "osm_named_trails"),
        ("cafe", 90, 145.003, -37.803, "Example Cafe", "overture_places"),
    ]

    out = walk._dedupe_named_trail_segments(rows)

    assert len(out) == 3
    merri = next(row for row in out if row[4] == "Merri Creek Trail")
    assert merri[1] == 180


def test_water_query_failure_is_not_silently_reported_clear(monkeypatch):
    result = _one_market_walkability(monkeypatch, water_result=None)

    assert result["coverage"]["water_barrier"] == "unavailable_unadjusted"
    assert result["water_barrier_check"] == "unavailable_unadjusted"
    assert "no water penalty was applied" in result["disclaimer"]


def test_missing_slope_coverage_is_neutral_but_explicit(monkeypatch):
    result = _one_market_walkability(
        monkeypatch, slope_result=(1.0, "unavailable_neutral", None))

    assert result["coverage"]["slope"] == "unavailable_neutral"
    assert "no slope penalty was applied" in result["disclaimer"]
    assert "slope_grade_proxy_pct" not in result


def test_missing_auxiliary_amenity_artifact_is_partial_not_clear(monkeypatch):
    result = _one_market_walkability(
        monkeypatch, missing_stream="rail_stops_near")

    assert result["coverage"]["amenities"] == "partial"
    assert result["coverage"]["amenity_sources"]["gtfs_rail"] == "unavailable"
    assert result["category_scores"]["train"]["count"] == 0


def test_osm_sports_source_is_credited_under_mapped_scenario(monkeypatch):
    from property_scores.walkability import score as walk

    monkeypatch.setattr(walk, "get_db", lambda: object())
    monkeypatch.setattr(walk, "pois_near_detailed", lambda *a, **k: [])
    monkeypatch.setattr(
        walk, "sports_fields_near",
        lambda *a, **k: [
            ("sports_and_recreation_venue", 0, 145.0, -37.8, "Monash Aquatic"),
        ])
    for name in ("transit_stops_near", "osm_amenities_near",
                 "rail_stops_near", "walking_trails_near"):
        monkeypatch.setattr(walk, name, lambda *a, **k: [])
    monkeypatch.setattr(walk, "road_crossings", lambda *a, **k: set())
    monkeypatch.setattr(walk, "water_crossings", lambda *a, **k: set())
    monkeypatch.setattr(
        walk, "_slope_penalty",
        lambda *a, **k: (1.0, "data_returned", 1.0))

    result = walk.walkability_score(-37.8, 145.0)

    assert result["osm_amenity_categories"] == ["sports"]
    assert result["amenity_source_categories"]["osm_sports"] == ["sports"]
    assert result["category_scores"]["sports"]["nearest"]["source"] == "osm_sports"
