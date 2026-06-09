"""Overture Maps data loading helpers via DuckDB."""

import glob

import duckdb

from property_scores.common.config import data_path

import threading

ROADS_FILE = "overture_roads.parquet"
POIS_FILE = "overture_pois.parquet"
WATER_FILE = "overture_water.parquet"
# Measured per-segment AADT ground truth, one parquet per state
# (aadt_vic.parquet, aadt_nsw.parquet, ...). Each file shares the schema
# aadt_near() expects (aadt, hv_pct, road_name, geometry, xmin, ymin) so they
# can be read together with a single glob. Drop a new state's parquet in and it
# is picked up automatically — no code change needed.
AADT_GLOB = "aadt_*.parquet"
NFDH_FILE = "nfdh_aadt_national.parquet"
PTV_SHAPES_FILE = "ptv_rail_shapes.parquet"
PTV_FREQ_FILE = "ptv_rail_frequency.parquet"
AU_RAIL_SHAPES_FILE = "au_rail_shapes.parquet"
AU_RAIL_FREQ_FILE = "au_rail_frequency.parquet"

_install_lock = threading.Lock()
_base_db = None


def get_db() -> duckdb.DuckDBPyConnection:
    """Return a per-call cursor off one shared, spatial-loaded base connection.

    The base connection is created once (install + load_extension('spatial'),
    ~47ms) and reused. Each call gets a cheap independent ``.cursor()`` that
    shares the loaded extension and parquet metadata. A single DuckDB connection
    is not safe to drive concurrently from multiple threads, but separate cursors
    off one database instance are — the supported multi-thread pattern — so the
    FastAPI threadpool workers share one warm connection instead of paying the
    connect + extension reload on every score call. All runtime queries are
    read-only (read_parquet, no temp tables / SET state), so cursors cannot
    collide on shared catalog state.
    """
    global _base_db
    if _base_db is None:
        with _install_lock:
            if _base_db is None:
                db = duckdb.connect()
                db.install_extension("spatial")
                db.load_extension("spatial")
                _base_db = db
    return _base_db.cursor()


def _local_or_fail(filename: str) -> str:
    p = data_path(filename)
    if not p.exists():
        raise FileNotFoundError(
            f"Data file not found: {p}\n"
            f"Run: python -m property_scores.common.download --type roads"
        )
    return str(p)


def roads_near(db: duckdb.DuckDBPyConnection, lat: float, lng: float,
               radius_m: int = 1000, *, source: str | None = None) -> list[tuple]:
    table = f"read_parquet('{source or _local_or_fail(ROADS_FILE)}')"
    delta = radius_m / 111_000 * 1.5
    import math
    m_per_deg = 111_320 * math.cos(math.radians(lat))
    deg_thresh = radius_m / m_per_deg
    sql = f"""
        SELECT class,
               ST_Distance(geometry, ST_Point({lng}, {lat})) * {m_per_deg} AS dist_m,
               CASE WHEN speed_limits IS NOT NULL AND len(speed_limits) > 0
                    THEN speed_limits[1].max_speed.value
                    ELSE NULL END AS speed_kmh,
               ST_X(ST_ClosestPoint(geometry, ST_Point({lng}, {lat}))) AS near_lng,
               ST_Y(ST_ClosestPoint(geometry, ST_Point({lng}, {lat}))) AS near_lat
        FROM {table}
        WHERE bbox.xmin BETWEEN {lng - delta} AND {lng + delta}
          AND bbox.ymin BETWEEN {lat - delta} AND {lat + delta}
          AND ST_Distance(geometry, ST_Point({lng}, {lat})) < {deg_thresh}
          AND subtype = 'road'
    """
    return db.sql(sql).fetchall()


