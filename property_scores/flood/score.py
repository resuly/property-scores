"""
Flood risk score combining overlays + JRC satellite + HAND elevation.

Three complementary signals:
1. ArcGIS REST overlays — official planning zones (VIC/NSW/SA/TAS/ACT)
2. JRC Global Surface Water — 38-year satellite water occurrence (global 30m)
3. HAND (Height Above Nearest Drainage) — physical flood vulnerability (30m COG)

Score 0-100 where 100 = lowest risk / safest.
"""

import logging
import math

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State bounding boxes (approximate, WGS84)
# ACT is checked first since it sits inside NSW bounds.
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

# ---------------------------------------------------------------------------
# ArcGIS REST endpoints per state
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

SEVERITY_SCORES: dict[str, tuple[int, int]] = {
    "floodway": (10, 20),
    "flood":    (20, 40),
    "moderate": (40, 60),
}

# ---------------------------------------------------------------------------
# JRC Global Surface Water — Planetary Computer COG tiles
# ---------------------------------------------------------------------------
JRC_TILE_URL = (
    "https://ai4edataeuwest.blob.core.windows.net/jrcglobalwater"
    "/occurrence/occurrence_{tile}v1_3_2020cog.tif"
)
PC_SIGN = "https://planetarycomputer.microsoft.com/api/sas/v1/sign"

# Australia tiles: 10x10 degree grid
AU_TILES = [
    f"{lng}E_{lat}S"
    for lng in (110, 120, 130, 140, 150)
    for lat in (10, 20, 30, 40)
]

TIMEOUT = 10

_jrc_signed_cache: dict[str, tuple[str, float]] = {}


# ---------------------------------------------------------------------------
# Helpers — ArcGIS
# ---------------------------------------------------------------------------

def _detect_state(lat: float, lng: float) -> str | None:
    for state, min_lat, max_lat, min_lng, max_lng in STATE_BOUNDS:
        if min_lat <= lat <= max_lat and min_lng <= lng <= max_lng:
            return state
    return None


def _query_layer(url: str, lat: float, lng: float,
                 *, where: str | None = None,
                 count_only: bool = False) -> dict | None:
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
    data = _query_layer(url, lat, lng, where=where, count_only=True)
    if data is None:
        return None
    count = data.get("count")
    if count is not None:
        return count > 0
    features = data.get("features")
    if features is not None:
        return len(features) > 0
    return None


def _overlay_check(state: str, lat: float, lng: float) -> tuple[str | None, list[str], list[str]]:
    """Check ArcGIS overlays. Returns (worst_severity, hit_zones, warnings)."""
    layers = ENDPOINTS.get(state)
    if not layers:
        return None, [], []

    hit_zones: list[str] = []
    worst_severity: str | None = None
    warnings: list[str] = []
    severity_rank = {"floodway": 0, "flood": 1, "moderate": 2}

    for layer_name, url, severity in layers:
        where = "O_NAME LIKE '%Flood%'" if state == "TAS" else None
        result = _layer_has_features(url, lat, lng, where=where)

        if result is None:
            warnings.append(f"Could not reach {layer_name}")
            continue
        if result:
            hit_zones.append(layer_name)
            if worst_severity is None or severity_rank.get(severity, 99) < severity_rank.get(worst_severity, 99):
                worst_severity = severity

    return worst_severity, hit_zones, warnings


# ---------------------------------------------------------------------------
# Helpers — JRC Global Surface Water
# ---------------------------------------------------------------------------

def _jrc_tile_for(lat: float, lng: float) -> str | None:
    """Return JRC tile name for a coordinate.

    Tiles are named by upper-left corner: 140E_30S covers -30 to -40 lat.
    """
    tile_lng = int(math.floor(lng / 10) * 10)
    tile_lat = int(math.floor(abs(lat) / 10) * 10)
    if tile_lng < 0 or tile_lat < 0:
        return None
    tile = f"{tile_lng}E_{tile_lat}S"
    return tile if tile in AU_TILES else None


def _get_signed_url(tile: str) -> str | None:
    """Get or cache a Planetary Computer signed URL (valid ~1 hour)."""
    import time
    now = time.time()
    if tile in _jrc_signed_cache:
        cached_url, cached_at = _jrc_signed_cache[tile]
        if now - cached_at < 3000:
            return cached_url

    raw_url = JRC_TILE_URL.format(tile=tile)
    try:
        resp = requests.get(PC_SIGN, params={"href": raw_url}, timeout=10)
        if resp.ok:
            signed = resp.json().get("href")
            _jrc_signed_cache[tile] = (signed, now)
            return signed
    except requests.RequestException:
        pass
    return None


