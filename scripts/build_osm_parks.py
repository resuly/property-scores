#!/usr/bin/env python3
"""Build au_osm_parks.parquet: public park / green-space POLYGONS from OSM.

The walkability "park" scenario used to read Overture places only, and most of
those (4,280 of 4,575 park-class places in greater Sydney, April 2026 release)
come from Meta: Facebook pages placed at whatever address the page owner typed.
A page whose address is just "Sydney, NSW" lands on the locality centroid, so
"Loftus, Royal National Park" sat 160 m from Crows Nest station and
"Ayers Rock, Uluru NT" in the CBD (2026-11-09 audit). OSM maps the parks
themselves, as polygons, so the scenario now measures the distance to the
nearest real park boundary instead.

Source: the Overture Maps base theme ``land_use`` type, which carries the OSM
leisure/boundary polygons with OSM ids (licence ODbL-1.0; credited through the
walkability ``osm_amenity_categories`` disclosure like the other OSM streams).
Reading it through DuckDB is one bbox-filtered scan, no Overpass mirrors and no
multipolygon assembly.

Kept (named polygons only; the name is what the payload shows):
  park/park, park/village_green        OSM leisure=park / landuse=village_green
  protected/national_park, nature_reserve
  recreation/recreation_ground         council reserves and ovals
  horticulture/garden                  only >= 2,000 m2 and named like a
                                       public garden; most OSM leisure=garden
                                       ways are private back gardens
Not kept: dog_park (own scenario, au_osm_amenities), pitch / golf (sports),
managed/grass (road verges), cemetery.

Output columns: id, name, class, area_m2, geometry (WKB), xmin, xmax, ymin, ymax
Usage: .venv/bin/python scripts/build_osm_parks.py [--release 2026-10-21.0]
                                                    [--bbox W,S,E,N] [--out PATH]
"""
import argparse
import os
import sys
from pathlib import Path

DEFAULT_RELEASE = "2026-10-21.0"
AU_BBOX = (112.0, -44.5, 154.5, -9.0)  # west, south, east, north

# A degree of latitude in metres; longitude is scaled by cos(latitude) inline.
_M_PER_DEG = 111_320.0

PARK_POLYGON_SQL = """(
    (subtype = 'park' AND class IN ('park', 'village_green'))
 OR (subtype = 'protected' AND class IN ('national_park', 'nature_reserve'))
 OR (subtype = 'recreation' AND class = 'recreation_ground')
 OR (subtype = 'horticulture' AND class = 'garden'
     AND area_m2 >= 2000
     AND regexp_matches(lower(name), 'garden|botanic|arboretum|reserve|park'))
)"""

# Sanity floor for a full-AU build: the 2026-10-21.0 release gives 37,169
# polygons (park 28,055, nature_reserve 4,524, recreation_ground 2,956,
# national_park 1,279, garden 312, village_green 43); the floor is ~80% of it.
# A run that returns far fewer polygons is a broken download, not a real
# shrink, and must not replace the last good file.
MIN_AU_ROWS = 30_000


def build(release: str, bbox: tuple, out_path: Path, min_rows: int,
          memory_limit: str = "3GB") -> int:
    import duckdb
    w, s, e, n = bbox
    db = duckdb.connect()
    db.sql(f"SET memory_limit='{memory_limit}'")
    db.install_extension("spatial")
    db.load_extension("spatial")
    db.install_extension("httpfs")
    db.load_extension("httpfs")
    db.sql("SET s3_region='us-west-2'")
    src = (f"read_parquet('s3://overturemaps-us-west-2/release/{release}"
           f"/theme=base/type=land_use/*', hive_partitioning=1)")
    tmp = out_path.with_suffix(".parquet.tmp")
    db.sql(f"""
        COPY (
            SELECT id, name, class, area_m2,
                   ST_AsWKB(geometry) AS geometry,
                   bbox.xmin AS xmin, bbox.xmax AS xmax,
                   bbox.ymin AS ymin, bbox.ymax AS ymax
            FROM (
                SELECT id, subtype, class, names.primary AS name, geometry, bbox,
                       ST_Area(geometry) * {_M_PER_DEG} * {_M_PER_DEG}
                         * cos(radians((bbox.ymin + bbox.ymax) / 2)) AS area_m2
                FROM {src}
                WHERE bbox.xmin <= {e} AND bbox.xmax >= {w}
                  AND bbox.ymin <= {n} AND bbox.ymax >= {s}
                  AND subtype IN ('park', 'protected', 'recreation', 'horticulture')
                  AND names.primary IS NOT NULL
                  AND ST_GeometryType(geometry) IN ('POLYGON', 'MULTIPOLYGON')
            )
            WHERE {PARK_POLYGON_SQL}
            ORDER BY ymin, xmin
        ) TO '{tmp}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 2048)
    """)
    rows = db.sql(f"SELECT count(*) FROM read_parquet('{tmp}')").fetchone()[0]
    by_class = db.sql(f"SELECT class, count(*) FROM read_parquet('{tmp}') "
                      "GROUP BY 1 ORDER BY 2 DESC").fetchall()
    print(f"{rows} polygons {by_class}", file=sys.stderr)
    if rows < min_rows:
        tmp.unlink(missing_ok=True)
        sys.exit(f"only {rows} park polygons (< {min_rows}); keeping last good {out_path}")
    # Small row groups over ymin-sorted rows let a point query skip all but
    # a few groups (one 37k-row group cost ~90 ms per walkability call).
    os.replace(tmp, out_path)
    print(f"Saved {out_path} ({out_path.stat().st_size / 1e6:.1f} MB)", file=sys.stderr)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--release", default=DEFAULT_RELEASE)
    ap.add_argument("--bbox", help="W,S,E,N (default: Australia)")
    ap.add_argument("--memory-limit", default="3GB")
    ap.add_argument("--out", type=Path,
                    default=Path(__file__).resolve().parents[1] / "data" / "au_osm_parks.parquet")
    args = ap.parse_args()
    bbox = tuple(float(v) for v in args.bbox.split(",")) if args.bbox else AU_BBOX
    build(args.release, bbox, args.out, MIN_AU_ROWS if not args.bbox else 1,
          args.memory_limit)


if __name__ == "__main__":
    main()