def rail_near(db: duckdb.DuckDBPyConnection, lat: float, lng: float,
              radius_m: int = 1000, *, source: str | None = None) -> list[tuple]:
    """Find tram/train segments within radius. Returns (class, dist_m)."""
    table = f"read_parquet('{source or _local_or_fail(ROADS_FILE)}')"
    delta = radius_m / 111_000 * 1.5
    import math
    m_per_deg = 111_320 * math.cos(math.radians(lat))
    deg_thresh = radius_m / m_per_deg
    sql = f"""
        SELECT class,
               ST_Distance(geometry, ST_Point({lng}, {lat})) * {m_per_deg} AS dist_m
        FROM {table}
        WHERE bbox.xmin BETWEEN {lng - delta} AND {lng + delta}
          AND bbox.ymin BETWEEN {lat - delta} AND {lat + delta}
          AND ST_Distance(geometry, ST_Point({lng}, {lat})) < {deg_thresh}
          AND subtype = 'rail'
    """
    return db.sql(sql).fetchall()


def aadt_near(db: duckdb.DuckDBPyConnection, lat: float, lng: float,
              radius_m: int = 500) -> list[tuple]:
    """Find measured AADT segments within radius, across all state parquets.

    Reads every data/aadt_*.parquet (one per state) together. Returns
    (aadt, hv_pct, road_name, dist_m, nearest_lng, nearest_lat).
    """
    files = sorted(glob.glob(str(data_path(AADT_GLOB))))
    if not files:
        return []
    file_list = "[" + ", ".join(f"'{f}'" for f in files) + "]"
    table = f"read_parquet({file_list}, union_by_name=true)"
    delta = radius_m / 111_000 * 1.5
    import math
    m_per_deg = 111_320 * math.cos(math.radians(lat))
    deg_thresh = radius_m / m_per_deg
    sql = f"""
        SELECT aadt, hv_pct, road_name,
               ST_Distance(geometry, ST_Point({lng}, {lat})) * {m_per_deg} AS dist_m,
               ST_X(ST_ClosestPoint(geometry, ST_Point({lng}, {lat}))) AS near_lng,
               ST_Y(ST_ClosestPoint(geometry, ST_Point({lng}, {lat}))) AS near_lat
        FROM {table}
        WHERE xmin BETWEEN {lng - delta} AND {lng + delta}
          AND ymin BETWEEN {lat - delta} AND {lat + delta}
          AND ST_Distance(geometry, ST_Point({lng}, {lat})) < {deg_thresh}
        ORDER BY dist_m
    """
    return db.sql(sql).fetchall()


def nfdh_near(db: duckdb.DuckDBPyConnection, lat: float, lng: float,
              radius_m: int = 1000) -> list[tuple]:
    """Find NFDH national traffic counter stations within radius.

    Aggregates directional/lane counts into total AADT per station.
    Returns (aadt, hv_pct, road_name, dist_m, station_lng, station_lat).
    """
    nfdh_path = data_path(NFDH_FILE)
    if not nfdh_path.exists():
        return []
    import math
    m_per_deg = 111_320 * math.cos(math.radians(lat))
    delta = radius_m / 111_000 * 1.5
    sql = f"""
        WITH raw AS (
            SELECT station_id, road_name, lon, lat as slat,
                   aadt, heavy_vehicle_pct, direction
            FROM read_parquet('{nfdh_path}')
            WHERE lon BETWEEN {lng - delta} AND {lng + delta}
              AND lat BETWEEN {lat - delta} AND {lat + delta}
        ),
        agg AS (
            SELECT station_id, road_name, lon, slat,
                   COALESCE(
                       MAX(CASE WHEN direction IS NULL THEN aadt END),
                       SUM(CASE WHEN direction IS NOT NULL THEN aadt END)
                   ) AS total_aadt,
                   MAX(heavy_vehicle_pct) AS hv_pct
            FROM raw
            GROUP BY station_id, road_name, lon, slat
        )
        SELECT total_aadt, hv_pct, road_name,
               SQRT(POW((lon - {lng}) * {m_per_deg}, 2) +
                    POW((slat - {lat}) * 111320, 2)) AS dist_m,
               lon, slat
        FROM agg
        WHERE total_aadt IS NOT NULL
          AND SQRT(POW((lon - {lng}) * {m_per_deg}, 2) +
                   POW((slat - {lat}) * 111320, 2)) < {radius_m}
        ORDER BY dist_m
    """
    return db.sql(sql).fetchall()


