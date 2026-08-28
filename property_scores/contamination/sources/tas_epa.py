"""TAS EPA LIST activity/storage points as evidence-only context.

The LIST metadata grants CC BY 3.0 AU for both layers. Client/business holder
fields are deliberately not requested: the score only needs public activity
context and distance, and neither layer is a contaminated-land finding.
"""
import time as _time

from property_scores.contamination.sources._common import (
    _distance_m,
    fetch_json,
    geojson_features_or_none,
    point_coords,
)

REGULATED_QUERY_URL = (
    "https://services.thelist.tas.gov.au/arcgis/rest/services/Public/"
    "NaturalEnvironment/MapServer/74/query"
)
UPSS_QUERY_URL = (
    "https://services.thelist.tas.gov.au/arcgis/rest/services/Public/"
    "Infrastructure/MapServer/51/query"
)
REGULATED_CACHE_TTL_S = 24 * 3600
UPSS_CACHE_TTL_S = 30 * 24 * 3600
_PAGE_SIZE = 2_000
_regulated_cache: tuple[list[dict], float] | None = None
_upss_cache: tuple[list[dict], float] | None = None


def _clean(value):
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def clear_cache() -> None:
    global _regulated_cache, _upss_cache
    _regulated_cache = None
    _upss_cache = None


def _query_all(query_url: str, out_fields: str) -> list | None:
    count_data = fetch_json(query_url, {
        "where": "1=1",
        "returnCountOnly": "true",
        "f": "json",
    })
    expected = count_data.get("count") if isinstance(count_data, dict) else None
    if not isinstance(expected, int) or expected < 1 or expected > 50_000:
        return None
    rows = []
    offset = 0
    seen_objectids = set()
    last_objectid = None
    while True:
        data = fetch_json(query_url, {
            "where": "1=1",
            "outFields": out_fields,
            "returnGeometry": "true",
            "outSR": "4326",
            "resultOffset": offset,
            "resultRecordCount": _PAGE_SIZE,
            "orderByFields": "OBJECTID",
            "f": "geojson",
        })
        features = geojson_features_or_none(data)
        if features is None:
            return None
        for feature in features:
            props = feature.get("properties") if isinstance(feature, dict) else None
            objectid = props.get("OBJECTID") if isinstance(props, dict) else None
            if (not isinstance(objectid, int) or objectid in seen_objectids
                    or (last_objectid is not None and objectid <= last_objectid)):
                return None
            seen_objectids.add(objectid)
            last_objectid = objectid
        rows.extend(features)
        offset += len(features)
        if offset == expected:
            return rows
        if (offset > expected or not features or offset > 50_000
                or (not data.get("exceededTransferLimit")
                    and len(features) < _PAGE_SIZE)):
            return None


def all_regulated_sites(force_refresh: bool = False) -> list[dict] | None:
    global _regulated_cache
    now = _time.time()
    if (
        not force_refresh
        and _regulated_cache is not None
        and now - _regulated_cache[1] < REGULATED_CACHE_TTL_S
    ):
        return _regulated_cache[0]
    features = _query_all(
        REGULATED_QUERY_URL,
        "OBJECTID,SITEID,PREMISES_NAME,ACTIVITY_CATEGORY",
    )
    if features is None:
        return None
    rows = []
    for feature in features:
        coords = point_coords(feature)
        props = feature.get("properties") if isinstance(feature, dict) else None
        if coords is None or not isinstance(props, dict):
            return None
        site_id = _clean(props.get("SITEID"))
        if site_id is None:
            return None
        lat, lng = coords
        rows.append({
            "site_id": site_id,
            "premises_name": _clean(props.get("PREMISES_NAME")),
            "activity_category": _clean(props.get("ACTIVITY_CATEGORY")),
            "source_kind": "EPA regulated site",
            "lat": lat,
            "lng": lng,
        })
    _regulated_cache = (rows, now)
    return rows


def all_upss(force_refresh: bool = False) -> list[dict] | None:
    global _upss_cache
    now = _time.time()
    if (
        not force_refresh
        and _upss_cache is not None
        and now - _upss_cache[1] < UPSS_CACHE_TTL_S
    ):
        return _upss_cache[0]
    features = _query_all(UPSS_QUERY_URL, "OBJECTID,SITE_ID,STATUS")
    if features is None:
        return None
    rows = []
    for feature in features:
        coords = point_coords(feature)
        props = feature.get("properties") if isinstance(feature, dict) else None
        if coords is None or not isinstance(props, dict):
            return None
        site_id = _clean(props.get("SITE_ID"))
        if site_id is None:
            return None
        lat, lng = coords
        rows.append({
            "site_id": site_id,
            "status": _clean(props.get("STATUS")),
            "source_kind": "EPA underground petroleum storage system",
            "lat": lat,
            "lng": lng,
        })
    _upss_cache = (rows, now)
    return rows


def _near(rows: list[dict] | None, lat: float, lng: float, radius_m: int,
          include_coordinates: bool) -> list[dict] | None:
    if rows is None:
        return None
    hits = []
    for row in rows:
        distance = _distance_m(lat, lng, row["lat"], row["lng"])
        if distance <= radius_m:
            hit = dict(row)
            if not include_coordinates:
                hit.pop("lat", None)
                hit.pop("lng", None)
            hit["distance_m"] = round(distance)
            hits.append(hit)
    return sorted(hits, key=lambda row: row["distance_m"])


def regulated_sites_near(lat: float, lng: float, radius_m: int = 500,
                         include_coordinates: bool = False) -> list[dict] | None:
    return _near(all_regulated_sites(), lat, lng, radius_m, include_coordinates)


def upss_near(lat: float, lng: float, radius_m: int = 500,
              include_coordinates: bool = False) -> list[dict] | None:
    return _near(all_upss(), lat, lng, radius_m, include_coordinates)
