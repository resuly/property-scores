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
import time
from threading import Event, Lock, Thread, Timer

logger = logging.getLogger(__name__)

PARCELS_DB = os.environ.get("PARCELS_DB", "/data/parcels/parcels.duckdb")
_TARGET_SNAP_M = 25.0

_conn = None
_conn_ino: int | None = None
_query_lock = Lock()
_refresh_lock = Lock()
_refreshing_ino: int | None = None


def _open_base():
    stat = os.stat(PARCELS_DB)
    import duckdb

    base = duckdb.connect(PARCELS_DB, read_only=True)
    base.execute("LOAD spatial")
    return base, stat.st_ino


def _refresh_worker(expected_ino: int) -> None:
    """Open a replacement off-request, then atomically install it."""
    global _conn, _conn_ino, _refreshing_ino
    replacement = None
    try:
        replacement, opened_ino = _open_base()
        if opened_ino != expected_ino or os.stat(PARCELS_DB).st_ino != opened_ino:
            replacement.close()
            replacement = None
            return
        with _query_lock:
            old = _conn
            _conn, _conn_ino = replacement, opened_ino
            replacement = None
            if old is not None:
                old.close()
    except Exception:
        logger.warning("parcel database background warmup failed", exc_info=True)
    finally:
        if replacement is not None:
            replacement.close()
        with _refresh_lock:
            if _refreshing_ino == expected_ino:
                _refreshing_ino = None


def _schedule_refresh(ino: int) -> None:
    global _refreshing_ino
    with _refresh_lock:
        if _refreshing_ino == ino:
            return
        _refreshing_ino = ino
    worker = Thread(target=_refresh_worker, args=(ino,), daemon=True,
                    name="parcel-db-warmup")
    worker.start()


def warmup() -> bool:
    """Synchronously open the cadastre before the service accepts requests."""
    global _conn, _conn_ino
    replacement = None
    try:
        replacement, ino = _open_base()
        with _query_lock:
            old = _conn
            _conn, _conn_ino = replacement, ino
            replacement = None
            if old is not None:
                old.close()
        return True
    except Exception:
        logger.warning("parcel database startup warmup unavailable", exc_info=True)
        return False
    finally:
        if replacement is not None:
            replacement.close()


def _get_conn():
    """Return a warm cursor; schedule DB generations to open off-request.

    The caller holds ``_query_lock``. Cold-opening the 19 GB DB here would be
    uncancellable, so startup prewarms the first generation and inode changes
    trigger one background replacement. Requests use radius fallback until
    that replacement is ready instead of waiting past their signal deadline.
    """
    stat = os.stat(PARCELS_DB)
    if _conn is None or stat.st_ino != _conn_ino:
        _schedule_refresh(stat.st_ino)
        raise RuntimeError("parcel database generation is warming")
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
    timeout_s: float | None = None,
) -> list[bool] | None:
    """Return whether each evidence point is strictly inside the target lot.

    ``None`` means the cadastre database/target parcel was unavailable. A
    returned ``False`` is conclusive for this snapshot. The R-tree query reads
    every geometry slice in one small envelope; multiple slices with the same
    PFI are treated as one parcel.
    """
    if timeout_s is not None:
        if (not isinstance(timeout_s, (int, float))
                or not math.isfinite(timeout_s) or timeout_s <= 0):
            return None
        deadline = time.monotonic() + timeout_s
    else:
        deadline = None
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

    timeout_fired = Event()
    acquired = False
    try:
        if deadline is None:
            _query_lock.acquire()
            acquired = True
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            acquired = _query_lock.acquire(timeout=remaining)
            if not acquired:
                return None
        try:
            conn = _get_conn()
            timer = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                interrupt = getattr(conn, "interrupt", None)
                if not callable(interrupt):
                    # A timeout contract without a cancellation primitive is
                    # not a timeout. Fall back rather than start unbounded I/O.
                    return None

                def _interrupt() -> None:
                    timeout_fired.set()
                    interrupt()

                timer = Timer(remaining, _interrupt)
                timer.daemon = True
                timer.start()
            try:
                rows = conn.execute(
                    """
                    SELECT pfi, ST_AsGeoJSON(geom)
                    FROM parcels
                    WHERE state = ?
                      AND ST_Intersects(geom, ST_MakeEnvelope(?, ?, ?, ?))
                    """,
                    [state.lower().strip(), west, south, east, north],
                ).fetchall()
            finally:
                if timer is not None:
                    timer.cancel()
                    timer.join()
            if timeout_fired.is_set():
                return None
        finally:
            if acquired:
                _query_lock.release()
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
        if timeout_fired.is_set():
            logger.info("parcel attribution exceeded its signal budget; "
                        "using radius fallback")
            return None
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
