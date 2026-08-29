"""Overture Maps data loading helpers via DuckDB."""

import glob
import os

import duckdb

from property_scores.common.config import data_path

import threading

ROADS_FILE = "overture_roads.parquet"
POIS_FILE = "overture_pois.parquet"
WATER_FILE = "overture_water.parquet"
# Measured per-segment AADT ground truth, one parquet per state
# (aadt_vic.parquet, aadt_nsw.parquet, ...). Each file shares the schema
# aadt_near() reads: (aadt, hv_pct, road_name, geometry, xmin, ymin). That is the
# on-disk COLUMN layout, not aadt_near()'s return tuple — the return also carries
# the resolved publisher, see below. Drop a new state's parquet in and it is
# picked up automatically, but register its licensor too (AADT_SOURCE_BY_STATE).
AADT_GLOB = "aadt_*.parquet"
NFDH_FILE = "nfdh_aadt_national.parquet"

# Which authority actually publishes each state's measured-AADT file. The label
# has to travel with the row: aadt_near() reads every aadt_*.parquet together,
# so nothing downstream can infer the publisher from the call site.
#
# Defect found 2026-08-05: noise/score.py stamped "vicroads" on every row the
# glob returned, so a Transport for NSW counter on the Pacific Highway was
# published as VicRoads data. That is not only a wrong label — it selects the
# wrong licensor in scripts/export_noise_grid_csv.py's attribution block, i.e.
# we were crediting Victoria for four other states' CC-BY data.
AADT_SOURCE_BY_STATE = {
    "vic": "vicroads",   # DataVic / Dept of Transport and Planning, Traffic Volume
    "nsw": "tfnsw",      # Transport for NSW, Roads Traffic Volume Counts
    "qld": "qld_tmr",    # QLD Dept of Transport and Main Roads, road location & traffic
    "sa": "sa_dit",      # SA Dept for Infrastructure and Transport, Traffic Volumes
    "wa": "mrwa",        # Main Roads WA, Traffic Digest
}


def aadt_source_for_file(path) -> str:
    """Map a data/aadt_<state>.parquet path to its publishing authority's slug.

    An unregistered state yields "aadt_<state>" rather than a guess. That label
    is deliberately not in export_noise_grid_csv.py's _AADT_LICENSOR, so adding
    a state's parquet without registering its licensor makes the attribution
    block refuse to ship instead of silently crediting somebody else.
    """
    stem = os.path.basename(str(path))
    if stem.startswith("aadt_") and stem.endswith(".parquet"):
        state = stem[len("aadt_"):-len(".parquet")].lower()
        return AADT_SOURCE_BY_STATE.get(state, f"aadt_{state}")
    return f"aadt_{stem}"
PTV_SHAPES_FILE = "ptv_rail_shapes.parquet"
PTV_FREQ_FILE = "ptv_rail_frequency.parquet"
AU_RAIL_SHAPES_FILE = "au_rail_shapes.parquet"
AU_RAIL_FREQ_FILE = "au_rail_frequency.parquet"

_install_lock = threading.Lock()
_base_db = None

# Metres per degree of latitude. Longitude degrees are shorter by cos(latitude)
# and must be scaled separately -- see _metres_from_closest_point.
_M_PER_DEG_LAT = 111_320.0


def _closest_point_sql(geom: str, lng: float, lat: float) -> str:
    """The point on `geom` nearest the query point, computed once per row."""
    return f"ST_ClosestPoint({geom}, ST_Point({lng}, {lat}))"


def _metres_from_closest_point(lng: float, lat: float, m_per_deg: float,
                               cp: str = "cp") -> str:
    """SQL for ground metres between the query point and a closest-point column.

    ``ST_Distance`` returns DEGREES, and a degree of longitude is only cos(lat)
    as long as a degree of latitude -- 88.0 km against 111.3 km at Melbourne,
    81.6 km at Hobart. Scaling a mixed-axis degree distance by one factor
    therefore understates any north-south offset by cos(lat) and lets a radius
    over-reach north-south by 1/cos(lat). Measured before this was fixed: noise
    sources up to 21% too close (Clayton's Centre Rd reported at 376 m when it
    is 474 m), 27 of 302 returned rows actually outside the stated 500 m, and
    7.2% of Melbourne walkability POIs outside the 1500 m radius against 0.3%
    in Darwin -- a latitude-dependent bias that made scores incomparable
    between cities.

    Project each axis, then combine. ``nfdh_near`` and ``gtfs_rail_near``
    already did this correctly and are the pattern this follows.
    """
    return (f"SQRT(POW((ST_X({cp}) - {lng}) * {m_per_deg}, 2) + "
            f"POW((ST_Y({cp}) - {lat}) * {_M_PER_DEG_LAT}, 2))")


