"""Walkability line geometry must use actual intersections and nearest points."""

import duckdb

from property_scores.common.overture import (pois_near_detailed, road_crossings,
                                              walking_trails_near,
                                              water_crossings)


def _roads_parquet(tmp_path):
    path = tmp_path / "roads.parquet"
    db = duckdb.connect()
    db.install_extension("spatial")
    db.load_extension("spatial")
    db.execute("""
        CREATE TABLE roads AS
        SELECT * FROM (VALUES
          ('trail-near', struct_pack("primary" := 'The Bay Run'), 'road',
           'footway', NULL,
           ST_GeomFromText('LINESTRING(151.1450 -33.8696,151.1460 -33.8696)'),
           struct_pack(xmin := 151.1450, xmax := 151.1460,
                       ymin := -33.8696, ymax := -33.8696)),
          ('motorway-crossing', struct_pack("primary" := 'Crossing Road'), 'road',
           'motorway', NULL,
           ST_GeomFromText('LINESTRING(151.1455 -33.8710,151.1455 -33.8670)'),
           struct_pack(xmin := 151.1455, xmax := 151.1455,
                       ymin := -33.8710, ymax := -33.8670)),
          ('motorway-behind', struct_pack("primary" := 'Behind Road'), 'road',
           'motorway', NULL,
           ST_GeomFromText('LINESTRING(151.1430 -33.8710,151.1430 -33.8670)'),
           struct_pack(xmin := 151.1430, xmax := 151.1430,
                       ymin := -33.8710, ymax := -33.8670))
        ) AS t(id, names, subtype, class, subclass, geometry, bbox)
    """)
    db.execute("COPY roads TO ? (FORMAT PARQUET)", [str(path)])
    return path


def test_named_trail_uses_nearest_point_on_line(tmp_path):
    path = _roads_parquet(tmp_path)
    db = duckdb.connect()
    db.install_extension("spatial")
    db.load_extension("spatial")
    rows = walking_trails_near(
        db, -33.86877, 151.14501, 1500, source=str(path))
    assert rows
    category, distance, lng, lat, name = rows[0]
    assert category == "hiking_trail"
    assert name == "The Bay Run"
    assert distance < 100
    assert abs(lat - (-33.8696)) < 0.00001


def test_long_trail_bbox_overlap_reaches_exact_distance_filter(tmp_path):
    path = tmp_path / "long-trail.parquet"
    db = duckdb.connect()
    db.install_extension("spatial")
    db.load_extension("spatial")
    db.execute("""
        CREATE TABLE roads AS SELECT
          struct_pack("primary" := 'Long Creek Trail') AS names,
          'road' AS subtype,
          'footway' AS class,
          NULL::VARCHAR AS subclass,
          ST_GeomFromText(
            'LINESTRING(151.1000 -33.8696,151.1460 -33.8696)') AS geometry,
          struct_pack(xmin := 151.1000, xmax := 151.1460,
                      ymin := -33.8696, ymax := -33.8696) AS bbox
    """)
    db.execute("COPY roads TO ? (FORMAT PARQUET)", [str(path)])

    rows = walking_trails_near(
        db, -33.86877, 151.14501, 1500, source=str(path))

    assert rows
    assert rows[0][4] == "Long Creek Trail"
    assert rows[0][1] < 100


def test_road_barrier_requires_actual_property_to_poi_intersection(tmp_path):
    path = _roads_parquet(tmp_path)
    db = duckdb.connect()
    db.install_extension("spatial")
    db.load_extension("spatial")
    targets = [
        ("east", 151.1465, -33.86877),
        ("west", 151.1445, -33.86877),
    ]
    hit = road_crossings(
        db, -33.86877, 151.14501, targets, source=str(path))
    assert hit == {"east"}


def test_road_barrier_query_failure_is_explicit_not_fail_open():
    class BrokenDB:
        def sql(self, _query):
            raise RuntimeError("spatial extension unavailable")

    assert road_crossings(
        BrokenDB(), -33.86877, 151.14501,
        [("supermarket", 151.1465, -33.86877)],
        source="roads.parquet",
    ) is None


def test_water_barrier_query_failure_is_explicit_not_fail_open(tmp_path, monkeypatch):
    path = tmp_path / "water.parquet"
    path.write_bytes(b"not a parquet file")
    monkeypatch.setattr(
        "property_scores.common.overture.data_path", lambda _name: path)

    assert water_crossings(
        duckdb.connect(), -33.86877, 151.14501,
        [("supermarket", 151.1465, -33.86877)],
    ) is None


def test_generic_school_with_primary_website_is_promoted(tmp_path, monkeypatch):
    path = tmp_path / "pois.parquet"
    db = duckdb.connect()
    db.install_extension("spatial")
    db.load_extension("spatial")
    db.execute("""
        CREATE TABLE pois AS SELECT
          struct_pack("primary" := 'school') AS categories,
          struct_pack("primary" := 'The Springfield Anglican College') AS names,
          ['https://example.edu/our-college/primary-schooling']::VARCHAR[] AS websites,
          ST_GeomFromText('POINT(152.907723 -27.656501)') AS geometry,
          struct_pack(xmin := 152.907723, xmax := 152.907723,
                      ymin := -27.656501, ymax := -27.656501) AS bbox
    """)
    db.execute("COPY pois TO ? (FORMAT PARQUET)", [str(path)])
    monkeypatch.setattr(
        "property_scores.common.overture.data_path", lambda _name: path)

    rows = pois_near_detailed(db, -27.65638, 152.90765, 300)
    assert rows[0][0] == "primary_school"
    assert rows[0][1] < 30