def gtfs_rail_near(db: duckdb.DuckDBPyConnection, lat: float, lng: float,
                   radius_m: int = 1000) -> list[tuple]:
    """Find rail/tram routes near a point using GTFS shapes + frequencies.

    Checks national AU file first, falls back to PTV-only file.
    Returns (route_type, route_name, dist_m, peak_svc_per_hr, offpeak_svc_per_hr).
    route_type: 0=tram, 1=metro, 2=train.
    """
    au_shapes = data_path(AU_RAIL_SHAPES_FILE)
    au_freq = data_path(AU_RAIL_FREQ_FILE)
    if au_shapes.exists() and au_freq.exists():
        shapes_path, freq_path = au_shapes, au_freq
    else:
        shapes_path = data_path(PTV_SHAPES_FILE)
        freq_path = data_path(PTV_FREQ_FILE)
        if not shapes_path.exists() or not freq_path.exists():
            return []

    import math
    cos_lat = math.cos(math.radians(lat))
    M_PER_DEG = 111_320.0
    # Measure distance to the rail LINE (interpolated between vertices), not to
    # the nearest stored vertex. NSW rail shapes are decimated (median vertex
    # spacing ~244m, gaps up to ~6km), so a property right beside a line can sit
    # far from any vertex and be missed. Use a generous bbox so sparse-vertex
    # shapes whose nearest vertex is outside the radius are still captured,
    # rebuild each candidate shape's linestring (ordered by sequence), and take
    # ST_Distance to it. Longitudes are scaled by cos(lat) so the planar metric
    # is in metres on both axes.
    buf_m = radius_m + 3000
    dlat = buf_m / M_PER_DEG
    dlng = buf_m / (M_PER_DEG * cos_lat)
    qx = lng * cos_lat

    sql = f"""
        WITH cand AS (
            SELECT DISTINCT shape_id
            FROM read_parquet('{shapes_path}')
            WHERE lng BETWEEN {lng - dlng} AND {lng + dlng}
              AND lat BETWEEN {lat - dlat} AND {lat + dlat}
        ),
        lines AS (
            SELECT s.shape_id, ANY_VALUE(s.route_type) AS route_type,
                   ST_MakeLine(LIST(ST_Point(s.lng * {cos_lat}, s.lat)
                                    ORDER BY s.sequence)) AS geom
            FROM read_parquet('{shapes_path}') s
            JOIN cand c ON s.shape_id = c.shape_id
            GROUP BY s.shape_id
            HAVING COUNT(*) >= 2
        ),
        nearest AS (
            SELECT shape_id, route_type,
                   ST_Distance(geom, ST_Point({qx}, {lat})) * {M_PER_DEG} AS dist_m,
                   ST_X(ST_ClosestPoint(geom, ST_Point({qx}, {lat}))) / {cos_lat} AS near_lng,
                   ST_Y(ST_ClosestPoint(geom, ST_Point({qx}, {lat}))) AS near_lat
            FROM lines
        )
        SELECT n.route_type, f.route_name, n.dist_m,
               f.peak_services_per_hour, f.offpeak_services_per_hour,
               n.near_lng, n.near_lat
        FROM nearest n
        JOIN read_parquet('{freq_path}') f
          ON n.shape_id = f.shape_id
        WHERE n.dist_m < {radius_m}
        ORDER BY n.dist_m
    """
    return db.sql(sql).fetchall()


# Backward-compatible alias
ptv_rail_near = gtfs_rail_near


