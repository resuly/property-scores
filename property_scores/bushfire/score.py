"""
Bushfire risk score combining planning overlays + satellite vegetation/slope.

Three complementary signals:
1. ArcGIS REST overlays — official bushfire-prone zones (VIC/NSW/WA/SA/TAS)
2. ESA WorldCover 10m — land cover / vegetation fuel load (global COG)
3. Copernicus DEM 30m — terrain slope for fire spread (global COG)

Score 0-100 where 100 = lowest bushfire risk.
"""

import logging
import math
import time as _time

import numpy as np
import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ArcGIS REST endpoints per state
# ---------------------------------------------------------------------------
VIC_PLAN_BASE = (
    "https://plan-gis.mapshare.vic.gov.au/arcgis/rest/services"
    "/Planning/Vicplan_PlanningSchemeOverlays/MapServer"
)

SA_PLAN_BASE = (
    "https://location.sa.gov.au/server6/rest/services"
    "/ePlanningPublic/CurrentPDC_wmas/MapServer"
)

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

NSW_CATEGORY_MAP = {
    "Vegetation Category 1": "extreme",
    "Vegetation Category 2": "high",
    "Vegetation Category 3": "moderate",
    "Vegetation Buffer": "low",
}

TIMEOUT = 10

# ---------------------------------------------------------------------------
# ESA WorldCover — fuel load mapping
# ---------------------------------------------------------------------------
# ESA classes → bushfire fuel risk (0-1)
FUEL_RISK: dict[int, tuple[float, str]] = {
    10:  (0.95, "Tree cover"),
    20:  (0.80, "Shrubland"),
    30:  (0.60, "Grassland"),
    40:  (0.30, "Cropland"),
    50:  (0.10, "Built-up"),
    60:  (0.25, "Bare/sparse"),
    70:  (0.00, "Snow/ice"),
    80:  (0.00, "Water"),
    90:  (0.40, "Wetland"),
    95:  (0.50, "Mangroves"),
    100: (0.15, "Moss/lichen"),
}

PC_STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
PC_SIGN = "https://planetarycomputer.microsoft.com/api/sas/v1/sign"

_signed_cache: dict[str, tuple[str, float]] = {}


# ---------------------------------------------------------------------------
# State detection
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# ArcGIS overlay queries
# ---------------------------------------------------------------------------

