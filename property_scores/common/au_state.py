"""Australian state detection and ArcGIS REST query helpers."""

import requests

# Bounding boxes: (min_lng, min_lat, max_lng, max_lat)
# Check ACT first (inside NSW), then states from smallest to largest
_STATE_BOXES = [
    ("ACT", 148.7, -35.93, 149.4, -35.1),
    ("TAS", 143.5, -43.7, 148.5, -39.5),
    ("VIC", 140.9, -39.2, 150.0, -33.9),
    ("SA",  129.0, -38.1, 141.0, -26.0),
    ("NSW", 140.9, -37.6, 153.7, -28.1),
    ("QLD", 137.9, -29.2, 153.6, -10.0),
    ("WA",  112.9, -35.2, 129.0, -13.7),
    ("NT",  129.0, -26.0, 138.0, -10.9),
]


def detect_state(lat: float, lng: float) -> str | None:
    for state, x1, y1, x2, y2 in _STATE_BOXES:
        if x1 <= lng <= x2 and y1 <= lat <= y2:
            return state
    return None


def arcgis_point_query(endpoint: str, lat: float, lng: float,
                       *, out_fields: str = "*", timeout: int = 10) -> dict | None:
    """Query an ArcGIS MapServer/FeatureServer layer for features intersecting a point."""
    params = {
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "outSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": out_fields,
        "returnCountOnly": "false",
        "f": "json",
    }
    try:
        resp = requests.get(f"{endpoint}/query", params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            return None
        return data
    except (requests.RequestException, ValueError):
        return None


def arcgis_point_count(endpoint: str, lat: float, lng: float,
                       *, timeout: int = 8) -> int:
    """Fast count-only query — returns number of features at a point."""
    params = {
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "returnCountOnly": "true",
        "f": "json",
    }
    try:
        resp = requests.get(f"{endpoint}/query", params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        return data.get("count", 0)
    except (requests.RequestException, ValueError):
        return -1
