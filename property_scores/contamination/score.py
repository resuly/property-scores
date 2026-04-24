"""
Contamination risk score for Australian properties.

Two signal layers:
1. Official EPA registers — VIC (WFS), NSW (ArcGIS), WA (ArcGIS)
2. Industrial proximity — Overture POI fuel stations, factories, dry cleaners

Score 0-100 where 100 = cleanest / lowest contamination risk.
"""

import logging
import math

import requests

from property_scores.common.overture import get_db, pois_near

logger = logging.getLogger(__name__)

TIMEOUT = 10

# ---------------------------------------------------------------------------
# State detection
# ---------------------------------------------------------------------------

STATE_BOUNDS: list[tuple[str, float, float, float, float]] = [
    ("ACT", -35.93, -35.12, 148.76, 149.40),
    ("VIC", -39.20, -33.98, 140.96, 149.98),
    ("TAS", -43.65, -39.60, 143.50, 148.50),
    ("SA",  -38.10, -25.95, 129.00, 141.00),
    ("NSW", -37.55, -28.15, 140.99, 153.64),
    ("QLD", -29.18, -10.05, 137.95, 153.55),
    ("WA",  -35.13, -13.69, 112.92, 129.00),
    ("NT",  -26.00, -10.97, 129.00, 138.00),
]


def _detect_state(lat: float, lng: float) -> str | None:
    for state, min_lat, max_lat, min_lng, max_lng in STATE_BOUNDS:
        if min_lat <= lat <= max_lat and min_lng <= lng <= max_lng:
            return state
    return None


# ---------------------------------------------------------------------------
# EPA register queries
# ---------------------------------------------------------------------------

def _vic_epa_sites(lat: float, lng: float, radius_m: int = 2000) -> list[dict]:
    """Query VIC EPA Priority Sites Register via WFS."""
    deg = radius_m / 111_000
    bbox = f"{lng - deg},{lat - deg},{lng + deg},{lat + deg},EPSG:4326"
    try:
        resp = requests.get(
            "https://opendata.maps.vic.gov.au/geoserver/wfs",
            params={
                "service": "WFS",
                "version": "2.0.0",
                "request": "GetFeature",
                "typeNames": "open-data-platform:psr_point",
                "bbox": bbox,
                "outputFormat": "application/json",
                "count": "50",
            },
            timeout=TIMEOUT,
        )
        if not resp.ok:
            return []
        data = resp.json()
        features = data.get("features", [])
        results = []
        m_per_deg = 111_320 * math.cos(math.radians(lat))
        for f in features:
            coords = f.get("geometry", {}).get("coordinates", [])
            if len(coords) >= 2:
                dist = math.sqrt(
                    ((coords[0] - lng) * m_per_deg) ** 2 +
                    ((coords[1] - lat) * 111320) ** 2
                )
                props = f.get("properties", {})
                results.append({
                    "name": props.get("address", "Unknown"),
                    "issue": props.get("issue", ""),
                    "distance_m": round(dist),
                    "source": "VIC EPA PSR",
                })
        return sorted(results, key=lambda x: x["distance_m"])
    except (requests.RequestException, ValueError, KeyError):
        return []


def _nsw_epa_sites(lat: float, lng: float, radius_m: int = 2000) -> list[dict]:
    """Query NSW EPA Contaminated Land Notified Sites."""
    url = (
        "https://mapprod2.environment.nsw.gov.au/arcgis/rest/services"
        "/EPA/Contaminated_land_notified_sites/MapServer/0/query"
    )
    m_per_deg = 111_320 * math.cos(math.radians(lat))
    deg = radius_m / 111_000
    try:
        resp = requests.get(url, params={
            "geometry": f"{lng - deg},{lat - deg},{lng + deg},{lat + deg}",
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "outSR": "4326",
            "outFields": "SiteName,Longitude,Latitude,ManagementClass,ContaminationActivityType",
            "f": "json",
        }, timeout=TIMEOUT)
        if not resp.ok:
            return []
        data = resp.json()
        results = []
        for feat in data.get("features", []):
            a = feat.get("attributes", {})
            flng = a.get("Longitude")
            flat = a.get("Latitude")
            if flng and flat:
                dist = math.sqrt(
                    ((flng - lng) * m_per_deg) ** 2 +
                    ((flat - lat) * 111320) ** 2
                )
                results.append({
                    "name": a.get("SiteName", "Unknown"),
                    "issue": a.get("ContaminationActivityType", ""),
                    "distance_m": round(dist),
                    "source": "NSW EPA CLR",
                })
        return sorted(results, key=lambda x: x["distance_m"])
    except (requests.RequestException, ValueError, KeyError):
        return []


def _wa_epa_sites(lat: float, lng: float, radius_m: int = 2000) -> list[dict]:
    """Query WA DWER Contaminated Sites Database."""
    url = (
        "https://public-services.slip.wa.gov.au/public/rest/services"
        "/SLIP_Public_Services/Environment/MapServer/5/query"
    )
    try:
        resp = requests.get(url, params={
            "geometry": f"{lng},{lat}",
            "geometryType": "esriGeometryPoint",
            "inSR": "4326",
            "outSR": "4326",
            "distance": radius_m,
            "units": "esriSRUnit_Meter",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "*",
            "f": "json",
        }, timeout=TIMEOUT)
        if not resp.ok:
            return []
        data = resp.json()
        m_per_deg = 111_320 * math.cos(math.radians(lat))
        results = []
        for feat in data.get("features", []):
            a = feat.get("attributes", {})
            geom = feat.get("geometry", {})
            flng = geom.get("x") or a.get("longitude")
            flat = geom.get("y") or a.get("latitude")
            if flng and flat:
                dist = math.sqrt(
                    ((flng - lng) * m_per_deg) ** 2 +
                    ((flat - lat) * 111320) ** 2
                )
            else:
                dist = radius_m
            results.append({
                "name": a.get("SITENAME", a.get("site_name", "Unknown")),
                "issue": a.get("CLASSIFICATION", ""),
                "distance_m": round(dist),
                "source": "WA DWER",
            })
        return sorted(results, key=lambda x: x["distance_m"])
    except (requests.RequestException, ValueError, KeyError):
        return []


