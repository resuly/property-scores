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
