"""Walkability tests — GTFS transit-stop merge (the missing-bus-stop fix).

Anchor (2026-06-10 Simon Kean verification): zero bus stops within 1500 m of
Turramurra station's bus interchange because Overture places lack AU public
transport stops. GTFS stops now feed the tram_bus scenario.
"""
import os
import tempfile

import pandas as pd
import pytest

from property_scores.common.overture import get_db, transit_stops_near
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
    assert transit_stops_near(get_db(), -33.7, 151.1, 1500, source="/tmp/nope_missing.parquet") == []


def test_bus_stop_category_maps_to_tram_bus_scenario():
    assert _match_category("bus_stop", "Somewhere St at Other St") == "tram_bus"
    assert _match_category("tram_stop", "Stop 12") == "tram_bus"


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
    assert sports_fields_near(get_db(), -33.7, 151.1, 1500, source="/tmp/nope2.parquet") == []


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
                 "osm_amenities_near", "rail_stops_near", "roads_near"):
        monkeypatch.setattr(walk, name, lambda *a, **k: [])
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
