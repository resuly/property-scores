"""
Flood risk score using Australian state government ArcGIS REST endpoints.

Queries planning overlay layers (flood zones, floodways, stormwater) to
determine whether a coordinate falls within mapped flood hazard areas.
Score 0-100 where 100 = lowest risk / safest.
"""

import requests

# ---------------------------------------------------------------------------
# State bounding boxes (approximate, WGS84)
# ACT is checked first since it sits inside NSW bounds.
# ---------------------------------------------------------------------------
STATE_BOUNDS: list[tuple[str, float, float, float, float]] = [
    # (state, min_lat, max_lat, min_lng, max_lng)
    ("ACT", -35.93, -35.12, 148.76, 149.40),
    ("VIC", -39.20, -33.98, 140.96, 149.98),
    ("TAS", -43.65, -39.60, 143.50, 148.50),
    ("SA",  -38.10, -25.95, 129.00, 141.00),
    ("NSW", -37.55, -28.15, 140.99, 153.64),
    ("QLD", -29.18, -10.05, 137.95, 153.55),
    ("WA",  -35.13, -13.69, 112.92, 129.00),
    ("NT",  -26.00, -10.97, 129.00, 138.00),
]

# ---------------------------------------------------------------------------
# ArcGIS REST endpoints per state
# Each entry: (layer_name, url, severity)
#   severity: "floodway" | "flood" | "moderate" | "info"
# ---------------------------------------------------------------------------
VIC_PLAN_BASE = (
    "https://plan-gis.mapshare.vic.gov.au/arcgis/rest/services"
    "/Planning/Vicplan_PlanningSchemeOverlays/MapServer"
)

ENDPOINTS: dict[str, list[tuple[str, str, str]]] = {
    "VIC": [
        ("Floodway Overlay (FO)",    f"{VIC_PLAN_BASE}/14", "floodway"),
        ("Rural Floodway (RFO)",     f"{VIC_PLAN_BASE}/32", "floodway"),
        ("LSIO (1% AEP)",           f"{VIC_PLAN_BASE}/15", "flood"),
        ("Special Building (SBO)",   f"{VIC_PLAN_BASE}/16", "moderate"),
    ],
    "NSW": [
        ("Flood Planning",
         "https://mapprod3.environment.nsw.gov.au/arcgis/rest/services"
         "/ePlanning/Planning_Portal_Hazard/MapServer/230",
         "flood"),
    ],
    "SA": [
        ("General Flooding",
         "https://location.sa.gov.au/server6/rest/services"
         "/ePlanningPublic/CurrentPDC_wmas/MapServer/16",
         "flood"),
        ("Evidence Required",
         "https://location.sa.gov.au/server6/rest/services"
         "/ePlanningPublic/CurrentPDC_wmas/MapServer/17",
         "moderate"),
        ("Coastal Flooding",
         "https://location.sa.gov.au/server6/rest/services"
         "/ePlanningPublic/CurrentPDC_wmas/MapServer/51",
         "flood"),
    ],
    "TAS": [
        ("Planning Overlay (Flood)",
         "https://services.thelist.tas.gov.au/arcgis/rest/services"
         "/Public/PlanningOnline/MapServer/3",
         "flood"),
    ],
    "ACT": [
        ("1% AEP Flood Extent",
         "https://services1.arcgis.com/E5n4f1VY84i0xSjy/arcgis/rest/services"
         "/ACTGOV_FLOOD_EXTENT/FeatureServer/0",
         "flood"),
    ],
}

# Severity → score range when point is *inside* the overlay
SEVERITY_SCORES: dict[str, tuple[int, int]] = {
    "floodway": (10, 20),
    "flood":    (20, 40),
    "moderate": (40, 60),
}

TIMEOUT = 10


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _detect_state(lat: float, lng: float) -> str | None:
    """Return Australian state/territory code for a coordinate."""
    for state, min_lat, max_lat, min_lng, max_lng in STATE_BOUNDS:
        if min_lat <= lat <= max_lat and min_lng <= lng <= max_lng:
            return state
    return None