def _query_arcgis(url: str, lat: float, lng: float,
                  *, where: str | None = None) -> dict | None:
    params = {
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326", "outSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "returnCountOnly": "false",
        "f": "json",
    }
    if where:
        params["where"] = where
    try:
        resp = requests.get(f"{url}/query", params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return None if "error" in data else data
    except (requests.RequestException, ValueError):
        return None


def _check_layer(state: str, layer_name: str, url: str, severity: str,
                 lat: float, lng: float) -> tuple[str | None, str | None]:
    where = None
    if state == "TAS":
        where = "O_NAME LIKE '%ush%ire%' OR O_NAME LIKE '%Bush Fire%'"

    data = _query_arcgis(url, lat, lng, where=where)
    if not data or not data.get("features"):
        return None, None

    attrs = data["features"][0].get("attributes", {})

    if state == "NSW":
        cat = attrs.get("d_Category", "")
        return NSW_CATEGORY_MAP.get(cat, severity), cat

    if state == "TAS":
        o_name = attrs.get("O_NAME", "")
        if "bush" not in o_name.lower() and "fire" not in o_name.lower():
            return None, None
        return severity, o_name

    detail = attrs.get("ZONE_CODE") or attrs.get("classvalue") or layer_name
    return severity, str(detail)


def _overlay_check(state: str, lat: float, lng: float) -> tuple[str | None, list[str], str | None]:
    """Returns (worst_severity, hit_zones, worst_category)."""
    layers = ENDPOINTS.get(state)
    if not layers:
        return None, [], None

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
            if state == "SA":
                break

    return worst_severity, hits, worst_category


# ---------------------------------------------------------------------------
# Satellite data helpers (Planetary Computer COG)
# ---------------------------------------------------------------------------

def _get_signed(href: str) -> str | None:
    now = _time.time()
    if href in _signed_cache:
        url, ts = _signed_cache[href]
        if now - ts < 3000:
            return url
    try:
        resp = requests.get(PC_SIGN, params={"href": href}, timeout=10)
        if resp.ok:
            signed = resp.json().get("href")
            _signed_cache[href] = (signed, now)
            return signed
    except requests.RequestException:
        pass
    return None


def _stac_find(collection: str, lat: float, lng: float, asset_key: str = "map") -> str | None:
    """Find and sign a COG URL via STAC for a given coordinate."""
    buf = 0.01
    try:
        resp = requests.post(f"{PC_STAC}/search", json={
            "collections": [collection],
            "bbox": [lng - buf, lat - buf, lng + buf, lat + buf],
            "limit": 3,
        }, timeout=15)
        if not resp.ok:
            return None
        items = resp.json().get("features", [])
        if not items:
            return None
        href = items[0].get("assets", {}).get(asset_key, {}).get("href")
        return _get_signed(href) if href else None
    except (requests.RequestException, ValueError, KeyError):
        return None


def _vegetation_fuel(lat: float, lng: float) -> dict | None:
    """Read ESA WorldCover land cover class and map to fuel risk.

    Samples a 3x3 grid at ~100m to get dominant cover around the point.
    """
    signed = _stac_find("esa-worldcover", lat, lng, "map")
    if not signed:
        return None
    try:
        import rasterio
        step = 0.001
        points = [(lng + dx * step, lat + dy * step)
                  for dy in (-1, 0, 1) for dx in (-1, 0, 1)]

        with rasterio.open(signed) as ds:
            values = [int(v[0]) for v in ds.sample(points)]

        # Use the mode (most common class)
        from collections import Counter
        counts = Counter(values)
        dominant_class = counts.most_common(1)[0][0]
        fuel, label = FUEL_RISK.get(dominant_class, (0.3, "Unknown"))

        # Also compute surrounding diversity for mixed vegetation
        unique_classes = set(values)
        has_trees = any(v == 10 for v in values)

        return {
            "land_cover_class": dominant_class,
            "land_cover_label": label,
            "fuel_risk": round(fuel, 2),
            "has_nearby_trees": has_trees,
        }
    except Exception as e:
        logger.debug("WorldCover read failed: %s", e)
        return None


def _terrain_slope(lat: float, lng: float) -> dict | None:
    """Read COP DEM 30m and compute average slope in a ~300m window."""
    signed = _stac_find("cop-dem-glo-30", lat, lng, "data")
    if not signed:
        return None
    try:
        import rasterio
        from rasterio.windows import from_bounds

        buf = 0.003  # ~300m
        with rasterio.open(signed) as ds:
            window = from_bounds(lng - buf, lat - buf, lng + buf, lat + buf,
                                 ds.transform)
            dem = ds.read(1, window=window)
            if dem.size < 9:
                return None

            dy_m = ds.res[0] * 111320
            dx_m = ds.res[1] * 111320 * math.cos(math.radians(lat))
            grad_y, grad_x = np.gradient(dem.astype(float), dy_m, dx_m)
            slopes = np.degrees(np.arctan(np.sqrt(grad_x ** 2 + grad_y ** 2)))

            center_y, center_x = dem.shape[0] // 2, dem.shape[1] // 2
            return {
                "slope_deg": round(float(slopes[center_y, center_x]), 1),
                "mean_slope_deg": round(float(slopes.mean()), 1),
                "max_slope_deg": round(float(slopes.max()), 1),
                "elevation_m": round(float(dem[center_y, center_x]), 0),
            }
    except Exception as e:
        logger.debug("COP DEM read failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# MODIS fire history (burned area detection)
# ---------------------------------------------------------------------------

PROJ_DATA_PATH = None


def _ensure_proj():
    """Set PROJ_DATA to rasterio's bundled proj.db if needed for WarpedVRT."""
    global PROJ_DATA_PATH
    if PROJ_DATA_PATH:
        return
    import os
    if "PROJ_DATA" not in os.environ:
        candidate = os.path.join(
            os.path.dirname(os.path.dirname(__import__("rasterio").__file__)),
            "rasterio", "proj_data",
        )
        if os.path.isdir(candidate):
            os.environ["PROJ_DATA"] = candidate
            PROJ_DATA_PATH = candidate


def _fire_history(lat: float, lng: float) -> dict | None:
    """Check MODIS burned area products for fire history within 10km.

    Searches the last 5 fire seasons (Australian summer: Oct-Mar).
    Returns count of fire seasons with nearby burns.
    """
    _ensure_proj()
    try:
        import rasterio
        from rasterio.vrt import WarpedVRT
        from rasterio.windows import from_bounds

        seasons_with_fire = 0
        total_burned_pixels = 0
        checked_seasons = 0
        t_start = _time.time()

        for year in range(2024, 2021, -1):  # 3 seasons max (was 5)
            if _time.time() - t_start > 15:  # hard timeout
                break
            try:
                resp = requests.post(f"{PC_STAC}/search", json={
                    "collections": ["modis-64A1-061"],
                    "bbox": [lng - 0.5, lat - 0.5, lng + 0.5, lat + 0.5],
                    "datetime": f"{year-1}-10-01/{year}-03-31",
                    "limit": 6,  # fewer tiles (was 20)
                }, timeout=8)
            except requests.RequestException:
                continue
            if not resp.ok:
                continue

            items = resp.json().get("features", [])
            season_burned = 0

            for item in items[:3]:  # max 3 tiles per season
                if _time.time() - t_start > 15:
                    break
                href = item.get("assets", {}).get("Burn_Date", {}).get("href")
                if not href:
                    continue
                signed = _get_signed(href)
                if not signed:
                    continue

                try:
                    with rasterio.open(signed) as src:
                        with WarpedVRT(src, crs="EPSG:4326") as vrt:
                            buf = 0.05
                            window = from_bounds(
                                lng - buf, lat - buf, lng + buf, lat + buf,
                                vrt.transform,
                            )
                            data = vrt.read(1, window=window)
                            import numpy as np
                            burned = int(np.count_nonzero((data > 0) & (data < 367)))
                            season_burned += burned
                except Exception:
                    continue

            if season_burned > 0:
                seasons_with_fire += 1
                total_burned_pixels += season_burned
            checked_seasons += 1

        if checked_seasons == 0:
            return None

        return {
            "seasons_with_fire": seasons_with_fire,
            "total_seasons_checked": checked_seasons,
            "total_burned_pixels": total_burned_pixels,
        }
    except Exception as e:
        logger.debug("Fire history query failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Satellite scoring
# ---------------------------------------------------------------------------

def _satellite_to_score(veg: dict | None, slope: dict | None,
                        fire: dict | None = None) -> int | None:
    """Compute a bushfire risk score from vegetation + slope + fire history."""
    if veg is None and slope is None:
        return None

    fuel_risk = veg["fuel_risk"] if veg else 0.3
    slope_deg = slope["mean_slope_deg"] if slope else 5.0

    if slope_deg >= 25:
        slope_factor = 1.0
    elif slope_deg >= 15:
        slope_factor = 0.7
    elif slope_deg >= 8:
        slope_factor = 0.4
    elif slope_deg >= 3:
        slope_factor = 0.2
    else:
        slope_factor = 0.1

    combined = fuel_risk * 0.7 + slope_factor * 0.3
    score = max(0, min(100, round((1 - combined) * 100)))

    # Fire history penalty: areas that have burned recently are higher risk
    if fire and fire["seasons_with_fire"] > 0:
        seasons = fire["seasons_with_fire"]
        if seasons >= 3:
            score = min(score, 15)
        elif seasons >= 2:
            score = min(score, 25)
        else:
            score = min(score, max(score - 15, 20))

    return score


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------

def bushfire_score(lat: float, lng: float) -> dict:
    """Compute bushfire risk score for an Australian coordinate.

    Combines official planning overlays with satellite-derived vegetation
    and terrain data for full Australia coverage.
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

    # --- Phase 1: ArcGIS overlays ---
    worst_severity, hits, worst_category = _overlay_check(state, lat, lng)

    overlay_score: int | None = None
    if worst_severity:
        lo, hi = SEVERITY_SCORES[worst_severity]
        overlay_score = round((lo + hi) / 2)
    elif ENDPOINTS.get(state):
        overlay_score = 90

    # --- Phase 2: Satellite (vegetation + slope + conditional fire history) ---
    veg = _vegetation_fuel(lat, lng)
    slope = _terrain_slope(lat, lng)

    # Skip expensive fire history for low-fuel areas (urban, water, cropland)
    fire = None
    if veg and veg["fuel_risk"] >= 0.4:
        fire = _fire_history(lat, lng)

    sat_score = _satellite_to_score(veg, slope, fire)

    # --- Combine ---
    if overlay_score is not None and sat_score is not None:
        score = min(overlay_score, sat_score)
    elif overlay_score is not None:
        score = overlay_score
    elif sat_score is not None:
        score = sat_score
    else:
        score = 85

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

    result: dict = {
        "score": score,
        "label": label,
        "disclaimer": "Estimate based on open data. Not equivalent to a BAL (Bushfire Attack Level) assessment.",
        "bushfire_zones": hits,
        "state": state,
        "category": worst_category,
    }
    if veg:
        result["vegetation"] = veg
    if slope:
        result["slope"] = slope
    if fire:
        result["fire_history"] = fire

    return result


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
    if result.get('vegetation'):
        v = result['vegetation']
        print(f"Vegetation: {v['land_cover_label']} (fuel={v['fuel_risk']})")
    if result.get('slope'):
        s = result['slope']
        print(f"Slope: {s['slope_deg']}° (mean={s['mean_slope_deg']}°, max={s['max_slope_deg']}°)")
