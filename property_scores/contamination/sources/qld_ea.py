"""QLD Environmental Authority polygons as on-site activity evidence.

The point-intersection query returns only permit/activity facts. The upstream
primary_holder field is neither requested nor returned.
"""
from property_scores.contamination.sources._common import (
    fetch_json,
    geojson_features_or_none,
)

QUERY_URL = (
    "https://spatial-gis.information.qld.gov.au/arcgis/rest/services/"
    "Boundaries/AdminBoundariesFramework/MapServer/140/query"
)


def activities_at(lat: float, lng: float) -> list[dict] | None:
    data = fetch_json(QUERY_URL, {
        "where": "1=1",
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": (
            "permit_ref_no,permit_version,permit_type,status,grant_date,"
            "effective_date,location_type,adjacent,era,permit_link,"
            "conditions_type,site,era_threshold,era_category"
        ),
        "returnGeometry": "false",
        "f": "json",
    })
    features = geojson_features_or_none(data)
    if features is None:
        return None
    rows = []
    for feature in features:
        attrs = feature.get("attributes") if isinstance(feature, dict) else None
        if not isinstance(attrs, dict):
            return None
        rows.append({
            "permit_reference": str(attrs.get("permit_ref_no") or "").strip(),
            "permit_version": attrs.get("permit_version"),
            "permit_type": str(attrs.get("permit_type") or "").strip(),
            "status": str(attrs.get("status") or "").strip(),
            "activity": str(attrs.get("era") or "").strip(),
            "activity_threshold": str(attrs.get("era_threshold") or "").strip(),
            "activity_category": str(attrs.get("era_category") or "").strip(),
            "conditions_type": str(attrs.get("conditions_type") or "").strip(),
            "site": str(attrs.get("site") or "").strip(),
            "register_link": str(attrs.get("permit_link") or "").strip() or None,
            "inside": True,
            "distance_m": 0,
        })
    return rows