# --- legacy_distance: why a known-wrong formula is kept on purpose -----------
#
# Production noise does not use the physics Lden. `NOISE_TRANSFER=1` (set in the
# systemd unit, NOT in .env) swaps in an EU(NL+UK) random forest plus a per-state
# affine, and that forest was FITTED on features computed with the pre-2026-07
# distance formula. For a fitted model an input formula is a feature definition,
# not a correctness question -- so "fixing" it feeds the forest out-of-
# distribution inputs and puts the physics anchor and the RF features on
# different rulers, which flips `noise_score`'s threshold gates and swings the
# blended score by up to 39 points in BOTH directions.
#
# Retraining on corrected geometry WAS built and measured (2026-07-26,
# scripts/poc_eu_transfer6_geodist.py, 96 s to regenerate all features). Across
# 4 variants x 5 seeds it made the production path slightly WORSE, consistently
# and at ~8x the seed noise: gate MAE 3.798 -> 3.852, r 0.696 -> 0.687. The
# uncalibrated transfer improved (raw bias -5.18 -> -3.95 dB) but the per-state
# affine already absorbs that, because within a city cos(lat) is near-constant
# and an affine is exactly what absorbs a per-city constant.
#
# So noise stays on the old ruler deliberately, and everything else -- flood,
# bushfire, walkability, heat island, view quality, contamination -- gets true
# metres. `legacy_distance=True` is passed ONLY from property_scores/noise/, and
# it exists so that dependency is explicit in code instead of implicit in a
# formula nobody may touch. Delete the parameter if the RF is ever retrained.
#
# Evidence: limon-ops logs/da-leads/2026-07-26_noise-model-handoff.md
_LEGACY_DISTANCE_DOC = """legacy_distance: reproduce the pre-2026-07 formula
        (scale a mixed-axis degree distance by the longitude factor, and let the
        degree prefilter be the only radius test). Wrong on the ground, but it is
        the ruler the noise transfer RF was fitted on -- see the module comment
        above. Noise callers pass True; every other caller must not."""


def _legacy_dist_sql(lng: float, lat: float, m_per_deg: float,
                     geom: str = "geometry") -> str:
    """The pre-2026-07 distance expression, kept verbatim for the noise model."""
    return f"ST_Distance({geom}, ST_Point({lng}, {lat})) * {m_per_deg}"


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
               radius_m: int = 1000, *, source: str | None = None,
               legacy_distance: bool = False) -> list[tuple]:
    f"""Road segments within radius. Returns (class, dist_m, speed_kmh, near_lng, near_lat).

    {_LEGACY_DISTANCE_DOC}
    """
    table = f"read_parquet('{source or _local_or_fail(ROADS_FILE)}')"
    delta = radius_m / 111_000 * 1.5
    import math
    m_per_deg = 111_320 * math.cos(math.radians(lat))
    deg_thresh = radius_m / m_per_deg
    speed_expr = """CASE WHEN speed_limits IS NOT NULL AND len(speed_limits) > 0
                         THEN speed_limits[1].max_speed.value
                         ELSE NULL END"""
    # Shared prefilter: identical in both modes, so they can never drift apart.
    where = f"""bbox.xmin BETWEEN {lng - delta} AND {lng + delta}
                  AND bbox.ymin BETWEEN {lat - delta} AND {lat + delta}
                  AND ST_Distance(geometry, ST_Point({lng}, {lat})) < {deg_thresh}
                  AND subtype = 'road'"""
    if legacy_distance:
        sql = f"""
            SELECT class,
                   {_legacy_dist_sql(lng, lat, m_per_deg)} AS dist_m,
                   {speed_expr} AS speed_kmh,
                   ST_X({_closest_point_sql('geometry', lng, lat)}) AS near_lng,
                   ST_Y({_closest_point_sql('geometry', lng, lat)}) AS near_lat
            FROM {table}
            WHERE {where}
        """
        return db.sql(sql).fetchall()
    # The degree threshold is a SUPERSET of radius_m metres (it over-reaches
    # north-south by 1/cos(lat)), so it stays as the cheap indexed prefilter and
    # the exact metric filter runs over the survivors.
    sql = f"""
        SELECT class, dist_m, speed_kmh, ST_X(cp) AS near_lng, ST_Y(cp) AS near_lat
        FROM (
            SELECT class, cp,
                   {_metres_from_closest_point(lng, lat, m_per_deg)} AS dist_m,
                   speed_kmh
            FROM (
                SELECT class, {_closest_point_sql('geometry', lng, lat)} AS cp,
                       {speed_expr} AS speed_kmh
                FROM {table}
                WHERE {where}
            )
        )
        WHERE dist_m < {radius_m}
    """
    return db.sql(sql).fetchall()


