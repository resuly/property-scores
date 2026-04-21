"""
Bushfire risk score using Australian state government planning overlays.

Queries ArcGIS REST endpoints for bushfire-prone/management overlay areas
to determine if a coordinate falls within a mapped bushfire hazard zone.
Score 0-100 where 100 = lowest bushfire risk.
"""

import requests

VIC_PLAN_BASE = (
    "https://plan-gis.mapshare.vic.gov.au/arcgis/rest/services"
    "/Planning/Vicplan_PlanningSchemeOverlays/MapServer"
)

SA_PLAN_BASE = (
    "https://location.sa.gov.au/server6/rest/services"
    "/ePlanningPublic/CurrentPDC_wmas/MapServer"
)

# (layer_name, url, severity)
# severity: "extreme" | "high" | "moderate" | "low"
ENDPOINTS: dict[str, list[tuple[str, str, str]]] = {
    "VIC": [
        ("Bushfire Management Overlay (BMO)", f"{VIC_PLAN_BASE}/19", "high"),
    ],
    "NSW": [
        ("Bushfire Prone Land",
         "https://mapprod3.environment.nsw.gov.au/arcgis/rest/services"
         "/ePlanning/Planning_Portal_Hazard/MapServer/229",
         "high"),
    ],
    "WA": [
        ("Bush Fire Prone Area (OBRM-023)",
         "https://services.slip.wa.gov.au/public/rest/services"
         "/Landgate_Public_Maps/Map_of_Bush_Fire_Prone_Areas_3/MapServer/8",
         "moderate"),
    ],
    "SA": [
        ("Urban Interface",  f"{SA_PLAN_BASE}/9",  "extreme"),
        ("High Risk",        f"{SA_PLAN_BASE}/10", "high"),
        ("Medium Risk",      f"{SA_PLAN_BASE}/11", "moderate"),
        ("General Risk",     f"{SA_PLAN_BASE}/12", "low"),
        ("Regional",         f"{SA_PLAN_BASE}/13", "low"),
        ("Outback",          f"{SA_PLAN_BASE}/14", "low"),
    ],
    "TAS": [
        ("Bushfire Prone Areas",
         "https://services.thelist.tas.gov.au/arcgis/rest/services"
         "/Public/PlanningOnline/MapServer/3",
         "moderate"),
    ],
}

SEVERITY_SCORES = {
    "extreme": (5, 15),
    "high":    (15, 30),
    "moderate": (30, 50),
    "low":     (50, 65),
}

# NSW d_Category → severity
NSW_CATEGORY_MAP = {
    "Vegetation Category 1": "extreme",
    "Vegetation Category 2": "high",
    "Vegetation Category 3": "moderate",
    "Vegetation Buffer": "low",
}

TIMEOUT = 10


def _detect_state(lat: float, lng: float) -> str | None:
    boxes = [
        ("ACT", -35.93, -35.12, 148.76, 149.40),
        ("VIC", -39.20, -33.98, 140.96, 149.98),
        ("TAS", -43.65, -39.60, 143.50, 148.50),
        ("SA",  -38.10, -25.95, 129.00, 141.00),
        ("NSW", -37.55, -28.15, 140.99, 153.64),
        ("QLD", -29.18, -10.05, 137.95, 153.55),
        ("WA",  -35.13, -13.69, 112.92, 129.00),
        ("NT",  -26.00, -10.97, 129.00, 138.00),
    ]
    for state, min_lat, max_lat, min_lng, max_lng in boxes:
        if min_lat <= lat <= max_lat and min_lng <= lng <= max_lng:
            return state
    return None


def _query_arcgis(url: str, lat: float, lng: float,
                  *, where: str | None = None,
                  out_fields: str = "*") -> dict | None:
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
    if where:
        params["where"] = where
    try:
        resp = requests.get(f"{url}/query", params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            return None
        return data
    except (requests.RequestException, ValueError):
        return None


def _check_layer(state: str, layer_name: str, url: str, severity: str,
                 lat: float, lng: float) -> tuple[str | None, str | None]:
    """Check if point intersects a bushfire layer.
    Returns (detected_severity, category_detail) or (None, None).
    """
    where = None
    if state == "TAS":
        where = "O_NAME LIKE '%ush%ire%' OR O_NAME LIKE '%Bush Fire%'"

    data = _query_arcgis(url, lat, lng, where=where)
    if not data or not data.get("features"):
        return None, None

    attrs = data["features"][0].get("attributes", {})

    if state == "NSW":
        cat = attrs.get("d_Category", "")
        nsw_sev = NSW_CATEGORY_MAP.get(cat, severity)
        return nsw_sev, cat

    if state == "TAS":
        o_name = attrs.get("O_NAME", "")
        if "bush" not in o_name.lower() and "fire" not in o_name.lower():
            return None, None
        return severity, o_name

    detail = attrs.get("ZONE_CODE") or attrs.get("classvalue") or layer_name
    return severity, str(detail)


def bushfire_score(lat: float, lng: float) -> dict:
    """Compute bushfire risk score for an Australian coordinate.

    Returns:
        dict with score (0-100, 100=safest), label, bushfire_zones, state, category.
    """
    state = _detect_state(lat, lng)
    if not state:
        return {
            "score": None,
            "label": "Outside Australia",
            "bushfire_zones": [],
            "state": None,
            "category": None,
        }

    layers = ENDPOINTS.get(state)
    if not layers:
        return {
            "score": 85,
            "label": "Low Risk",
            "bushfire_zones": [],
            "state": state,
            "category": None,
            "note": f"No bushfire overlay data for {state}. Coverage: VIC, NSW, WA, SA, TAS.",
        }

    hits = []
    worst_severity = None
    worst_category = None
    severity_rank = {"extreme": 0, "high": 1, "moderate": 2, "low": 3}

    for layer_name, url, default_severity in layers:
        sev, detail = _check_layer(state, layer_name, url, default_severity, lat, lng)
        if sev:
            hits.append(layer_name)
            if worst_severity is None or severity_rank.get(sev, 99) < severity_rank.get(worst_severity, 99):
                worst_severity = sev
                worst_category = detail
            # SA has multiple bands — highest hit is enough, stop early
            if state == "SA":
                break

    if worst_severity:
        lo, hi = SEVERITY_SCORES[worst_severity]
        score = round((lo + hi) / 2)
    else:
        score = 90

    score = max(0, min(100, score))

    if score >= 80:
        label = "Very Low Risk"
    elif score >= 60:
        label = "Low Risk"
    elif score >= 40:
        label = "Moderate Risk"
    elif score >= 20:
        label = "High Risk"
    else:
        label = "Very High Risk"

    return {
        "score": score,
        "label": label,
        "bushfire_zones": hits,
        "state": state,
        "category": worst_category,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Compute bushfire risk score")
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lng", type=float, required=True)
    args = parser.parse_args()

    result = bushfire_score(args.lat, args.lng)
    print(f"Bushfire Score: {result['score']}/100 ({result['label']})")
    print(f"State: {result['state']}")
    if result['category']:
        print(f"Category: {result['category']}")
    if result['bushfire_zones']:
        for z in result['bushfire_zones']:
            print(f"  - {z}")