def _jrc_flood_proximity(lat: float, lng: float) -> dict | None:
    """Sample JRC water occurrence in a grid around the point.

    Returns dict with max_occurrence, nearest_water_m, mean_occurrence.
    Samples 500m radius at ~100m steps (11x11 grid = 121 points).
    """
    tile = _jrc_tile_for(lat, lng)
    if not tile:
        return None

    signed_url = _get_signed_url(tile)
    if not signed_url:
        return None

    try:
        import rasterio

        step = 0.001  # ~111m
        half = 5
        points = []
        for dy in range(-half, half + 1):
            for dx in range(-half, half + 1):
                points.append((lng + dx * step, lat + dy * step))

        with rasterio.open(signed_url) as ds:
            values = [v[0] for v in ds.sample(points)]

        m_per_deg = 111_320 * math.cos(math.radians(lat))
        max_occ = 0
        nearest_water_m = None
        occ_sum = 0
        occ_count = 0
        flood_cells = 0  # cells with 1-90% occurrence (not permanent water)

        for i, val in enumerate(values):
            val = int(val)
            # JRC uses 0=never water, 1-100=occurrence %, 255=nodata
            if val < 1 or val > 100:
                continue
            occ_sum += val
            occ_count += 1
            if val <= 90:
                flood_cells += 1
            if val > max_occ:
                max_occ = val
            dy = (i // 11) - half
            dx = (i % 11) - half
            dist_m = math.sqrt((dx * step * m_per_deg) ** 2 +
                               (dy * step * 111320) ** 2)
            if nearest_water_m is None or dist_m < nearest_water_m:
                nearest_water_m = dist_m

        return {
            "max_occurrence_pct": max_occ,
            "nearest_water_m": round(nearest_water_m) if nearest_water_m is not None else None,
            "wet_cells": occ_count,
            "flood_cells": flood_cells,
            "total_cells": len(values),
            "mean_occurrence_pct": round(occ_sum / occ_count, 1) if occ_count else 0,
        }

    except Exception as e:
        logger.debug("JRC query failed: %s", e)
        return None


def _jrc_to_score(jrc: dict) -> int:
    """Convert JRC flood proximity data to a 0-100 score component.

    Distinguishes permanent water (>90% occurrence = rivers/lakes/bays) from
    actual flood evidence (1-90% occurrence = areas that sometimes flood).

    A few flood cells near a river is normal (water-level fluctuation).
    Many flood cells or flood cells away from permanent water = real risk.
    """
    nearest = jrc["nearest_water_m"]
    flood_cells = jrc["flood_cells"]  # 1-90% occurrence only
    wet_cells = jrc["wet_cells"]

    if wet_cells == 0:
        return 95

    if flood_cells == 0:
        if nearest is not None and nearest < 200:
            return 70
        return 85

    # flood_ratio: how much of the wet area is actual flood vs permanent water
    # High ratio = flood plain; low ratio = river edge noise
    flood_ratio = flood_cells / max(wet_cells, 1)

    # Many flood cells = clear flood plain
    if flood_cells >= 10:
        if nearest is not None and nearest < 200:
            return 15
        return 30

    # Moderate flood cells with high ratio (mostly flood, not river)
    if flood_cells >= 5 and flood_ratio > 0.5:
        if nearest is not None and nearest < 250:
            return 25
        return 40

    # Few flood cells near water — river edge or minor risk
    if flood_cells >= 5:
        return 55

    # 1-4 flood cells: typical river-edge noise, mild risk
    if flood_ratio > 0.7 and nearest is not None and nearest < 200:
        return 55

    return 75


# ---------------------------------------------------------------------------
# ERA5 P95 extreme rainfall (pre-computed grid)
# ---------------------------------------------------------------------------

_p95_grid = None


def _load_p95_grid():
    global _p95_grid
    if _p95_grid is not None:
        return _p95_grid
    from property_scores.common.config import data_path
    p = data_path("era5_rainfall_p95.parquet")
    if not p.exists():
        _p95_grid = []
        return _p95_grid
    try:
        import pandas as pd
        df = pd.read_parquet(p)
        valid = df.dropna(subset=["p95_mm"])
        _p95_grid = list(valid[["lat", "lng", "p95_mm", "p99_mm"]].itertuples(index=False))
        return _p95_grid
    except Exception:
        _p95_grid = []
        return _p95_grid


def _query_p95(lat: float, lng: float) -> dict | None:
    """Find nearest P95/P99 rainfall from pre-computed grid (within 2 degrees)."""
    grid = _load_p95_grid()
    if not grid:
        return None
    best_dist = 999.0
    best = None
    for glat, glng, p95, p99 in grid:
        d = math.sqrt((glat - lat) ** 2 + (glng - lng) ** 2)
        if d < best_dist:
            best_dist = d
            best = (p95, p99)
    if best_dist > 2.0:
        return None
    return {"p95_mm": best[0], "p99_mm": best[1], "grid_dist_deg": round(best_dist, 1)}


# ---------------------------------------------------------------------------
# HAND (Height Above Nearest Drainage) — AWS S3 COG
# ---------------------------------------------------------------------------
HAND_URL = "https://glo-30-hand.s3.amazonaws.com/v1/2021/Copernicus_DSM_COG_10_{tile}_HAND.tif"


def _hand_tile_for(lat: float, lng: float) -> str:
    """Return HAND tile name. Tiles are 1x1 degree, named by upper-left corner."""
    tile_lat = math.ceil(abs(lat))
    tile_lng = math.floor(lng)
    ns = "S" if lat < 0 else "N"
    return f"{ns}{tile_lat:02d}_00_E{tile_lng:03d}_00"


def _query_hand(lat: float, lng: float) -> dict | None:
    """Read HAND value from AWS GLO-30 COG. Returns height in meters above nearest drainage."""
    tile = _hand_tile_for(lat, lng)
    url = HAND_URL.format(tile=tile)
    try:
        import rasterio
        with rasterio.open(url) as ds:
            val = list(ds.sample([(lng, lat)]))[0][0]
            if val < 0 or val > 9000:
                return None
            return {"hand_m": round(float(val), 1)}
    except Exception as e:
        logger.debug("HAND query failed: %s", e)
        return None


def _hand_to_score(hand_m: float) -> int:
    """Convert HAND value to a flood risk score component."""
    if hand_m < 1:
        return 15
    if hand_m < 3:
        return 30
    if hand_m < 5:
        return 50
    if hand_m < 10:
        return 70
    if hand_m < 20:
        return 85
    return 95


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------

def flood_score(lat: float, lng: float) -> dict:
    """Compute flood risk score for a coordinate.

    Combines official planning overlays (where available) with JRC satellite
    water occurrence data for full Australia coverage.

    Returns:
        dict with score (0-100), label, flood_zones, state, jrc data.
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

    # --- Phase 1: ArcGIS overlays (official data) ---
    worst_severity, hit_zones, warnings = _overlay_check(state, lat, lng)

    overlay_score: int | None = None
    if worst_severity is not None:
        lo, hi = SEVERITY_SCORES[worst_severity]
        zone_penalty = min(len(hit_zones) - 1, 3) * 3
        overlay_score = max(lo, hi - zone_penalty)
    elif not ENDPOINTS.get(state):
        overlay_score = None
    else:
        overlay_score = 90 if not warnings else 80

    # --- Phase 2: JRC satellite data ---
    jrc = _jrc_flood_proximity(lat, lng)
    jrc_score: int | None = None
    if jrc:
        jrc_score = _jrc_to_score(jrc)

    # --- Phase 3: HAND (physical elevation above drainage) ---
    hand = _query_hand(lat, lng)

    # --- Phase 4: ERA5 P95 extreme rainfall ---
    p95 = _query_p95(lat, lng)

    # --- Combine ---
    # Overlay + JRC determine base risk; HAND modifies it
    base_scores = [s for s in (overlay_score, jrc_score) if s is not None]
    if base_scores:
        score = min(base_scores)
    else:
        score = 85

    # HAND adjustment: modifies score based on physical elevation
    has_flood_evidence = bool(base_scores and min(base_scores) < 80)
    if hand:
        hand_m = hand["hand_m"]
        if hand_m < 2 and has_flood_evidence:
            # Low + flood evidence = confirmed risk
            score = min(score, 55) - max(0, int((2 - hand_m) * 10))
        elif hand_m < 2:
            # Low but no flood evidence = physically exposed but not proven
            score = min(score, 70)
        elif hand_m < 5 and has_flood_evidence:
            score = min(score, 65)
        elif hand_m < 5:
            # Near drainage without satellite evidence — mild caution
            score = min(score, 80)
        elif hand_m > 20:
            # Well above drainage — boost confidence
            score = max(score, min(score + 10, 95))

    # P95 rainfall modifier: high extreme rainfall + other risk = compound
    if p95 and has_flood_evidence and p95["p95_mm"] > 25:
        score = max(0, score - 5)

    score = max(0, min(100, score))

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
        "disclaimer": "Estimate based on open data. Not a substitute for professional flood assessment or insurance evaluation.",
        "flood_zones": hit_zones,
        "state": state,
        "zone_count": len(hit_zones),
    }
    if jrc:
        result_dict["jrc"] = jrc
    if hand:
        result_dict["hand"] = hand
    if p95:
        result_dict["p95"] = p95
    if warnings:
        result_dict["warnings"] = warnings
    if not ENDPOINTS.get(state) and not jrc:
        result_dict["note"] = (
            f"No overlay data for {state} and JRC query failed. "
            "Score is a default estimate."
        )

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
        print(f"Overlay zones: {', '.join(result['flood_zones'])}")
    if result.get("jrc"):
        jrc = result["jrc"]
        print(f"JRC: max {jrc['max_occurrence_pct']}% occurrence, "
              f"{jrc['wet_cells']}/{jrc['total_cells']} wet cells, "
              f"nearest water {jrc['nearest_water_m']}m")
    if result.get("note"):
        print(f"Note: {result['note']}")
    if result.get("warnings"):
        print(f"Warnings: {'; '.join(result['warnings'])}")
