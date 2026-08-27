"""Optional local-cadastre attribution for point contamination evidence.

The parcel database is shared read-only with DA Leads in production. It is an
enhancement, never a prerequisite for a score: ``None`` means attribution was
unavailable and callers retain their conservative radius/density behaviour.

The address may snap up to 25 metres because geocoders commonly return a kerb
or road-centreline point. Evidence points never snap: a historical listing in
the neighbouring lot must not be pulled onto the target parcel.
"""
from __future__ import annotations

import json
import logging
import math
import os
from threading import Lock

logger = logging.getLogger(__name__)

PARCELS_DB = os.environ.get("PARCELS_DB", "/data/parcels/parcels.duckdb")
_TARGET_SNAP_M = 25.0

_conn = None
_conn_ino: int | None = None
_query_lock = Lock()


def _get_conn():
    """Return a cursor, reopening when an atomic DB replacement changes inode.

    The caller holds ``_query_lock`` for the cursor lifetime. That makes it
    safe to close an old base handle when a refresh swaps the 19 GB database.
    """
    global _conn, _conn_ino
    stat = os.stat(PARCELS_DB)
    if _conn is None or stat.st_ino != _conn_ino:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
        import duckdb

        base = duckdb.connect(PARCELS_DB, read_only=True)
        base.execute("LOAD spatial")
        _conn, _conn_ino = base, stat.st_ino
    return _conn.cursor()


def _rings(raw_geojson: str) -> list[list] | None:
    try:
        geometry = json.loads(raw_geojson)
    except (TypeError, json.JSONDecodeError):
        return None
    from property_scores.contamination.sources._common import polygon_rings
    return polygon_rings(geometry)


def _contains(rings: list[list], lat: float, lng: float) -> bool:
    from property_scores.contamination.score import _inside_polygon_rings
    return _inside_polygon_rings(lat, lng, rings)


def _distance_to_rings_m(rings: list[list], lat: float, lng: float) -> float:
    if _contains(rings, lat, lng):
        return 0.0
    from property_scores.contamination.score import _nearest_polygon_boundary
    return _nearest_polygon_boundary(lat, lng, rings)[0]


def same_parcel_flags(
    state: str,
    address_lat: float,
    address_lng: float,
    evidence_points: list[tuple[float, float]],
) -> list[bool] | None:
    """Return whether each evidence point is strictly inside the target lot.

    ``None`` means the cadastre database/target parcel was unavailable. A
    returned ``False`` is conclusive for this snapshot. The R-tree query reads
    every geometry slice in one small envelope; multiple slices with the same
    PFI are treated as one parcel.
    """
    if not evidence_points:
        return []
    values = [address_lat, address_lng,
              *(value for point in evidence_points for value in point)]
    if not all(isinstance(value, (int, float)) and math.isfinite(value)
               for value in values):
        return None
    if not (-90 <= address_lat <= 90 and -180 <= address_lng <= 180):
        return None
    if any(not (-90 <= lat <= 90 and -180 <= lng <= 180)
           for lat, lng in evidence_points):
        return None

    # A conservative WGS84 envelope around the address snap radius, enlarged
    # to include every evidence point. Exact candidate ordering and the 25 m
    # cutoff are computed below in local metres, never angular degrees.
    lat_delta = _TARGET_SNAP_M / 111_320.0
    cos_lat = max(abs(math.cos(math.radians(address_lat))), 1e-12)
    lng_delta = _TARGET_SNAP_M / (111_320.0 * cos_lat)
    west = min([address_lng - lng_delta, *(lng for _, lng in evidence_points)])
    east = max([address_lng + lng_delta, *(lng for _, lng in evidence_points)])
    south = min([address_lat - lat_delta, *(lat for lat, _ in evidence_points)])
    north = max([address_lat + lat_delta, *(lat for lat, _ in evidence_points)])

    try:
        with _query_lock:
            conn = _get_conn()
            rows = conn.execute(
                """
                SELECT pfi, ST_AsGeoJSON(geom)
                FROM parcels
                WHERE state = ?
                  AND ST_Intersects(geom, ST_MakeEnvelope(?, ?, ?, ?))
                """,
                [state.lower().strip(), west, south, east, north],
            ).fetchall()
        if not rows:
            return None

        geometries: list[tuple[str, list[list]]] = []
        for pfi, raw_geojson in rows:
            rings = _rings(raw_geojson)
            if rings is None:
                return None
            geometries.append((str(pfi), rings))

        # Overlapping cadastral records can legitimately yield more than one
        # containing PFI. Treat all strict hits as the target rather than
        # selecting an arbitrary LIMIT 1 row.
        target_pfis = {
            pfi for pfi, rings in geometries
            if _contains(rings, address_lat, address_lng)
        }
        if not target_pfis:
            nearest_by_pfi: dict[str, float] = {}
            for pfi, rings in geometries:
                distance = _distance_to_rings_m(
                    rings, address_lat, address_lng)
                nearest_by_pfi[pfi] = min(
                    distance, nearest_by_pfi.get(pfi, float("inf")))
            nearest_pfi, nearest_m = min(
                nearest_by_pfi.items(), key=lambda item: (item[1], item[0]))
            if nearest_m > _TARGET_SNAP_M:
                return None
            target_pfis = {nearest_pfi}

        return [
            any(pfi in target_pfis and _contains(rings, lat, lng)
                for pfi, rings in geometries)
            for lat, lng in evidence_points
        ]
    except Exception:
        logger.warning("parcel attribution unavailable; using radius fallback",
                       exc_info=True)
        return None


def close() -> None:
    """Release the shared handle (mainly for tests and graceful shutdown)."""
    global _conn, _conn_ino
    with _query_lock:
        if _conn is not None:
            _conn.close()
            _conn = None
            _conn_ino = None
