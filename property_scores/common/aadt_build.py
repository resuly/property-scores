"""Shared helper: write a per-state measured-AADT parquet in the schema that
property_scores.common.overture.aadt_near() reads via its aadt_*.parquet glob.

Each state downloader collects rows of:
    {"aadt": int, "hv_pct": float(0..1), "road_name": str|None, "wkt": str}
and calls write_aadt_parquet(rows, "data/aadt_<state>.parquet").

Schema produced (consumed by aadt_near):
    aadt       INTEGER
    hv_pct     DOUBLE    heavy-vehicle FRACTION 0..1 (score.py multiplies by 100)
    road_name  VARCHAR
    geometry   GEOMETRY  native LINESTRING/POINT in WGS84
    xmin,ymin  DOUBLE    flat bbox min-corner (bbox prefilter columns)
"""

import os

import duckdb


def coords_to_wkt(geom_type, coords):
    """GeoJSON-style geometry (type, coordinates) -> WKT, or None if unusable."""
    if not coords:
        return None

    def pt(c):
        return f"{c[0]} {c[1]}"

    if geom_type == "Point":
        return f"POINT ({pt(coords)})"
    if geom_type == "LineString":
        pts = [c for c in coords if len(c) >= 2]
        if len(pts) >= 2:
            return "LINESTRING (" + ", ".join(pt(c) for c in pts) + ")"
        if len(pts) == 1:
            return f"POINT ({pt(pts[0])})"
        return None
    if geom_type == "MultiLineString":
        parts = []
        for line in coords:
            pts = [c for c in line if len(c) >= 2]
            if len(pts) >= 2:
                parts.append("(" + ", ".join(pt(c) for c in pts) + ")")
        if parts:
            return "MULTILINESTRING (" + ", ".join(parts) + ")"
        for line in coords:
            if line:
                return f"POINT ({pt(line[0])})"
        return None
    return None


def clamp_hv(value):
    """Coerce a heavy-vehicle figure to a 0..1 fraction with a sane default."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.05
    if v > 1.0:  # given as a percent
        v = v / 100.0
    if 0.0 < v < 0.35:
        return round(v, 4)
    return 0.05


def write_aadt_parquet(rows, out_path):
    """rows: list of dicts {aadt, hv_pct, road_name, wkt}. Writes parquet."""
    rows = [r for r in rows if r.get("wkt") and r.get("aadt") and r["aadt"] > 0]
    if not rows:
        raise ValueError("no usable rows to write")
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("CREATE TABLE staging (aadt INTEGER, hv_pct DOUBLE, road_name VARCHAR, wkt VARCHAR)")
    con.executemany(
        "INSERT INTO staging VALUES (?, ?, ?, ?)",
        [(int(r["aadt"]), float(r.get("hv_pct") or 0.05),
          (r.get("road_name") or None), r["wkt"]) for r in rows],
    )
    tmp = out_path + ".tmp.parquet"
    con.execute(f"""
        COPY (
            WITH g AS (
                SELECT aadt, hv_pct, road_name, ST_GeomFromText(wkt) AS geometry
                FROM staging WHERE wkt IS NOT NULL
            )
            SELECT aadt, hv_pct, road_name, geometry,
                   ST_XMin(geometry) AS xmin, ST_YMin(geometry) AS ymin
            FROM g
        ) TO '{tmp}' (FORMAT PARQUET)
    """)
    os.replace(tmp, out_path)
    n = con.execute(f"SELECT count(*) FROM read_parquet('{out_path}')").fetchone()[0]
    return n


def data_out(filename):
    d = os.environ.get("DATA_DIR")
    if not d:
        here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        d = os.path.join(here, "data")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, filename)