def road_crossings(db: duckdb.DuckDBPyConnection, lat: float, lng: float,
                   targets: list[tuple], radius_m: int = 2000,
                   *, source: str | None = None) -> set | None:
    """Return target keys whose straight path crosses a motorway/trunk line.

    ``roads_near`` only supplies the point on each road nearest the property.
    Comparing that point's bearing with a POI bearing produced broad angular
    false positives (most visibly around Sydney's Bay Run).  This query tests
    the actual property-to-target segment against the actual road geometry.
    """
    if not targets:
        return set()
    table = f"read_parquet('{source or _local_or_fail(ROADS_FILE)}')"
    delta = radius_m / 111_000 * 1.5
    values = ", ".join(
        f"({i}, {tlng}, {tlat})" for i, (_key, tlng, tlat) in enumerate(targets))
    sql = f"""
        SELECT DISTINCT t.idx
        FROM (VALUES {values}) AS t(idx, tlng, tlat)
        JOIN {table} r
          ON ST_Intersects(r.geometry,
                           ST_MakeLine(ST_Point({lng}, {lat}),
                                       ST_Point(t.tlng, t.tlat)))
        WHERE r.bbox.xmin <= {lng + delta} AND r.bbox.xmax >= {lng - delta}
          AND r.bbox.ymin <= {lat + delta} AND r.bbox.ymax >= {lat - delta}
          AND r.subtype = 'road'
          AND r.class IN ('motorway', 'trunk')
    """
    try:
        hit = {row[0] for row in db.sql(sql).fetchall()}
    except Exception:
        return None
    return {targets[i][0] for i in hit}


def walking_trails_near(db: duckdb.DuckDBPyConnection, lat: float, lng: float,
                        radius_m: int = 1500, *, source: str | None = None) -> list[tuple] | None:
    """Named walking/cycling trail lines from Overture transportation.

    Places encode a trail as one representative point, which can be more than
    a kilometre from an address lying directly beside another part of the same
    trail.  Transportation retains the line geometry.  Only named path,
    footway and cycleway segments are accepted so ordinary unnamed sidewalks
    do not become recreational trails.  Returns the standard detailed-POI
    tuple with the nearest point on each line.
    """
    try:
        table = f"read_parquet('{source or _local_or_fail(ROADS_FILE)}')"
    except FileNotFoundError:
        return None
    delta = radius_m / 111_000 * 1.5
    import math
    m_per_deg = 111_320 * math.cos(math.radians(lat))
    deg_thresh = radius_m / m_per_deg
    sql = f"""
        SELECT 'hiking_trail' AS category, dist_m,
               ST_X(cp) AS lng, ST_Y(cp) AS lat, name
        FROM (
            SELECT name, cp,
                   {_metres_from_closest_point(lng, lat, m_per_deg)} AS dist_m
            FROM (
                SELECT names.primary AS name,
                       {_closest_point_sql('geometry', lng, lat)} AS cp
                FROM {table}
                WHERE bbox.xmin BETWEEN {lng - delta} AND {lng + delta}
                  AND bbox.ymin BETWEEN {lat - delta} AND {lat + delta}
                  AND ST_Distance(geometry, ST_Point({lng}, {lat})) < {deg_thresh}
                  AND class IN ('path', 'footway', 'cycleway')
                  AND names.primary IS NOT NULL
            )
        )
        WHERE dist_m < {radius_m}
        ORDER BY dist_m
    """
    try:
        return db.sql(sql).fetchall()
    except Exception:
        return None


