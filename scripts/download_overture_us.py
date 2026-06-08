#!/usr/bin/env python3
"""Download Overture US roads + buildings for the 4 metros that have dense
NTAD road-noise raster coverage, so we can sample noise training points and
compute geometry features identical to the NL/UK/DE transfer pipeline.

Background
----------
We already have the CONUS NTAD road-noise raster
(data/us/CONUS_road_noise_2020/State_rasters/*.tif, LAeq24h 45-85 dB, 30 m).
To turn raster pixels into training points we need the matching Overture road
classes + building geometry so the per-point features line up with the
NL/UK/DE feature set (per-class inverse-distance / nearest / ring counts;
building heights from centroids).

Output schema (MUST match data/eu/nl_roads.parquet + nl_buildings.parquet so
the existing poc_eu_transfer*.py / build_noise_model.py readers work unchanged):
  roads:     class VARCHAR, geometry GEOMETRY, xmin DOUBLE, ymin DOUBLE
  buildings: h DOUBLE, clng DOUBLE, clat DOUBLE   (h=height, clng/clat=centroid)

Overture access (per handoff conventions)
------------------------------------------
- Public anonymous S3 bucket overturemaps-us-west-2, region us-west-2.
- Release is date-pinned (`2026-05-20.0` here); newest version on
  https://docs.overturemaps.org/getting-data/ (it rolls ~monthly).
- INSTALL spatial; LOAD spatial; INSTALL httpfs; LOAD httpfs;
- CRITICAL: a single query with many bbox OR-clauses CANNOT push the predicate
  down and will full-scan ~2.3B buildings and hang. So we query ONE bbox at a
  time and UNION the per-city results locally. Memory-safe.
- Timing (US +ve latitudes, no negative-latitude quirk): roads ~100-140s/bbox,
  buildings ~200-260s/bbox over a home connection.

Usage
-----
    .venv/bin/python scripts/download_overture_us.py            # both layers, all cities
    .venv/bin/python scripts/download_overture_us.py --type roads
    .venv/bin/python scripts/download_overture_us.py --type buildings
    .venv/bin/python scripts/download_overture_us.py --release 2026-06-XX.0
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import duckdb

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "us"
DEFAULT_RELEASE = "2026-05-20.0"

# 4 representative US metros with dense NTAD road-noise coverage.
# (lat, lng) centre; ~0.3 deg box -> +/-0.15 deg each side.
HALF = 0.15
CITIES = {
    "los_angeles": (34.05, -118.25),
    "new_york": (40.71, -74.01),
    "chicago": (41.88, -87.63),
    "houston": (29.76, -95.37),
}

# Road classes kept = same set the AU/EU transfer code uses.
ROAD_CLASSES = (
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "residential", "service", "unclassified", "living_street",
)


def _bbox(lat: float, lng: float) -> tuple[float, float, float, float]:
    """(xmin, ymin, xmax, ymax) for a ~0.3 deg box centred on (lat, lng)."""
    return (lng - HALF, lat - HALF, lng + HALF, lat + HALF)


def _connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("INSTALL spatial;LOAD spatial;INSTALL httpfs;LOAD httpfs;")
    con.execute("SET s3_region='us-west-2';")
    con.execute("SET preserve_insertion_order=false;")
    return con


def _seg_url(release: str) -> str:
    return f"s3://overturemaps-us-west-2/release/{release}/theme=transportation/type=segment/*"


def _bld_url(release: str) -> str:
    return f"s3://overturemaps-us-west-2/release/{release}/theme=buildings/type=building/*"


def download_roads(con: duckdb.DuckDBPyConnection, release: str, out: Path) -> int:
    seg = _seg_url(release)
    classes = ", ".join(f"'{c}'" for c in ROAD_CLASSES)
    union_parts = []
    for name, (lat, lng) in CITIES.items():
        xmin, ymin, xmax, ymax = _bbox(lat, lng)
        t0 = time.time()
        print(f"[roads] {name} bbox=({xmin:.3f},{ymin:.3f},{xmax:.3f},{ymax:.3f}) ...", flush=True)
        tbl = f"roads_{name}"
        # ONE bbox -> predicate pushes down. Flatten bbox.xmin/ymin to plain
        # columns so the output schema matches data/eu/nl_roads.parquet.
        con.execute(f"""
            CREATE TEMP TABLE {tbl} AS
            SELECT class, geometry, bbox.xmin AS xmin, bbox.ymin AS ymin
            FROM read_parquet('{seg}', filename=false, hive_partitioning=1)
            WHERE bbox.xmin BETWEEN {xmin} AND {xmax}
              AND bbox.ymin BETWEEN {ymin} AND {ymax}
              AND subtype = 'road'
              AND class IN ({classes})
        """)
        n = con.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
        print(f"        {n:,} segments ({time.time()-t0:.0f}s)", flush=True)
        union_parts.append(f"SELECT * FROM {tbl}")

    out.parent.mkdir(parents=True, exist_ok=True)
    con.execute(
        f"COPY ({' UNION ALL '.join(union_parts)}) TO '{out}' (FORMAT PARQUET)"
    )
    total = con.execute(
        f"SELECT count(*) FROM read_parquet('{out}')"
    ).fetchone()[0]
    return total


def download_buildings(con: duckdb.DuckDBPyConnection, release: str, out: Path) -> int:
    bld = _bld_url(release)
    union_parts = []
    for name, (lat, lng) in CITIES.items():
        xmin, ymin, xmax, ymax = _bbox(lat, lng)
        t0 = time.time()
        print(f"[buildings] {name} bbox=({xmin:.3f},{ymin:.3f},{xmax:.3f},{ymax:.3f}) ...", flush=True)
        tbl = f"bld_{name}"
        # height + centroid lng/lat, matching data/eu/nl_buildings.parquet
        # (h, clng, clat). Default height 6 m when missing (same as EU code).
        con.execute(f"""
            CREATE TEMP TABLE {tbl} AS
            SELECT COALESCE(height, 6.0) AS h,
                   ST_X(ST_Centroid(geometry)) AS clng,
                   ST_Y(ST_Centroid(geometry)) AS clat
            FROM read_parquet('{bld}', filename=false, hive_partitioning=1)
            WHERE bbox.xmin BETWEEN {xmin} AND {xmax}
              AND bbox.ymin BETWEEN {ymin} AND {ymax}
        """)
        n = con.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
        print(f"            {n:,} buildings ({time.time()-t0:.0f}s)", flush=True)
        union_parts.append(f"SELECT * FROM {tbl}")

    out.parent.mkdir(parents=True, exist_ok=True)
    con.execute(
        f"COPY ({' UNION ALL '.join(union_parts)}) TO '{out}' (FORMAT PARQUET)"
    )
    total = con.execute(
        f"SELECT count(*) FROM read_parquet('{out}')"
    ).fetchone()[0]
    return total


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--type", choices=["roads", "buildings", "both"], default="both")
    ap.add_argument("--release", default=DEFAULT_RELEASE,
                    help=f"Overture release version (default {DEFAULT_RELEASE})")
    args = ap.parse_args()

    print(f"Overture release: {args.release}")
    print(f"Cities: {', '.join(CITIES)}")
    con = _connect()
    t0 = time.time()

    if args.type in ("roads", "both"):
        out = DATA_DIR / "us_roads.parquet"
        n = download_roads(con, args.release, out)
        mb = out.stat().st_size / 1e6
        print(f"\n==> {out}: {n:,} segments, {mb:.1f} MB\n", flush=True)

    if args.type in ("buildings", "both"):
        out = DATA_DIR / "us_buildings.parquet"
        n = download_buildings(con, args.release, out)
        mb = out.stat().st_size / 1e6
        print(f"\n==> {out}: {n:,} buildings, {mb:.1f} MB\n", flush=True)

    print(f"All done in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
