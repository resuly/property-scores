"""SA EPA Groundwater Prohibition Areas (GPA) adapter.

The whole state is 19 polygons in a 163KB GeoJSON file, so there is no query
service to call and none is needed: download the bundle once, keep it on disk
for 7 days, and answer hits with a local point-in-polygon test.

Semantics match VIC's GQRUZ: a GPA is a legal area where EPA SA prohibits
taking groundwater because site contamination has reached it. An inside hit is
the strongest signal in this batch - it is an official finding about the
groundwater under the parcel, not a proximity heuristic.

Fail-closed (2026-08-10 audit): ``None`` = the layer could not be read
(download failure, undecodable body, unrecognised structure). ``[]`` = read
fine, the point is not in or near any prohibition area. A failed download
never overwrites a good cache file.

Attribution (CC BY 4.0): ``Groundwater Prohibition Areas (c) Environment
Protection Authority (South Australia), licensed under CC BY 4.0.``
"""

import json
import logging
import os
import tempfile
import time as _time

from property_scores.contamination.sources._common import (
    fetch_bytes,
    geojson_features_or_none,
    polygon_distance_m,
    polygon_rings,
)

logger = logging.getLogger(__name__)

GEOJSON_URL = (
    "https://data.sa.gov.au/data/dataset/94498527-1cbc-476b-9f9e-9ca52efd687b"
    "/resource/d21cb39b-c8a1-41b1-8774-12f105c63ab1/download"
    "/epa_groundwaterprohibitionarea.geojson"
)

CACHE_PATH = os.path.join("data", "contam_cache", "sa_gpa.geojson")
CACHE_TTL_S = 7 * 24 * 3600

_parsed: tuple[list[dict], float] | None = None


def _cache_fresh(path: str) -> bool:
    try:
        return (_time.time() - os.path.getmtime(path)) < CACHE_TTL_S
    except OSError:
        return False


def _write_cache(path: str, payload: bytes) -> None:
    """Atomic replace, so a crash mid-write cannot leave a truncated cache."""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".")
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("could not cache SA GPA bundle at %s: %s", path, exc)


def _load_raw(cache_path: str, force_refresh: bool):
    """Return the decoded GeoJSON bundle, or ``None``."""
    if not force_refresh and _cache_fresh(cache_path):
        try:
            with open(cache_path, "rb") as handle:
                return json.loads(handle.read())
        except (OSError, ValueError) as exc:
        # 有意不 catch BudgetExceeded(2026-08-27 delta review): 预算耗尽时不走
        # stale 兜底而让 signal 转 error, 是把"耗尽"当 outage 的 fail-closed
        # 收窄; error 会拦安慰标签且不缓存, 方向安全。
            logger.warning("SA GPA cache unreadable, refetching: %s", exc)

    payload = fetch_bytes(GEOJSON_URL)
    if payload is None:
        # Download failed. A stale cache is still real data; an empty answer
        # would be a lie about a statutory prohibition area.
        try:
            with open(cache_path, "rb") as handle:
                logger.warning("SA GPA download failed; serving stale cache")
                return json.loads(handle.read())
        except (OSError, ValueError):
            return None
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    _write_cache(cache_path, payload)
    return data


def _parse(data) -> list[dict] | None:
    features = geojson_features_or_none(data)
    if features is None:
        return None
    areas = []
    for feat in features:
        if not isinstance(feat, dict):
            return None
        props = feat.get("properties")
        rings = polygon_rings(feat.get("geometry"))
        if not isinstance(props, dict) or rings is None:
            return None
        areas.append({
            "site": str(props.get("SITE") or "").strip() or "Unknown",
            "type": str(props.get("TYPE") or "").strip(),
            "epa_reference_number": props.get("EPA_REFERENCE_NUMBER"),
            "date_established_ms": props.get("DATE_ESTABLISHED"),
            "depth": str(props.get("DEPTH") or "").strip() or None,
            "rings": rings,
        })
    return areas


def load_areas(cache_path: str = CACHE_PATH,
               force_refresh: bool = False) -> list[dict] | None:
    """All GPA polygons with parsed rings, or ``None`` if unavailable."""
    global _parsed
    now = _time.time()
    if not force_refresh and _parsed is not None and now - _parsed[1] < CACHE_TTL_S:
        return _parsed[0]
    data = _load_raw(cache_path, force_refresh)
    if data is None:
        return None
    areas = _parse(data)
    if areas is None:
        return None
    _parsed = (areas, now)
    return areas


def clear_cache() -> None:
    """Drop the in-process parse cache (tests, cron-side refreshes)."""
    global _parsed
    _parsed = None


def areas_near(lat: float, lng: float, radius_m: int = 500,
               cache_path: str = CACHE_PATH) -> list[dict] | None:
    """Prohibition areas containing or near a point, nearest first.

    Returns ``[{site, type, epa_reference_number, date_established_ms, depth,
    inside, distance_m}]``; ``inside`` is True and ``distance_m`` is 0 when the
    point falls within the polygon. ``None`` if the layer could not be read,
    ``[]`` if it was read and the point is clear of every area.
    """
    if radius_m < 0:
        raise ValueError("radius_m must not be negative")
    areas = load_areas(cache_path)
    if areas is None:
        return None
    hits = []
    for area in areas:
        dist = polygon_distance_m(lat, lng, area["rings"])
        if dist > radius_m:
            continue
        hit = {k: v for k, v in area.items() if k != "rings"}
        hit["inside"] = dist == 0.0
        hit["distance_m"] = round(dist)
        hits.append(hit)
    return sorted(hits, key=lambda a: a["distance_m"])
