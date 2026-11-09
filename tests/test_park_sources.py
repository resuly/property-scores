"""Parks come from OSM polygons; Meta-sourced Overture park places are dropped.

Fixtures are real records, cut on 2026-11-09:
  tests/fixtures/parks/overture_pois.parquet  every Overture place (production
      file, April 2026 release) with a bbox in 151.185..151.218 E,
      -33.840..-33.812 S (Crows Nest / St Leonards / North Sydney), plus the
      "Ayers Rock, Uluru NT" record that Meta pins to the Sydney CBD.
  tests/fixtures/parks/au_osm_parks.parquet   scripts/build_osm_parks.py
      --bbox 151.18,-33.845,151.225,-33.805 against Overture 2026-10-21.0
      base land_use (OSM polygons).
"""
from pathlib import Path

import duckdb
import pytest

from property_scores.common import overture

FIX = Path(__file__).resolve().parent / "fixtures" / "parks"

# Near Crows Nest (Willoughby Rd). The Meta page "Loftus, Royal National
# Park" (address just "Sydney, NSW") sits 25 m away and used to be the
# nearest park; Royal National Park is 25 km south.
CROWS_NEST = (-33.8243, 151.2012)
AYERS_ROCK_GHOST = (-33.86224, 151.21232)


@pytest.fixture
def fixture_data(monkeypatch):
    monkeypatch.setattr(overture, "data_path", lambda f: FIX / f)
    return overture.get_db()


def _parks(rows):
    return [r for r in rows if r[0] in overture.PARK_CATEGORIES]


def test_park_categories_match_walkability_map():
    from property_scores.walkability.score import CATEGORY_MAP
    assert set(overture.PARK_CATEGORIES) == {
        k for k, (scenario, _) in CATEGORY_MAP.items() if scenario == "park"}


def test_meta_only_ghost_parks_dropped(fixture_data):
    db = fixture_data
    before = _parks(overture.pois_near_detailed(db, *CROWS_NEST, 1500))
    after = _parks(overture.pois_near_detailed(
        db, *CROWS_NEST, 1500, drop_meta_parks=True))
    # The fixture really carries the ghost, and it really was the nearest.
    assert before[0][4] == "Loftus, Royal National Park"
    assert round(before[0][1]) == 25
    assert "Loftus, Royal National Park" not in {r[4] for r in after}
    ghost = overture.pois_near_detailed(db, *AYERS_ROCK_GHOST, 300)
    assert "Ayers Rock, Uluru NT" in {r[4] for r in ghost}
    assert "Ayers Rock, Uluru NT" not in {r[4] for r in overture.pois_near_detailed(
        db, *AYERS_ROCK_GHOST, 300, drop_meta_parks=True)}


def test_only_meta_park_rows_are_dropped(fixture_data):
    db = fixture_data
    before = overture.pois_near_detailed(db, *CROWS_NEST, 1500)
    after = overture.pois_near_detailed(db, *CROWS_NEST, 1500,
                                        drop_meta_parks=True)
    # Non-park rows (cafes, schools ...) are untouched, Meta or not.
    # (sorted: rows at identical distances come back in any order)
    assert (sorted(map(repr, (r for r in before if r[0] not in overture.PARK_CATEGORIES)))
            == sorted(map(repr, (r for r in after if r[0] not in overture.PARK_CATEGORIES))))
    # Foursquare-backed park places stay (scope is Meta-sourced records).
    assert "the church creative space" in {r[4] for r in _parks(after)}
    # Exactly the Meta park rows went, judged from the raw records.
    meta_only = {row[0] for row in duckdb.sql(f"""
        SELECT names.primary FROM '{FIX / "overture_pois.parquet"}'
        WHERE categories.primary IN {tuple(overture.PARK_CATEGORIES)}
          AND list_transform(sources, s -> s.dataset) = ['meta', 'Overture']
    """).fetchall()}
    dropped = {r[4] for r in _parks(before)} - {r[4] for r in _parks(after)}
    assert dropped and dropped <= meta_only
    assert not ({r[4] for r in _parks(after)} & meta_only)


def test_osm_parks_measure_to_the_park_edge(fixture_data):
    rows = overture.osm_parks_near(fixture_data, *CROWS_NEST, 1500)
    names = [r[4] for r in rows]
    assert len(names) == len({n.lower() for n in names})  # one row per park
    assert all(r[0] == "park" for r in rows)
    assert [r[1] for r in rows] == sorted(r[1] for r in rows)
    by_name = {r[4]: r for r in rows}
    # St Leonards Park (14.7 ha): edge 921 m, centroid 1,156 m. Hume Street
    # Park Expansion is two polygons (centroids 100 m / 122 m); one row, edge.
    assert 900 < by_name["St Leonards Park"][1] < 950
    assert 85 < by_name["Hume Street Park Expansion"][1] < 95
    # Inside a park the distance is zero and the point is the query point.
    inside = overture.osm_parks_near(fixture_data, -33.8258, 151.2017, 300)
    assert inside[0][4] == "Ernest Place" and inside[0][1] == 0
    assert inside[0][2:4] == pytest.approx((151.2017, -33.8258))


def test_osm_parks_absent_file_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(overture, "data_path", lambda f: tmp_path / f)
    assert not overture.osm_parks_available()
    assert overture.osm_parks_near(overture.get_db(), *CROWS_NEST) == []


def _walk(monkeypatch, parks_present=True):
    from property_scores.walkability import score as walk
    data = (lambda f: FIX / f) if parks_present else (
        lambda f: FIX / f if f != overture.PARKS_FILE else FIX / "missing.parquet")
    monkeypatch.setattr(overture, "data_path", data)
    for name in ("transit_stops_near", "sports_fields_near",
                 "osm_amenities_near", "rail_stops_near", "walking_trails_near"):
        monkeypatch.setattr(walk, name, lambda *a, **k: [])
    monkeypatch.setattr(walk, "road_crossings", lambda *a, **k: set())
    monkeypatch.setattr(walk, "water_crossings", lambda *a, **k: set())
    monkeypatch.setattr(walk, "_slope_penalty", lambda *a, **k: 1.0)
    return walk.walkability_score(*CROWS_NEST)


def test_walkability_nearest_park_is_a_real_park(monkeypatch):
    result = _walk(monkeypatch)
    park = result["category_scores"]["park"]
    assert park["nearest"]["name"] == "Hume Street Park Expansion"
    assert park["distance_m"] == 89
    names = {o["name"] for o in park.get("options", [])}
    assert "Loftus, Royal National Park" not in names
    assert "park" in result["osm_amenity_categories"]


def test_walkability_without_park_polygons_keeps_old_stream(monkeypatch):
    result = _walk(monkeypatch, parks_present=False)
    park = result["category_scores"]["park"]
    # No OSM file deployed: nothing replaces the Overture places, so they are
    # not dropped either (graceful pre-data deploy).
    assert park["nearest"]["name"] == "Loftus, Royal National Park"
    assert "park" not in result.get("osm_amenity_categories", [])