def rail_near(db: duckdb.DuckDBPyConnection, lat: float, lng: float,
              radius_m: int = 1000, *, source: str | None = None,
              legacy_distance: bool = False) -> list[tuple]:
    f"""Find tram/train segments within radius. Returns (class, dist_m).

    {_LEGACY_DISTANCE_DOC}
    """
    table = f"read_parquet('{source or _local_or_fail(ROADS_FILE)}')"
    delta = radius_m / 111_000 * 1.5
    import math
    m_per_deg = 111_320 * math.cos(math.radians(lat))
    deg_thresh = radius_m / m_per_deg
    where = f"""bbox.xmin BETWEEN {lng - delta} AND {lng + delta}
                  AND bbox.ymin BETWEEN {lat - delta} AND {lat + delta}
                  AND ST_Distance(geometry, ST_Point({lng}, {lat})) < {deg_thresh}
                  AND subtype = 'rail'"""
    if legacy_distance:
        sql = f"""
            SELECT class, {_legacy_dist_sql(lng, lat, m_per_deg)} AS dist_m
            FROM {table}
            WHERE {where}
        """
        return db.sql(sql).fetchall()
    sql = f"""
        SELECT class, dist_m FROM (
            SELECT class, {_metres_from_closest_point(lng, lat, m_per_deg)} AS dist_m
            FROM (
                SELECT class, {_closest_point_sql('geometry', lng, lat)} AS cp
                FROM {table}
                WHERE {where}
            )
        )
        WHERE dist_m < {radius_m}
    """
    return db.sql(sql).fetchall()