# ---------------------------------------------------------------------------
# Industrial proximity (Overture POIs — national coverage)
# ---------------------------------------------------------------------------

INDUSTRIAL_KEYWORDS = {
    "fuel_station", "gas_station", "petrol",
    "chemical_plant", "chemical",
    "dry_cleaning",
    "recycling_center", "scrap",
    "waste_management", "waste_disposal",
    "auto_repair", "car_repair", "mechanic",
}

# Categories that LOOK industrial but aren't pollution risks
INDUSTRIAL_EXCLUDE = {
    "business_manufacturing", "industrial_equipment",
    "painting", "laundry_service", "warehouse",
    "commercial_industrial",
}


def _industrial_proximity(lat: float, lng: float) -> dict:
    """Count industrial/contamination-risk POIs within 500m using Overture."""
    try:
        db = get_db()
        pois = pois_near(db, lat, lng, radius_m=500)

        industrial: list[tuple[str, float]] = []
        for cat, dist_m in pois:
            if not cat:
                continue
            cat_lower = cat.lower()
            if any(ex in cat_lower for ex in INDUSTRIAL_EXCLUDE):
                continue
            if any(kw in cat_lower for kw in INDUSTRIAL_KEYWORDS):
                industrial.append((cat, dist_m))

        industrial.sort(key=lambda x: x[1])
        return {
            "count_500m": len(industrial),
            "nearest_m": round(industrial[0][1]) if industrial else None,
            "nearest_type": industrial[0][0] if industrial else None,
        }
    except Exception:
        return {"count_500m": 0, "nearest_m": None, "nearest_type": None}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _epa_to_score(sites: list[dict]) -> int:
    """Convert EPA sites to a score component."""
    if not sites:
        return 95

    nearest = sites[0]["distance_m"]
    count = len(sites)

    if nearest < 100:
        return 10
    if nearest < 250:
        return 25
    if nearest < 500 and count > 2:
        return 30
    if nearest < 500:
        return 45
    if nearest < 1000 and count > 3:
        return 50
    if nearest < 1000:
        return 65
    if nearest < 2000:
        return 80

    return 90


def _industrial_to_score(ind: dict) -> int:
    """Convert industrial proximity to a score component."""
    count = ind["count_500m"]
    nearest = ind["nearest_m"]

    if count == 0:
        return 95

    if nearest is not None and nearest < 100 and count > 3:
        return 30
    if nearest is not None and nearest < 100:
        return 45
    if count > 5:
        return 40
    if count > 3:
        return 55
    if count > 1:
        return 70

    return 80


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------

def contamination_score(lat: float, lng: float) -> dict:
    """Compute contamination risk score for an Australian coordinate.

    Combines official EPA registers (VIC/NSW/WA) with industrial POI
    proximity from Overture data for national coverage.
    """
    state = _detect_state(lat, lng)
    if state is None:
        return {
            "score": None,
            "label": "Outside Australia",
            "state": None,
            "epa_sites": [],
            "industrial": {},
        }

    # --- Phase 1: Official EPA registers ---
    epa_sites: list[dict] = []
    if state == "VIC":
        epa_sites = _vic_epa_sites(lat, lng)
    elif state == "NSW":
        epa_sites = _nsw_epa_sites(lat, lng)
    elif state == "WA":
        epa_sites = _wa_epa_sites(lat, lng)

    epa_score = _epa_to_score(epa_sites) if epa_sites or state in ("VIC", "NSW", "WA") else None

    # --- Phase 2: Industrial POI proximity ---
    industrial = _industrial_proximity(lat, lng)
    ind_score = _industrial_to_score(industrial)

    # --- Combine ---
    if epa_score is not None:
        score = min(epa_score, ind_score)
    else:
        score = ind_score

    score = max(0, min(100, score))

    if score >= 90:
        label = "Very Clean"
    elif score >= 70:
        label = "Clean"
    elif score >= 50:
        label = "Low Risk"
    elif score >= 30:
        label = "Moderate Risk"
    elif score >= 15:
        label = "High Risk"
    else:
        label = "Very High Risk"

    result: dict = {
        "score": score,
        "label": label,
        "disclaimer": "Estimate based on EPA registers and POI proximity. Not a substitute for site contamination assessment.",
        "state": state,
        "epa_sites_count": len(epa_sites),
        "industrial": industrial,
    }
    if epa_sites:
        result["nearest_epa_site"] = epa_sites[0]
    if not epa_sites and state not in ("VIC", "NSW", "WA"):
        result["note"] = f"No EPA register API for {state}. Score based on industrial POI proximity only."

    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Compute contamination risk score")
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lng", type=float, required=True)
    args = parser.parse_args()

    result = contamination_score(args.lat, args.lng)
    print(f"Contamination: {result['score']}/100 ({result['label']})")
    print(f"State: {result['state']}")
    if result.get("nearest_epa_site"):
        s = result["nearest_epa_site"]
        print(f"Nearest EPA site: {s['name']} ({s['distance_m']}m) — {s['source']}")
    print(f"EPA sites within 2km: {result['epa_sites_count']}")
    ind = result["industrial"]
    print(f"Industrial POIs 500m: {ind['count_500m']}" +
          (f" (nearest: {ind['nearest_type']} at {ind['nearest_m']}m)" if ind['nearest_m'] else ""))
