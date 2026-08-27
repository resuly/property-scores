"""NSW EPA Contaminated Sites List adapter (Data.NSW hosted FeatureServer).

The whole register is 1,991 points, comfortably under the service's
maxRecordCount of 2000, so we pull it once and answer radius queries from an
in-process cache instead of hitting the endpoint per address. Cache TTL is
24h; the register changes on the order of weeks.

Fail-closed (2026-08-10 audit): ``None`` = the register could not be read
(network error, non-2xx, HTTP 200 carrying an ArcGIS ``{"error": ...}`` body,
or an unrecognised payload shape). ``[]`` = the register was read and holds
no site inside the radius. A failed pull is never cached.

Attribution required on any surface that exposes these records:
``Contaminated land data (c) State of New South Wales through the NSW
Environment Protection Authority, via Data.NSW.`` Official wording caveat:
these are *notified* sites; notified is not the same as regulated or proven
contaminated.
"""

import logging
import time as _time

from property_scores.contamination.sources._common import (
    _distance_m,
    fetch_json,
    geojson_features_or_none,
)

logger = logging.getLogger(__name__)

QUERY_URL = (
    "https://portal.data.nsw.gov.au/arcgis/rest/services/Hosted"
    "/Contaminated_Sites_List/FeatureServer/0/query"
)
OUT_FIELDS = (
    "objectid,suburb,sitename,address,contaminationactivitytype,"
    "managementclass,latitude,longitude"
)
_PAGE_SIZE = 2000
_MAX_PAGES = 5  # 10,000 records, 5x the current register size

CACHE_TTL_S = 24 * 3600
_cache: tuple[list[dict], float] | None = None


def _clean(value) -> str:
    """Trim to a string. Upstream ``managementclass`` values carry a trailing
    space ("Regulation under CLM Act not required "), measured 2026-08-27;
    untrimmed they break equality checks and render badly."""
    if value is None:
        return ""
    return str(value).strip()


def _parse_features(features: list) -> list[dict] | None:
    sites: list[dict] = []
    for feat in features:
        if not isinstance(feat, dict):
            return None
        attrs = feat.get("attributes")
        if not isinstance(attrs, dict):
            return None
        lat, lng = attrs.get("latitude"), attrs.get("longitude")
        if lat is None or lng is None:
            # Every record in this register is geocoded; a missing coordinate
            # means the schema moved under us, which is an outage.
            return None
        try:
            lat, lng = float(lat), float(lng)
        except (TypeError, ValueError):
            return None
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
            return None
        sites.append({
            "name": _clean(attrs.get("sitename")) or "Unknown",
            "activity_type": _clean(attrs.get("contaminationactivitytype")),
            "management_class": _clean(attrs.get("managementclass")),
            "suburb": _clean(attrs.get("suburb")),
            "address": _clean(attrs.get("address")),
            "lat": round(lat, 6),
            "lng": round(lng, 6),
        })
    return sites


def fetch_all_sites() -> list[dict] | None:
    """Pull the full register, bypassing the cache. ``None`` on any failure."""
    collected: list[dict] = []
    offset = 0
    for _ in range(_MAX_PAGES):
        data = fetch_json(QUERY_URL, {
            "where": "1=1",
            "outFields": OUT_FIELDS,
            "returnGeometry": "false",
            "resultOffset": offset,
            "resultRecordCount": _PAGE_SIZE,
            "f": "json",
        })
        features = geojson_features_or_none(data)
        if features is None:
            return None
        page = _parse_features(features)
        if page is None:
            return None
        collected.extend(page)
        if not isinstance(data, dict) or not data.get("exceededTransferLimit"):
            return collected
        offset += len(features)
        if not features:
            return collected
    logger.warning("NSW contaminated sites paging hit the %d page cap", _MAX_PAGES)
    return collected


def all_sites(force_refresh: bool = False) -> list[dict] | None:
    """Full register, served from a 24h in-process cache. ``None`` on failure.

    A failure is not cached and does not evict a still-valid cache entry, so a
    transient outage degrades to slightly stale data rather than to a false
    "no contaminated sites in NSW".
    """
    global _cache
    now = _time.time()
    if not force_refresh and _cache is not None:
        sites, ts = _cache
        if now - ts < CACHE_TTL_S:
            return sites
    sites = fetch_all_sites()
    if sites is None:
        if _cache is not None:
            logger.warning("NSW register refresh failed; serving stale cache")
            return _cache[0]
        return None
    _cache = (sites, now)
    return sites


def clear_cache() -> None:
    """Drop the in-process register cache (tests, and cron-side refreshes)."""
    global _cache
    _cache = None


def sites_near(lat: float, lng: float, radius_m: int = 2000) -> list[dict] | None:
    """Contaminated sites within ``radius_m`` of a point, nearest first.

    Returns ``[{name, activity_type, management_class, suburb, address,
    distance_m, lat, lng}]``, or ``None`` if the register could not be read.
    ``[]`` means it was read and nothing is nearby - the two are not
    interchangeable.
    """
    if radius_m <= 0:
        raise ValueError("radius_m must be positive")
    sites = all_sites()
    if sites is None:
        return None
    hits = []
    for site in sites:
        dist = _distance_m(lat, lng, site["lat"], site["lng"])
        if dist <= radius_m:
            hit = dict(site)
            hit["distance_m"] = round(dist)
            hits.append(hit)
    return sorted(hits, key=lambda s: s["distance_m"])