def aadt_near(db: duckdb.DuckDBPyConnection, lat: float, lng: float,
              radius_m: int = 500, *, legacy_distance: bool = False) -> list[tuple]:
    f"""Find measured AADT segments within radius, across all state parquets.

    Reads every data/aadt_*.parquet (one per state) together. Returns
    (aadt, hv_pct, road_name, dist_m, nearest_lng, nearest_lat, source).

    `source` is the publishing authority's slug for the file the row came from
    (see AADT_SOURCE_BY_STATE). It is read from DuckDB's `filename` column
    rather than assumed, because the glob mixes every state into one result set
    and the caller has no other way to tell a VicRoads segment from a TfNSW one.

    {_LEGACY_DISTANCE_DOC}
    """
    files = sorted(glob.glob(str(data_path(AADT_GLOB))))
    if not files:
        return []
    file_list = "[" + ", ".join(f"'{f}'" for f in files) + "]"
    table = f"read_parquet({file_list}, union_by_name=true, filename=true)"
    delta = radius_m / 111_000 * 1.5
    import math
    m_per_deg = 111_320 * math.cos(math.radians(lat))
    deg_thresh = radius_m / m_per_deg
    where = f"""xmin BETWEEN {lng - delta} AND {lng + delta}
                  AND ymin BETWEEN {lat - delta} AND {lat + delta}
                  AND ST_Distance(geometry, ST_Point({lng}, {lat})) < {deg_thresh}"""
    if legacy_distance:
        sql = f"""
            SELECT aadt, hv_pct, road_name,
                   {_legacy_dist_sql(lng, lat, m_per_deg)} AS dist_m,
                   ST_X({_closest_point_sql('geometry', lng, lat)}) AS near_lng,
                   ST_Y({_closest_point_sql('geometry', lng, lat)}) AS near_lat,
                   filename AS src_file
            FROM {table}
            WHERE {where}
            ORDER BY dist_m
        """
        rows = db.sql(sql).fetchall()
    else:
        sql = f"""
            SELECT aadt, hv_pct, road_name, dist_m,
                   ST_X(cp) AS near_lng, ST_Y(cp) AS near_lat, src_file
            FROM (
                SELECT aadt, hv_pct, road_name, cp, src_file,
                       {_metres_from_closest_point(lng, lat, m_per_deg)} AS dist_m
                FROM (
                    SELECT aadt, hv_pct, road_name, filename AS src_file,
                           {_closest_point_sql('geometry', lng, lat)} AS cp
                    FROM {table}
                    WHERE {where}
                )
            )
            WHERE dist_m < {radius_m}
            ORDER BY dist_m
        """
        rows = db.sql(sql).fetchall()
    # Resolve the file path to its publisher once per row, so callers get a
    # label they can hand straight to an attribution block.
    return [r[:6] + (aadt_source_for_file(r[6]),) for r in rows]


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
               radius_m: int = 5000, *, legacy_distance: bool = False) -> list[tuple]:
    f"""Find water features within radius.

    Returns (class, subtype, dist_m) sorted by distance.
    class: ocean, lake, river, reservoir, pond, stream, etc.

    {_LEGACY_DISTANCE_DOC}
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
    where = f"""bbox.xmin <= {lng + delta}
                  AND bbox.xmax >= {lng - delta}
                  AND bbox.ymin <= {lat + delta}
                  AND bbox.ymax >= {lat - delta}
                  AND ST_Distance(geometry, ST_Point({lng}, {lat})) < {deg_thresh}"""
    if legacy_distance:
        sql = f"""
            SELECT class, subtype,
                   {_legacy_dist_sql(lng, lat, m_per_deg)} AS dist_m
            FROM read_parquet('{water_path}')
            WHERE {where}
            ORDER BY dist_m
        """
    else:
        sql = f"""
            SELECT class, subtype, dist_m FROM (
                SELECT class, subtype,
                       {_metres_from_closest_point(lng, lat, m_per_deg)} AS dist_m
                FROM (
                    SELECT class, subtype,
                           {_closest_point_sql('geometry', lng, lat)} AS cp
                    FROM read_parquet('{water_path}')
                    WHERE {where}
                )
            )
            WHERE dist_m < {radius_m}
            ORDER BY dist_m
        """
    try:
        return db.sql(sql).fetchall()
    except Exception:
        return []


def building_footprint_m2(db: duckdb.DuckDBPyConnection, lat: float,
                          lng: float) -> float | None:
    """Footprint area (m²) of the building CONTAINING the point, else None.

    Whole-building semantics: for strata/apartments this is the tower's
    footprint, not a unit's share — callers must label it as such. G-NAF
    points for detached houses often sit in the yard, so a containment miss
    falls back to the nearest footprint within 30 m (still the same parcel's
    dwelling at that range); beyond that returns None rather than guessing.
    """
    buildings_path = data_path("overture_buildings.parquet")
    if not buildings_path.exists():
        return None
    import math
    m_per_deg = 111_320 * math.cos(math.radians(lat))
    sql = f"""
        SELECT ST_Area(geometry)
        FROM read_parquet('{buildings_path}')
        WHERE bbox.xmin <= {lng} AND bbox.xmax >= {lng}
          AND bbox.ymin <= {lat} AND bbox.ymax >= {lat}
          AND ST_Contains(geometry, ST_Point({lng}, {lat}))
        LIMIT 1
    """
    try:
        row = db.sql(sql).fetchone()
    except Exception:
        return None
    if row and row[0] is not None:
        return float(row[0]) * m_per_deg * 111_320

    fallback_m = 30.0
    delta_lat = fallback_m / 111_320
    delta_lng = fallback_m / m_per_deg
    sql = f"""
        SELECT area, dist_m FROM (
            SELECT area, {_metres_from_closest_point(lng, lat, m_per_deg)} AS dist_m
            FROM (
                SELECT ST_Area(geometry) AS area,
                       {_closest_point_sql('geometry', lng, lat)} AS cp
                FROM read_parquet('{buildings_path}')
                WHERE bbox.xmin <= {lng + delta_lng} AND bbox.xmax >= {lng - delta_lng}
                  AND bbox.ymin <= {lat + delta_lat} AND bbox.ymax >= {lat - delta_lat}
            )
        )
        ORDER BY dist_m
        LIMIT 1
    """
    try:
        row = db.sql(sql).fetchone()
    except Exception:
        return None
    if not row or row[0] is None or row[1] is None or row[1] > fallback_m:
        return None
    return float(row[0]) * m_per_deg * 111_320


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
        SELECT height, dist_m, num_floors FROM (
            SELECT height, num_floors,
                   {_metres_from_closest_point(lng, lat, m_per_deg)} AS dist_m
            FROM (
                SELECT height, num_floors,
                       {_closest_point_sql('geometry', lng, lat)} AS cp
                FROM read_parquet('{buildings_path}')
                WHERE bbox.xmin BETWEEN {lng - delta} AND {lng + delta}
                  AND bbox.ymin BETWEEN {lat - delta} AND {lat + delta}
                  AND ST_Distance(geometry, ST_Point({lng}, {lat})) < {deg_thresh}
            )
        )
        WHERE dist_m < {radius_m}
        ORDER BY dist_m
    """
    return db.sql(sql).fetchall()


