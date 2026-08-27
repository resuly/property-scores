"""Shared HTTP + geometry helpers for the contamination source adapters.

Fail-closed discipline (2026-08-10 audit, inherited from ``score.py``):
a failed query returns ``None`` and an empty register returns ``[]``. Every
helper here that can fail returns ``None`` rather than an empty container, so
callers keep the two distinguishable all the way up.

Politeness: 10s timeout, one attempt plus one retry per request, browser
User-Agent. Retries cover transport-level failures only (connection errors,
non-2xx, undecodable bodies). An HTTP 200 carrying an upstream error document
is a logical failure, not a flaky socket, so it is not retried.
"""

import logging
import time as _time

import requests

# Reused rather than re-derived so the adapters cannot drift from the
# already-audited implementations in score.py. score.py is not modified.
from property_scores.contamination.score import (  # noqa: F401
    TIMEOUT,
    _distance_m,
    _features_or_none,
    _inside_polygon_rings,
    _search_envelope,
)

logger = logging.getLogger(__name__)

# Several of these government endpoints sit behind WAFs that answer a bare
# python-requests UA with a challenge page. A plain browser UA avoids it.
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept": "application/json, */*"}

_RETRY_SLEEP_S = 0.5


def fetch_json(url: str, params: dict | None = None, timeout: int = TIMEOUT):
    """GET ``url`` and return the decoded JSON body, or ``None`` on failure.

    ``None`` covers every way the request can fail to produce a body we can
    read: connection errors, timeouts, non-2xx status, and undecodable JSON.
    It deliberately does NOT distinguish "empty result" - that judgement
    belongs to the caller, which inspects the decoded body.

    One attempt plus one retry, per the politeness budget for these
    unauthenticated government services.
    """
    last_error = None
    for attempt in range(2):
        if attempt:
            _time.sleep(_RETRY_SLEEP_S)
        try:
            resp = requests.get(url, params=params, timeout=timeout, headers=HEADERS)
        except requests.RequestException as exc:
            last_error = exc
            continue
        if not resp.ok:
            # A non-2xx is an outage, not an empty register.
            last_error = f"HTTP {resp.status_code}"
            continue
        try:
            return resp.json()
        except ValueError as exc:
            last_error = exc
            continue
    logger.warning("fetch_json failed for %s: %s", url, last_error)
    return None


def fetch_bytes(url: str, timeout: int = TIMEOUT) -> bytes | None:
    """GET ``url`` and return the raw body, or ``None`` on failure.

    Same retry budget and same fail-closed meaning as :func:`fetch_json`.
    Used for the whole-of-state GeoJSON bundle that SA publishes as a file
    rather than as a query service.
    """
    last_error = None
    for attempt in range(2):
        if attempt:
            _time.sleep(_RETRY_SLEEP_S)
        try:
            resp = requests.get(url, timeout=timeout, headers=HEADERS)
        except requests.RequestException as exc:
            last_error = exc
            continue
        if not resp.ok:
            last_error = f"HTTP {resp.status_code}"
            continue
        return resp.content
    logger.warning("fetch_bytes failed for %s: %s", url, last_error)
    return None


def geojson_features_or_none(data) -> list | None:
    """Feature list of a successful GeoJSON/Esri body, else ``None``.

    Thin wrapper over ``score._features_or_none`` so that the adapters read
    the same way whichever flavour of upstream they talk to. GeoServer WFS
    reports failures as ``{"exceptions": [...]}`` under HTTP 200 and ArcGIS
    REST as ``{"error": {...}}``; both are already handled there.
    """
    return _features_or_none(data)


def point_coords(feature: dict) -> tuple[float, float] | None:
    """Return ``(lat, lng)`` of a GeoJSON Point feature, or ``None``.

    ``None`` here means "this feature is not shaped the way we expect", which
    the callers escalate into a whole-query ``None``: a register that changed
    shape has not been searched, whatever the status code said.
    """
    if not isinstance(feature, dict):
        return None
    geom = feature.get("geometry")
    if not isinstance(geom, dict) or geom.get("type") != "Point":
        return None
    coords = geom.get("coordinates")
    if not isinstance(coords, (list, tuple)) or len(coords) < 2:
        return None
    try:
        lng, lat = float(coords[0]), float(coords[1])
    except (TypeError, ValueError):
        return None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
        return None
    return lat, lng


def polygon_rings(geometry) -> list[list] | None:
    """Flatten a GeoJSON Polygon/MultiPolygon into a list of rings.

    Rings keep GeoJSON ``[lng, lat]`` ordering, which is what
    ``score._inside_polygon_rings`` expects. Returns ``None`` for anything
    that is not a polygon we can read.
    """
    if not isinstance(geometry, dict):
        return None
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if not isinstance(coords, (list, tuple)):
        return None
    if gtype == "Polygon":
        polygons = [coords]
    elif gtype == "MultiPolygon":
        polygons = list(coords)
    else:
        return None
    rings: list[list] = []
    for polygon in polygons:
        if not isinstance(polygon, (list, tuple)):
            return None
        for ring in polygon:
            if not isinstance(ring, (list, tuple)) or len(ring) < 3:
                return None
            clean = []
            for point in ring:
                if not isinstance(point, (list, tuple)) or len(point) < 2:
                    return None
                try:
                    clean.append((float(point[0]), float(point[1])))
                except (TypeError, ValueError):
                    return None
            rings.append(clean)
    return rings or None


def polygon_distance_m(lat: float, lng: float, rings: list[list]) -> float:
    """0.0 when the point is inside, else distance to the nearest vertex.

    Vertex distance is a conservative over-estimate of the true distance to
    the boundary (never an under-estimate by more than the vertex spacing);
    these registers are cadastre-resolution polygons with dense vertices, so
    it is close enough for a proximity band and needs no segment maths.
    """
    if _inside_polygon_rings(lat, lng, rings):
        return 0.0
    best = float("inf")
    for ring in rings:
        for point_lng, point_lat in ring:
            best = min(best, _distance_m(lat, lng, point_lat, point_lng))
    return best
