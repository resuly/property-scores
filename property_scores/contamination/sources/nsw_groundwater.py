"""NSW EPI Groundwater Vulnerability point-intersection adapter.

Data.NSW registers the ArcGIS REST resource under Creative Commons
Attribution and the publisher metadata states CC BY 4.0. This is planning
sensitivity evidence, not a contamination finding or groundwater plume.
"""
from property_scores.contamination.sources._common import (
    fetch_json,
    geojson_features_or_none,
)

QUERY_URL = (
    "https://mapprod3.environment.nsw.gov.au/arcgis/rest/services/"
    "Planning/Protection/MapServer/4/query"
)


def vulnerability_at(lat: float, lng: float) -> list[dict] | None:
    data = fetch_json(QUERY_URL, {
        "where": "1=1",
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": (
            "EPI_NAME,LGA_NAME,PUBLISHED_DATE,COMMENCED_DATE,"
            "CURRENCY_DATE,AMENDMENT,LAY_CLASS,EPI_TYPE"
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
            "planning_instrument": str(attrs.get("EPI_NAME") or "").strip(),
            "lga": str(attrs.get("LGA_NAME") or "").strip(),
            "layer_class": str(attrs.get("LAY_CLASS") or "").strip(),
            "instrument_type": str(attrs.get("EPI_TYPE") or "").strip(),
            "currency_date_ms": attrs.get("CURRENCY_DATE"),
            "inside": True,
            "distance_m": 0,
        })
    return rows