def pois_near(db: duckdb.DuckDBPyConnection, lat: float, lng: float,
              radius_m: int = 1500, *, source: str | None = None,
              legacy_distance: bool = False) -> list[tuple]:
    f"""POIs within radius. Returns (category, dist_m).

    {_LEGACY_DISTANCE_DOC}
    """
    table = f"read_parquet('{source or _local_or_fail(POIS_FILE)}')"
    delta = radius_m / 111_000 * 1.5
    import math
    m_per_deg = 111_320 * math.cos(math.radians(lat))
    deg_thresh = radius_m / m_per_deg
    where = f"""bbox.xmin BETWEEN {lng - delta} AND {lng + delta}
                  AND bbox.ymin BETWEEN {lat - delta} AND {lat + delta}
                  AND ST_Distance(geometry, ST_Point({lng}, {lat})) < {deg_thresh}"""
    if legacy_distance:
        sql = f"""
            SELECT categories.primary AS category,
                   {_legacy_dist_sql(lng, lat, m_per_deg)} AS dist_m
            FROM {table}
            WHERE {where}
        """
        return db.sql(sql).fetchall()
    sql = f"""
        SELECT category, dist_m FROM (
            SELECT category, {_metres_from_closest_point(lng, lat, m_per_deg)} AS dist_m
            FROM (
                SELECT categories.primary AS category,
                       {_closest_point_sql('geometry', lng, lat)} AS cp
                FROM {table}
                WHERE {where}
            )
        )
        WHERE dist_m < {radius_m}
    """
    return db.sql(sql).fetchall()


def pois_near_detailed(db: duckdb.DuckDBPyConnection, lat: float, lng: float,
                       radius_m: int = 1500) -> list[tuple]:
    """Like pois_near but returns (category, dist_m, lng, lat, name)."""
    pois_path = data_path(POIS_FILE)
    if not pois_path.exists():
        raise FileNotFoundError(
            f"Required Overture places artifact missing: {pois_path}")
    delta = radius_m / 111_000 * 1.5
    import math
    m_per_deg = 111_320 * math.cos(math.radians(lat))
    deg_thresh = radius_m / m_per_deg
    sql = f"""
        SELECT category, dist_m, poi_lng, poi_lat, name FROM (
            SELECT category, poi_lng, poi_lat, name,
                   {_metres_from_closest_point(lng, lat, m_per_deg)} AS dist_m
            FROM (
                SELECT CASE
                         WHEN categories.primary = 'school'
                          AND len(list_filter(
                                websites,
                                url -> contains(lower(url), 'primary'))) > 0
                         THEN 'primary_school'
                         ELSE categories.primary
                       END AS category,
                       ST_X(geometry) AS poi_lng,
                       ST_Y(geometry) AS poi_lat,
                       names.primary AS name,
                       {_closest_point_sql('geometry', lng, lat)} AS cp
                FROM read_parquet('{pois_path}')
                WHERE bbox.xmin BETWEEN {lng - delta} AND {lng + delta}
                  AND bbox.ymin BETWEEN {lat - delta} AND {lat + delta}
                  AND ST_Distance(geometry, ST_Point({lng}, {lat})) < {deg_thresh}
            )
        )
        WHERE dist_m < {radius_m}
        ORDER BY dist_m
    """
    return db.sql(sql).fetchall()


