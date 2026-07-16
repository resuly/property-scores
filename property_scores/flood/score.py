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
        # SAPPA backend; old server6 service deleted upstream (2026-06-11).
        # Requests to geohub need the SAPPA Referer (handled in _query_layer).
        ("Hazards (Flooding)",
         "https://lsa2.geohub.sa.gov.au/arcgis/rest/services"
         "/SAPPA/PropertyPlanningAtlasV18/MapServer/141",
         "flood"),
        ("Hazards (Flooding - General)",
         "https://lsa2.geohub.sa.gov.au/arcgis/rest/services"
         "/SAPPA/PropertyPlanningAtlasV18/MapServer/372",
         "moderate"),
        ("Coastal Flooding",
         "https://lsa2.geohub.sa.gov.au/arcgis/rest/services"
         "/SAPPA/PropertyPlanningAtlasV18/MapServer/367",
         "flood"),
    ],
    "TAS": [
        # Statewide overlay layer (14). Layer 3 is the Kingborough Interim
        # Planning Scheme only, so the old config returned zero polygons for
        # the entire state (Invermay 1929: 4,000 homeless, scored Low Risk).
        # CODE_NO 12 = "Flood-prone Hazard Areas Code" (16,960 polygons).
        ("Flood-prone Hazard Areas",
         "https://services.thelist.tas.gov.au/arcgis/rest/services"
         "/Public/PlanningOnline/MapServer/14",
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
    """Shared border-true state detection (common.au_state).

    The old private overlapping-bbox copy routed southern inland NSW
    (Albury, Wagga, Goulburn, Griffith, Cooma) into VIC, so those towns
    were checked against the wrong state register (2026-06-11 audit).
    """
    from property_scores.common.au_state import detect_state
    return detect_state(lat, lng)


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
        headers = ({"Referer": "https://sappa.plan.sa.gov.au/"}
                   if "geohub.sa.gov.au" in url else None)
        resp = requests.get(f"{url}/query", params=params, timeout=TIMEOUT,
                            headers=headers)
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
        where = "CODE_NO = 12" if state == "TAS" else None
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

    UNUSED / DEAD CODE (2026-07-02 audit): flood_score() uses
    _water_proximity_local() (local Overture water), NOT this remote JRC path.
    Do NOT cite "JRC satellite flood (38yr)" as a live data source in docs or
    marketing. Kept for reference only.

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
# BOM 2016 IFD design rainfall (pre-computed grid; scripts/precompute_bom_ifd.py)
#
# Replaces the ERA5-via-Open-Meteo P95 climatology: BOM IFD is the ARR 2019
# national-standard design rainfall (true 1% AEP), CC BY 4.0 so it is clean
# for the commercial API (the Open-Meteo free tier is non-commercial ToS).
# Attribution: Bureau of Meteorology, (c) Commonwealth of Australia, CC BY 4.0.
# ---------------------------------------------------------------------------

_ifd_grid = None


def _load_ifd_grid():
    global _ifd_grid
    if _ifd_grid is not None:
        return _ifd_grid
    from property_scores.common.config import data_path
    p = data_path("bom_ifd_1pct.parquet")
    if not p.exists():
        _ifd_grid = []
        return _ifd_grid
    try:
        import pandas as pd
        df = pd.read_parquet(p)
        valid = df.dropna(subset=["ifd_1pct_1h_mm"])
        _ifd_grid = list(valid[["lat", "lng", "ifd_1pct_1h_mm",
                                "ifd_1pct_6h_mm"]].itertuples(index=False))
        return _ifd_grid
    except Exception:
        _ifd_grid = []
        return _ifd_grid


def _query_ifd(lat: float, lng: float) -> dict | None:
    """Nearest 1% AEP design-rainfall depths from the grid (within 2 degrees)."""
    grid = _load_ifd_grid()
    if not grid:
        return None
    best_dist = 999.0
    best = None
    for glat, glng, mm_1h, mm_6h in grid:
        d = math.sqrt((glat - lat) ** 2 + (glng - lng) ** 2)
        if d < best_dist:
            best_dist = d
            best = (mm_1h, mm_6h)
    if best_dist > 2.0:
        return None
    return {"ifd_1pct_1h_mm": best[0], "ifd_1pct_6h_mm": best[1],
            "grid_dist_deg": round(best_dist, 1), "source": "BOM 2016 IFD (CC BY 4.0)"}


# ---------------------------------------------------------------------------
# Local data alternatives (replace remote COG calls)
# ---------------------------------------------------------------------------

def _water_proximity_local(lat: float, lng: float) -> dict | None:
    """Replace JRC satellite query with local Overture water data.

    Maps water proximity + type to JRC-equivalent output format so the
    existing _jrc_to_score() function works unchanged.
    """
    try:
        from property_scores.common.overture import get_db, water_near

        db = get_db()
        waters = water_near(db, lat, lng, radius_m=500)
        if not waters:
            return {
                "max_occurrence_pct": 0,
                "nearest_water_m": None,
                "wet_cells": 0,
                "flood_cells": 0,
                "total_cells": 121,
                "mean_occurrence_pct": 0,
            }

        flood_types = {"river", "stream", "drain", "canal", "wetland"}
        permanent_types = {"ocean", "sea", "bay", "lake", "reservoir"}

        nearest_m = waters[0][2]
        flood_waters = [w for w in waters if w[0] in flood_types]
        permanent_waters = [w for w in waters if w[0] in permanent_types]

        if flood_waters:
            nearest_flood = min(w[2] for w in flood_waters)
            flood_cells = max(1, min(15, int(10 * (1 - nearest_flood / 500))))
        else:
            flood_cells = 0

        if permanent_waters:
            max_occ = 95
        elif flood_waters:
            max_occ = min(80, max(10, int(80 * (1 - nearest_m / 500))))
        else:
            max_occ = 0

        wet_cells = len([w for w in waters if w[2] < 500])

        return {
            "max_occurrence_pct": max_occ,
            "nearest_water_m": round(nearest_m),
            "wet_cells": wet_cells,
            "flood_cells": flood_cells,
            "total_cells": 121,
            "mean_occurrence_pct": round(max_occ * 0.6) if max_occ > 0 else 0,
        }
    except Exception as e:
        logger.debug("Local water proximity failed: %s", e)
        return None


# HAND elevation source -> vertical-accuracy tier for elevation_confidence.
# high = survey-grade LiDAR (~5m raster or 1m contour); medium = coarser LiDAR
# contour or the 30m DEM-H; low = proxy (no elevation coverage).
_ELEV_CONFIDENCE = {
    "lidar_5m_local": "high",   # baked national 5 m LiDAR VRT (bare-earth)
    "lidar_5m": "high",
    "lidar_contour_1m": "high",
    "contour_med": "medium",
    "dem_relief": "medium",
}


def _hand_from_elev(lat: float, lng: float, elev, source: str,
                    uncertain_thresh: float) -> dict | None:
    """HAND (Height Above Nearest Drainage) from an elevation sampler `elev`.

    HAND = the point's height above the local drainage line. We approximate the
    drainage as the lowest elevation in a ring around the point (rivers/creeks
    sit at the local minimum) and take point_elev - that minimum. `elev(lat,lng)`
    returns metres or None; source-agnostic so DEM-H 30 m and on-demand LiDAR
    share one ring geometry. `uncertain_thresh` is the local relief below which a
    read is within the source's vertical noise (deferred to overlay/JRC): ~5 m
    for GLO-30/DEM-H, ~1 m for LiDAR (its whole value is trusting low relief near
    water). None if the point or the whole ring is outside coverage.
    """
    pt = elev(lat, lng)
    if pt is None:
        return None

    coslat = max(0.2, math.cos(math.radians(lat)))

    def _ring(r_m: float, n: int) -> list:
        dlat = r_m / 111320.0
        dlng = r_m / (111320.0 * coslat)
        out = []
        for i in range(n):
            a = 2 * math.pi * i / n
            e = elev(lat + dlat * math.sin(a), lng + dlng * math.cos(a))
            if e is not None:
                out.append(e)
        return out

    # Nearest-ring-first: drainage = lowest point in the NEAREST ring, so a
    # distant gorge/escarpment can't inflate HAND. Widen only if the nearest
    # ring is entirely outside coverage.
    samples = _ring(300, 16) or _ring(600, 12) or _ring(1000, 8)
    if not samples:
        return None

    drainage = min(min(samples), pt)
    relief = max(max(samples), pt) - drainage
    hand_m = max(0.0, round(pt - drainage, 1))
    return {
        "hand_m": hand_m,
        "point_elev_m": round(pt, 1),
        "drainage_elev_m": round(drainage, 1),
        "relief_m": round(relief, 1),
        "uncertain": relief < uncertain_thresh,
        "source": source,
    }


def _hand_local(lat: float, lng: float, state: str | None = None) -> dict | None:
    """HAND from the best available elevation, cheapest-trusted first.

    1. On-demand LiDAR where a state publishes it open — NSW/QLD raster
       ImageServers or VIC/TAS contour services; one fetch feeds the whole ring.
       Sub-5 m bare-earth, so low relief near water is trusted (confidence high;
       VIC/TAS at 5 m contour -> medium). Skipped/None outside coverage or on a
       timeout/failure, so live scoring never blocks on the remote service.
    2. Local GA DEM-H 30 m (data/global/dem.vrt, on disk) — confidence = medium.
    3. Overture water/building proxy outside DEM tile coverage — confidence = low.
    """
    # 0. Baked national 5 m LiDAR VRT (offline, high confidence). Nodata-gated,
    #    so outside the footprint it returns None and we fall through to the
    #    live state services / DEM-H. Preferred: no network, deterministic.
    try:
        from property_scores.common import lidar_local
        if lidar_local.available():
            h = _hand_from_elev(lat, lng, lidar_local.elevation,
                                "lidar_5m_local", 1.0)
            if h is not None:
                return h
    except Exception as e:
        logger.debug("local LiDAR HAND failed, falling back: %s", e)

    # 1. On-demand LiDAR (survey-grade where covered, network) — fallback for any
    #    gap the baked VRT doesn't cover yet (e.g. captures after the 2015 mosaic).
    if state:
        try:
            from property_scores.flood import lidar
            if lidar.covered(state):
                win = lidar.open_window(lat, lng, state)
                if win is not None:
                    try:
                        h = _hand_from_elev(lat, lng, win.elev,
                                            win.source, win.uncertain_thresh)
                    finally:
                        win.close()
                    if h is not None:
                        return h
        except Exception as e:
            logger.debug("LiDAR HAND failed, falling back: %s", e)

    # 2. Local DEM-H 30 m
    try:
        from property_scores.common import terrain
        if terrain.available():
            h = _hand_from_elev(lat, lng, terrain.elevation, "dem_relief", 5.0)
            if h is not None:
                return h
    except Exception as e:
        logger.debug("DEM HAND failed, falling back to proxy: %s", e)

    # 3. Proxy outside DEM tile coverage
    return _hand_local_proxy(lat, lng)


def _hand_local_proxy(lat: float, lng: float) -> dict | None:
    """Fallback HAND from Overture water distance + building density.

    Used only outside DEM tile coverage. HAND = height above nearest drainage:
    - Close to river/stream with few buildings → low HAND (floodplain)
    - Close to river with dense buildings → moderate HAND (developed, likely raised)
    - Far from any drainage → high HAND
    """
    try:
        from property_scores.common.overture import get_db, water_near, buildings_near

        db = get_db()
        waters = water_near(db, lat, lng, radius_m=1000)
        drainage_types = {"river", "stream", "drain", "canal"}
        drainages = [w for w in waters if w[0] in drainage_types]

        if not drainages:
            return {"hand_m": 20.0}

        nearest_drain_m = min(w[2] for w in drainages)
        buildings = buildings_near(db, lat, lng, radius_m=300)
        building_count = len(buildings)

        if nearest_drain_m < 100:
            hand_m = 0.5 if building_count < 5 else 2.0
        elif nearest_drain_m < 300:
            hand_m = 2.0 if building_count < 5 else 5.0
        elif nearest_drain_m < 500:
            hand_m = 5.0 if building_count < 10 else 8.0
        elif nearest_drain_m < 1000:
            hand_m = 10.0
        else:
            hand_m = 20.0

        return {"hand_m": round(hand_m, 1)}
    except Exception as e:
        logger.debug("Local HAND approximation failed: %s", e)
        return None


def inundation_grid(lat: float, lng: float, radius_m: int = 500) -> dict | None:
    """DEM elevation window around a point, relative to the local drainage
    minimum — the data behind the map's water-level simulation overlay.

    Uses the same drainage reference as _hand_local so the panel's "height
    above drainage" and the simulation agree: the query point goes under
    exactly when the simulated level passes hand_m. Terrain fill (bathtub)
    illustration only — not hydraulic modelling. None outside DEM coverage.
    """
    try:
        from property_scores.common import terrain
        from property_scores.common.landcover import sampler as _sampler

        if not terrain.available():
            return None
        hand = _hand_local(lat, lng)
        if not hand or hand.get("source") != "dem_relief":
            return None

        import numpy as np
        from rasterio.windows import transform as _win_transform

        rs = _sampler()
        src = rs._src(terrain.DEM_VRT)
        if src is None:
            return None
        x, y = rs._to_raster_xy(src, lat, lng)
        px = abs(src.transform.a)
        # Separate row/column half-widths: an equal-degree window covers
        # cos(lat) less ground east-west (same fix as landcover_grid).
        half_row = max(int((radius_m / 111_320.0) / px), 1)
        half_col = max(int((radius_m / (111_320.0 * max(
            math.cos(math.radians(lat)), 0.1))) / px), 1)
        row, col = src.index(x, y)
        r0, r1 = max(row - half_row, 0), row + half_row + 1
        c0, c1 = max(col - half_col, 0), col + half_col + 1
        arr = src.read(1, window=((r0, r1), (c0, c1))).astype(float)
        if arr.size == 0:
            return None

        wt = _win_transform(((r0, r1), (c0, c1)), src.transform)
        h, w = arr.shape
        west, north = wt.c, wt.f
        east = west + w * wt.a
        south = north + h * wt.e  # wt.e is negative
        # The DEM VRT has no nodata (gaps read back 0.0). Gate the window on
        # tile coverage at its corners; inside a covered 1-degree tile a gap
        # cannot occur (tiles are complete).
        for cla, cln in ((north, west), (north, east), (south, west), (south, east)):
            if not terrain.covered(cla, cln):
                return None

        rel = np.round(arr - float(hand["drainage_elev_m"]), 1)
        return {
            "bbox": [round(west, 6), round(south, 6), round(east, 6), round(north, 6)],
            "nrows": h,
            "ncols": w,
            "radius_m": radius_m,
            "cell_m": round(px * 111_320.0),
            "drainage_elev_m": hand["drainage_elev_m"],
            "point_hand_m": hand["hand_m"],
            "rel": rel.tolist(),
        }
    except Exception as e:
        logger.debug("inundation_grid failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------

def _hand_discounted_jrc(jrc_score: int | None, hand: dict | None) -> int | None:
    """Blend the JRC score toward neutral (95) as HAND rises through 10-20 m.

    Physics gate on classification evidence: a point 20 m+ above its drainage
    cannot flood from the water JRC sees nearby (or falsely sees in dark forest
    and terrain shadow). Guards reused from the HAND boost branch: uncertain
    DEM relief and the coastal 0 m drainage artefact leave JRC untouched, so
    genuine floodplains (hand < 10 m) keep the full satellite evidence.
    """
    if jrc_score is None or not hand:
        return jrc_score
    if hand.get("uncertain", False):
        return jrc_score
    if hand.get("drainage_elev_m", 0) <= 1.0:
        # Tidal/coastal drainage: the 0m artefact guard stays for sea-level
        # floodplains, but a clifftop well above any tide cannot flood from
        # the water below it (Kangaroo Point: 17m AHD above the Brisbane
        # River scored 25 High Risk, 2026-06-11 audit). 15m AHD clears any
        # storm surge + king tide combination by an order of magnitude.
        if (hand.get("point_elev_m") or 0) < 15.0:
            return jrc_score
    w = max(0.0, min(1.0, (hand["hand_m"] - 10.0) / 10.0))
    if w <= 0.0:
        return jrc_score
    return round(jrc_score * (1.0 - w) + 95 * w)


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

    # --- Phase 1: official overlays. Local layer library first (the SAME
    # library that serves the customer-visible hazards block, so score and
    # layers can never contradict; no live state-service dependency), remote
    # ArcGIS as fallback for states the library does not cover (SA/TAS) or
    # when the library file is unavailable (dev boxes).
    from property_scores.flood.local_overlays import check as _local_check

    local = _local_check(state, lat, lng)
    if local is not None:
        worst_severity, hit_zones, warnings = local["worst"], local["hit_zones"], []
        overlay_trust: str | None = local["trust"]
        overlay_basis: str | None = "local_library"
    else:
        worst_severity, hit_zones, warnings = _overlay_check(state, lat, lng)
        if not ENDPOINTS.get(state):
            overlay_trust, overlay_basis = None, None
        elif state == "NSW":
            # NSW layer 230 holds ~620 polygons across ~12 of 128 LGAs. A miss
            # there says nothing about Windsor or Lismore (both zero in it,
            # 2026-06-11 audit), so it cannot vouch a 90 "checked clean".
            overlay_trust, overlay_basis = "hit_only", "state_service"
        else:
            overlay_trust, overlay_basis = "full", "state_service"

    overlay_score: int | None = None
    if worst_severity is not None:
        lo, hi = SEVERITY_SCORES[worst_severity]
        zone_penalty = min(len(hit_zones) - 1, 3) * 3
        overlay_score = max(lo, hi - zone_penalty)
    elif overlay_trust == "full":
        # Statewide statutory overlay checked clean.
        overlay_score = 90 if not warnings else 80
    else:
        # hit_only coverage or no source: a miss proves nothing;
        # JRC + HAND carry the estimate instead.
        overlay_score = None

    # --- Phase 2: Water proximity (local Overture data) ---
    jrc = _water_proximity_local(lat, lng)
    jrc_score: int | None = None
    if jrc:
        jrc_score = _jrc_to_score(jrc)

    # --- Phase 3: HAND approximation (LiDAR where covered, else DEM-H) ---
    hand = _hand_local(lat, lng, state)

    # --- Phase 4: BOM IFD design rainfall (1% AEP) ---
    ifd = _query_ifd(lat, lng)

    # --- Combine ---
    # Overlay + JRC determine base risk; HAND modifies it.
    # JRC water occurrence is satellite classification, not physics: dense dark
    # forest and terrain shadow read as "water" (North Wahroonga hilltop: 49 wet
    # cells at hand 51.9 m scored 65 "Moderate"). When the point sits well above
    # any real drainage, surface flooding is physically impossible, so JRC
    # proximity loses its evidentiary standing: discounted from 10 m HAND,
    # fully neutral at 20 m+. Official overlays are NOT discounted (overland
    # flow paths can flag genuinely hilly lots).
    jrc_score = _hand_discounted_jrc(jrc_score, hand)
    base_scores = [s for s in (overlay_score, jrc_score) if s is not None]
    if base_scores:
        score = min(base_scores)
    else:
        score = 85

    # HAND adjustment: modifies score based on physical elevation
    has_flood_evidence = bool(base_scores and min(base_scores) < 80)
    if hand:
        hand_m = hand["hand_m"]
        # GLO-30 relief below ~5m is within DEM noise → unreliable HAND, defer to
        # overlay/JRC rather than (de)penalising on it.
        hand_uncertain = hand.get("uncertain", False)
        drainage_elev = hand.get("drainage_elev_m", 1.0)
        if hand_uncertain:
            pass
        elif hand_m < 2 and has_flood_evidence:
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
        elif hand_m > 20 and drainage_elev > 1.0:
            # Well above a real (non sea-level) drainage — boost confidence.
            # The drainage_elev gate blocks the coastal/bay 0m artefact from
            # turning a waterfront property's elevation into a false safety bonus.
            score = max(score, min(score + 10, 95))

    # Design-rainfall modifier: intense 1% AEP storms + other flood evidence
    # = compound risk. 70mm/1h is the upper-intensity band (Brisbane 98,
    # Sydney 59, Melbourne 49), so only genuinely storm-heavy locations with
    # independent flood evidence take the penalty.
    if ifd and has_flood_evidence and ifd["ifd_1pct_1h_mm"] > 70:
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
        result_dict["water_proximity"] = jrc
    if hand:
        result_dict["hand"] = hand
        # Vertical-accuracy tier of the elevation behind the HAND read, exposed
        # so the report/API can flag it: high = on-demand LiDAR (NSW/QLD, ~5m or
        # finer bare-earth), medium = GA DEM-H 30m, low = water/building proxy.
        result_dict["elevation_confidence"] = _ELEV_CONFIDENCE.get(
            hand.get("source"), "low")
    if ifd:
        result_dict["design_rainfall"] = ifd
    if warnings:
        result_dict["warnings"] = warnings
    # Transparency flags: whether an OFFICIAL flood overlay covered this
    # point, and which source answered. 'checked_partial_coverage' means the
    # available overlay library for this state only vouches hits (a miss says
    # nothing); the score then rests on satellite water proximity + HAND +
    # rainfall rather than authoritative mapping.
    result_dict["official_layer"] = (
        "hit" if hit_zones
        else "checked_no_hit" if overlay_trust == "full"
        else "checked_partial_coverage" if overlay_trust == "hit_only"
        else "none"
    )
    if overlay_basis:
        result_dict["overlay_basis"] = overlay_basis
    if overlay_trust is None and not jrc:
        result_dict["note"] = (
            f"No official flood overlay for {state} and no water-proximity "
            "signal. Score is a default estimate."
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
    if result.get("water_proximity"):
        jrc = result["water_proximity"]
        print(f"JRC: max {jrc['max_occurrence_pct']}% occurrence, "
              f"{jrc['wet_cells']}/{jrc['total_cells']} wet cells, "
              f"nearest water {jrc['nearest_water_m']}m")
    if result.get("note"):
        print(f"Note: {result['note']}")
    if result.get("warnings"):
        print(f"Warnings: {'; '.join(result['warnings'])}")
