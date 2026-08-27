"""SA EPA licensed activities as on-site historical-use evidence.

The public GeoJSON is CC BY 4.0. Licence-holder names are deliberately not
parsed or returned because they can identify natural persons; the score only
needs activity, licence number and public-register link.
"""
import time as _time

from property_scores.contamination.sources._common import (
    _distance_m,
    fetch_json,
    geojson_features_or_none,
    point_coords,
)

GEOJSON_URL = (
    "https://data.sa.gov.au/data/dataset/8fdb86ff-d3d1-4f9e-85a5-bed4080d5ee1"
    "/resource/26e076f3-c37f-4089-8f28-3f7c9afd997e/download/"
    "topo_epa_activities_wgs84.geojson"
)
CACHE_TTL_S = 7 * 24 * 3600
_cache: tuple[list[dict], float] | None = None


def clear_cache() -> None:
    global _cache
    _cache = None


def all_activities(force_refresh: bool = False) -> list[dict] | None:
    global _cache
    now = _time.time()
    if not force_refresh and _cache is not None and now - _cache[1] < CACHE_TTL_S:
        return _cache[0]
    data = fetch_json(GEOJSON_URL)
    features = geojson_features_or_none(data)
    if features is None:
        # A stale row is still useful for a map, but not for a score that
        # claims the source was checked now. Return None so the builder marks
        # status=error and the combined result is not cached.
        return None
    rows = []
    for feature in features:
        coords = point_coords(feature)
        props = feature.get("properties") if isinstance(feature, dict) else None
        if coords is None or not isinstance(props, dict):
            return None
        lat, lng = coords
        rows.append({
            "licence_number": props.get("EPALICENCE"),
            "activity": str(props.get("ACTIVITY") or "").strip(),
            "register_link": str(props.get("PR_LINK") or "").strip() or None,
            "lat": lat,
            "lng": lng,
        })
    _cache = (rows, now)
    return rows


def activities_near(lat: float, lng: float, radius_m: int = 30) -> list[dict] | None:
    rows = all_activities()
    if rows is None:
        return None
    hits = []
    for row in rows:
        distance = _distance_m(lat, lng, row["lat"], row["lng"])
        if distance <= radius_m:
            hit = dict(row)
            hit.pop("lat", None)
            hit.pop("lng", None)
            hit["distance_m"] = round(distance)
            hits.append(hit)
    return sorted(hits, key=lambda row: row["distance_m"])