BUS_STOPS_FILE = "au_bus_stops.parquet"
def water_crossings(db: duckdb.DuckDBPyConnection, lat: float, lng: float,
                    targets: list[tuple], radius_m: int = 2000) -> set | None:
    """Which targets require crossing a major water body from (lat, lng).

    targets: [(key, t_lng, t_lat), ...]. Returns the set of keys whose
    straight property->target segment intersects a river/canal/lake/ocean/
    bay/lagoon polygon bigger than ~2 ha. Stockton was credited with
    Newcastle CBD cafes across the Hunter River (22/25, 2026-06-11 audit);
    a ferry is not a walk. Small ponds/pools are excluded by class+area so a
    footbridge over a creek is not penalised.
    """
    water_path = data_path(WATER_FILE)
    import os
    if not targets:
        return set()
    if not os.path.exists(str(water_path)):
        return None
    import math
    delta = radius_m / 111_000 * 1.5
    values = ", ".join(
        f"({i}, {tlng}, {tlat})" for i, (_k, tlng, tlat) in enumerate(targets))
    sql = f"""
        SELECT DISTINCT t.idx
        FROM (VALUES {values}) AS t(idx, tlng, tlat)
        JOIN read_parquet('{water_path}') w
          ON ST_Intersects(w.geometry,
                           ST_MakeLine(ST_Point({lng}, {lat}),
                                       ST_Point(t.tlng, t.tlat)))
        WHERE w.bbox.xmin <= {lng + delta} AND w.bbox.xmax >= {lng - delta}
          AND w.bbox.ymin <= {lat + delta} AND w.bbox.ymax >= {lat - delta}
          AND w.class IN ('river', 'canal', 'lake', 'ocean', 'sea', 'bay', 'lagoon')
          AND ST_Area(w.geometry) > 0.0000020
          -- A waterfront probe can fall a few metres inside a coarse water
          -- polygon.  Such a polygon is the origin/shoreline, not a body that
          -- must be crossed to reach every amenity in every direction.
          AND NOT ST_Intersects(w.geometry, ST_Point({lng}, {lat}))
          AND NOT ST_Intersects(w.geometry, ST_Point(t.tlng, t.tlat))
    """
    try:
        hit = {row[0] for row in db.sql(sql).fetchall()}
    except Exception:
        # Empty means the query completed and no crossing was found. None is
        # deliberately different: callers must disclose that the check could
        # not answer rather than turning an outage into "no water barrier".
        return None
    return {targets[i][0] for i in hit}


AMENITIES_FILE = "au_osm_amenities.parquet"


def osm_amenities_near(db: duckdb.DuckDBPyConnection, lat: float, lng: float,
                       radius_m: int = 1500, *, source: str | None = None) -> list[tuple] | None:
    """OSM public amenities near a point: same 5-tuple shape as the POI stream.

    playground / dog_park / swimming_pool / beach from OSM polygons+nodes
    (scripts/build_osm_amenities.py); Overture's commercial POI recall on
    public infrastructure is 26-44% holes (2026-06-11 audit, Brunswick Baths
    front door read "not found within 1.5km"). Categories already map via
    CATEGORY_MAP. Returns [] when the parquet is absent.
    """
    path = source or data_path(AMENITIES_FILE)
    import os
    if not os.path.exists(str(path)):
        return None
    import math
    m_per_deg = 111_320 * math.cos(math.radians(lat))
    dlat = radius_m / 111_320
    dlng = radius_m / m_per_deg
    sql = f"""
        SELECT category, dist_m, lng, lat, name
        FROM (
            SELECT category, name, lng, lat,
                   sqrt(pow((lng - {lng}) * {m_per_deg}, 2)
                        + pow((lat - {lat}) * 111320, 2)) AS dist_m
            FROM read_parquet('{path}')
            WHERE lat BETWEEN {lat - dlat} AND {lat + dlat}
              AND lng BETWEEN {lng - dlng} AND {lng + dlng}
        )
        WHERE dist_m < {radius_m}
        ORDER BY dist_m
    """
    try:
        return db.sql(sql).fetchall()
    except Exception:
        return None


RAIL_STOPS_FILE = "au_rail_stops.parquet"