def water_near(db: duckdb.DuckDBPyConnection, lat: float, lng: float,
               radius_m: int = 5000) -> list[tuple]:
    """Find water features within radius.

    Returns (class, subtype, dist_m) sorted by distance.
    class: ocean, lake, river, reservoir, pond, stream, etc.
    """
    water_path = data_path(WATER_FILE)
    if not water_path.exists():
        return []
    import math
    m_per_deg = 111_320 * math.cos(math.radians(lat))
    delta = radius_m / 111_000 * 1.5
    deg_thresh = radius_m / m_per_deg
    # Use overlap logic (not BETWEEN) so large polygons (ocean, bay)
    # whose bbox spans the search area are included.
    sql = f"""
        SELECT class, subtype,
               ST_Distance(geometry, ST_Point({lng}, {lat})) * {m_per_deg} AS dist_m
        FROM read_parquet('{water_path}')
        WHERE bbox.xmin <= {lng + delta}
          AND bbox.xmax >= {lng - delta}
          AND bbox.ymin <= {lat + delta}
          AND bbox.ymax >= {lat - delta}
          AND ST_Distance(geometry, ST_Point({lng}, {lat})) < {deg_thresh}
        ORDER BY dist_m
    """
    try:
        return db.sql(sql).fetchall()
    except Exception:
        return []


def buildings_near(db: duckdb.DuckDBPyConnection, lat: float, lng: float,
                   radius_m: int = 500) -> list[tuple]:
    """Find buildings within radius. Returns (height, dist_m, num_floors)."""
    buildings_path = data_path("overture_buildings.parquet")
    if not buildings_path.exists():
        return []
    import math
    m_per_deg = 111_320 * math.cos(math.radians(lat))
    delta = radius_m / 111_000 * 1.5
    deg_thresh = radius_m / m_per_deg
    sql = f"""
        SELECT height,
               ST_Distance(geometry, ST_Point({lng}, {lat})) * {m_per_deg} AS dist_m,
               num_floors
        FROM read_parquet('{buildings_path}')
        WHERE bbox.xmin BETWEEN {lng - delta} AND {lng + delta}
          AND bbox.ymin BETWEEN {lat - delta} AND {lat + delta}
          AND ST_Distance(geometry, ST_Point({lng}, {lat})) < {deg_thresh}
        ORDER BY dist_m
    """
    return db.sql(sql).fetchall()


def pois_near(db: duckdb.DuckDBPyConnection, lat: float, lng: float,
              radius_m: int = 1500, *, source: str | None = None) -> list[tuple]:
    table = f"read_parquet('{source or _local_or_fail(POIS_FILE)}')"
    delta = radius_m / 111_000 * 1.5
    import math
    m_per_deg = 111_320 * math.cos(math.radians(lat))
    deg_thresh = radius_m / m_per_deg
    sql = f"""
        SELECT categories.primary AS category,
               ST_Distance(geometry, ST_Point({lng}, {lat})) * {m_per_deg} AS dist_m
        FROM {table}
        WHERE bbox.xmin BETWEEN {lng - delta} AND {lng + delta}
          AND bbox.ymin BETWEEN {lat - delta} AND {lat + delta}
          AND ST_Distance(geometry, ST_Point({lng}, {lat})) < {deg_thresh}
    """
    return db.sql(sql).fetchall()


def pois_near_detailed(db: duckdb.DuckDBPyConnection, lat: float, lng: float,
                       radius_m: int = 1500) -> list[tuple]:
    """Like pois_near but returns (category, dist_m, lng, lat, name)."""
    pois_path = data_path(POIS_FILE)
    if not pois_path.exists():
        return []
    delta = radius_m / 111_000 * 1.5
    import math
    m_per_deg = 111_320 * math.cos(math.radians(lat))
    deg_thresh = radius_m / m_per_deg
    sql = f"""
        SELECT categories.primary AS category,
               ST_Distance(geometry, ST_Point({lng}, {lat})) * {m_per_deg} AS dist_m,
               ST_X(geometry) AS poi_lng,
               ST_Y(geometry) AS poi_lat,
               names.primary AS name
        FROM read_parquet('{pois_path}')
        WHERE bbox.xmin BETWEEN {lng - delta} AND {lng + delta}
          AND bbox.ymin BETWEEN {lat - delta} AND {lat + delta}
          AND ST_Distance(geometry, ST_Point({lng}, {lat})) < {deg_thresh}
        ORDER BY dist_m
    """
    return db.sql(sql).fetchall()