def _query_layer(url: str, lat: float, lng: float,
                 *, where: str | None = None,
                 count_only: bool = False) -> dict | None:
    """Query an ArcGIS REST layer with a point intersection.

    Returns the parsed JSON response, or None on error.
    """
    params: dict[str, str] = {
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "outSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "f": "json",
    }
    if count_only:
        params["returnCountOnly"] = "true"
    if where:
        params["where"] = where

    try:
        resp = requests.get(f"{url}/query", params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError):
        return None


def _layer_has_features(url: str, lat: float, lng: float,
                        *, where: str | None = None) -> bool | None:
    """Check whether any features intersect the point.

    Returns True/False, or None if the endpoint could not be reached.
    """
    data = _query_layer(url, lat, lng, where=where, count_only=True)
    if data is None:
        return None
    count = data.get("count")
    if count is not None:
        return count > 0
    # Some servers don't honour returnCountOnly; fall back to feature check.
    features = data.get("features")
    if features is not None:
        return len(features) > 0
    return None


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------

def flood_score(lat: float, lng: float) -> dict:
    """Compute flood risk score for a coordinate.

    Args:
        lat, lng: WGS84 coordinates.

    Returns:
        dict with score (0-100), label, flood_zones, state, zone_count.
    """
    state = _detect_state(lat, lng)
    if state is None:
        return {
            "score": None,
            "label": "Outside Australia",
            "flood_zones": [],
            "state": None,
            "zone_count": 0,
            "error": "Coordinate is outside Australian state bounding boxes",
        }

    layers = ENDPOINTS.get(state)
    if not layers:
        return {
            "score": 85,
            "label": "Low Risk",
            "flood_zones": [],
            "state": state,
            "zone_count": 0,
            "note": (
                f"No flood overlay data available for {state}. "
                "Score assumes low risk based on absence of data. "
                "Coverage is limited to VIC, NSW, SA, TAS, and ACT."
            ),
        }

    hit_zones: list[str] = []
    worst_severity: str | None = None
    warnings: list[str] = []
    severity_rank = {"floodway": 0, "flood": 1, "moderate": 2}

    for layer_name, url, severity in layers:
        # TAS uses a single overlay layer filtered by name
        where = "O_NAME LIKE '%Flood%'" if state == "TAS" else None

        result = _layer_has_features(url, lat, lng, where=where)

        if result is None:
            warnings.append(f"Could not reach {layer_name}")
            continue

        if result:
            hit_zones.append(layer_name)
            if worst_severity is None or severity_rank.get(severity, 99) < severity_rank.get(worst_severity, 99):
                worst_severity = severity

    # --- Compute score ---
    if worst_severity is not None:
        lo, hi = SEVERITY_SCORES[worst_severity]
        # More overlapping zones = worse within the range
        zone_penalty = min(len(hit_zones) - 1, 3) * 3  # up to -9
        score = max(lo, hi - zone_penalty)
    else:
        # No flood zone hit
        if warnings:
            # Some layers unreachable, less certainty
            score = 80
        else:
            score = 90

    score = max(0, min(100, score))

    # --- Label ---
    if score >= 90:
        label = "Very Low Risk"
    elif score >= 70:
        label = "Low Risk"
    elif score >= 40:
        label = "Moderate Risk"
    elif score >= 20:
        label = "High Risk"
    else:
        label = "Very High Risk"

    result_dict: dict = {
        "score": score,
        "label": label,
        "flood_zones": hit_zones,
        "state": state,
        "zone_count": len(hit_zones),
    }
    if warnings:
        result_dict["warnings"] = warnings

    return result_dict


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Compute flood risk score")
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lng", type=float, required=True)
    args = parser.parse_args()

    result = flood_score(args.lat, args.lng)
    print(f"Flood Score: {result['score']}/100 ({result['label']})")
    print(f"State: {result.get('state', 'N/A')}")
    if result["flood_zones"]:
        print(f"Flood zones: {', '.join(result['flood_zones'])}")
    if result.get("note"):
        print(f"Note: {result['note']}")
    if result.get("warnings"):
        print(f"Warnings: {'; '.join(result['warnings'])}")