def rail_stops_near(db: duckdb.DuckDBPyConnection, lat: float, lng: float,
                    radius_m: int = 1500, *, source: str | None = None) -> list[tuple] | None:
    """GTFS rail/metro/tram stations near a point: same 5-tuple shape.

    GTFS only contains stops with CURRENT service, so this kills both failure
    modes of the Overture train POIs at once (2026-06-11 audit): Perth's
    Morley-Ellenbrook line (opened 2024) was missing, while Newcastle
    stations closed in 2014 still served as "nearest train". category is
    "train_station" so rows merge straight into the POI stream.
    """
    path = source or data_path(RAIL_STOPS_FILE)
    import os
    if not os.path.exists(str(path)):
        return None
    import math
    m_per_deg = 111_320 * math.cos(math.radians(lat))
    dlat = radius_m / 111_320
    dlng = radius_m / m_per_deg
    sql = f"""
        SELECT 'train_station' AS category, dist_m, lng, lat, stop_name AS name
        FROM (
            SELECT stop_name, lng, lat,
                   sqrt(pow((lng - {lng}) * {m_per_deg}, 2)
                        + pow((lat - {lat}) * 111320, 2)) AS dist_m
            FROM read_parquet('{path}')
            WHERE lat BETWEEN {lat - dlat} AND {lat + dlat}
              AND lng BETWEEN {lng - dlng} AND {lng + dlng}
              AND lower(stop_name) NOT LIKE '%rail replacement bus%'
              AND lower(stop_name) NOT LIKE '%bus station%'
              AND lower(stop_name) NOT LIKE '%tram stop%'
              AND stop_name NOT LIKE '%/%'
              AND (lower(stop_name) LIKE '%railway station%'
                   OR lower(stop_name) LIKE '%train station%'
                   OR lower(stop_name) LIKE '%metro station%'
                   OR lower(stop_name) LIKE '% station')
        )
        WHERE dist_m < {radius_m}
        ORDER BY dist_m
    """
    try:
        return db.sql(sql).fetchall()
    except Exception:
        return None


SPORTS_FIELDS_FILE = "au_sports_fields.parquet"


def sports_fields_near(db: duckdb.DuckDBPyConnection, lat: float, lng: float,
                       radius_m: int = 1500, *, source: str | None = None) -> list[tuple] | None:
    """OSM leisure-polygon sports grounds near a point: same 5-tuple shape.

    Council ovals are OSM leisure polygons, not commercial POIs, so Overture
    places miss most of them. Centroids from au_sports_fields.parquet
    (scripts/build_sports_fields.py) join the POI stream under the
    "sports_and_recreation_venue" category (maps to the sports scenario).
    Returns [] when the parquet is absent.
    """
    path = source or data_path(SPORTS_FIELDS_FILE)
    import os
    if not os.path.exists(str(path)):
        return None
    import math
    m_per_deg = 111_320 * math.cos(math.radians(lat))
    dlat = radius_m / 111_320
    dlng = radius_m / m_per_deg
    sql = f"""
        SELECT 'sports_and_recreation_venue' AS category, dist_m, lng, lat, name
        FROM (
            SELECT name, lng, lat,
                   sqrt(pow((lng - {lng}) * {m_per_deg}, 2)
                        + pow((lat - {lat}) * 111320, 2)) AS dist_m
            FROM read_parquet('{path}')
            WHERE lat BETWEEN {lat - dlat} AND {lat + dlat}
              AND lng BETWEEN {lng - dlng} AND {lng + dlng}
        )
        WHERE dist_m < {radius_m}
        ORDER BY dist_m
    """
    try:
        return db.sql(sql).fetchall()
    except Exception:
        return None


def transit_stops_near(db: duckdb.DuckDBPyConnection, lat: float, lng: float,
                       radius_m: int = 1500, *, source: str | None = None) -> list[tuple] | None:
    """GTFS bus/tram stops near a point: (category, dist_m, lng, lat, name).

    Overture places have essentially no Australian bus stops (2026-06-10:
    zero within 1500 m of Turramurra station's bus interchange), so the
    walkability tram_bus scenario reads official GTFS stops from
    au_bus_stops.parquet (scripts/build_bus_stops.py). category is
    "bus_stop" / "tram_stop" so rows merge straight into the POI stream.
    Returns [] when the parquet is absent (graceful pre-data deploys).
    """
    path = source or data_path(BUS_STOPS_FILE)
    import os
    if not os.path.exists(str(path)):
        return None
    import math
    m_per_deg = 111_320 * math.cos(math.radians(lat))
    dlat = radius_m / 111_320
    dlng = radius_m / m_per_deg
    sql = f"""
        SELECT mode || '_stop' AS category, dist_m, lng, lat, stop_name AS name
        FROM (
            SELECT mode, stop_name, lng, lat,
                   sqrt(pow((lng - {lng}) * {m_per_deg}, 2)
                        + pow((lat - {lat}) * 111320, 2)) AS dist_m
            FROM read_parquet('{path}')
            WHERE lat BETWEEN {lat - dlat} AND {lat + dlat}
              AND lng BETWEEN {lng - dlng} AND {lng + dlng}
        )
        WHERE dist_m < {radius_m}
        ORDER BY dist_m
    """
    try:
        return db.sql(sql).fetchall()
    except Exception:
        return None
