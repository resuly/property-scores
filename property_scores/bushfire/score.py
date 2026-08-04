"""
Bushfire risk score combining planning overlays + satellite vegetation/slope.

Three complementary signals:
1. ArcGIS REST overlays — official bushfire-prone zones, 7 of 8 states:
   VIC/NSW/QLD/WA/SA/TAS/ACT (QLD = QFD 2017-vintage proxy, ACT = SBMP BPA;
   only the NT has none). Keep this list in sync with OVERLAY_ENDPOINTS below;
   a stale 5-state version of this line caused wrong public copy 2026-07-03.
2. ESA WorldCover 10m — land cover / vegetation fuel load (global COG)
3. Terrain slope for fire spread via common.terrain — national 5 m LiDAR
   bare-earth VRT where covered (~245,000 km2), 30 m DEM outside it: dem.vrt is
   GA DEM-H bare-earth for all 196 AU tiles since 2026-07-15 (the 25 Copernicus
   tiles left in it are EU/US training-region cells, never sampled for AU
   addresses). This line said "Copernicus DEM 30m" until 2026-08-04.

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

# SAPPA backend (PlanSA Planning & Design Code overlays). The old server6
# CurrentPDC_wmas was deleted upstream (HTTP 200 with an error body) and the
# whole state silently scored on proxies for weeks (2026-06-11 audit).
# CloudFront WAF on this host requires the SAPPA Referer header exactly.
SA_PLAN_BASE = (
    "https://lsa2.geohub.sa.gov.au/arcgis/rest/services"
    "/SAPPA/PropertyPlanningAtlasV18/MapServer"
)
SA_HEADERS = {"Referer": "https://sappa.plan.sa.gov.au/"}

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
        # Official severity ordering per the Planning & Design Code: High
        # Risk outranks Urban Interface (the old config had them inverted).
        ("Hazards (Bushfire - High Risk)",       f"{SA_PLAN_BASE}/135", "high"),
        ("Hazards (Bushfire - Urban Interface)", f"{SA_PLAN_BASE}/140", "moderate"),
        ("Hazards (Bushfire - Medium Risk)",     f"{SA_PLAN_BASE}/136", "moderate"),
        ("Hazards (Bushfire - General Risk)",    f"{SA_PLAN_BASE}/137", "low"),
        ("Hazards (Bushfire - Regional)",        f"{SA_PLAN_BASE}/139", "low"),
        ("Hazards (Bushfire - Outback)",         f"{SA_PLAN_BASE}/138", "low"),
    ],
    "TAS": [
        # Statewide overlay layer (14); layer 3 was Kingborough-only so the
        # whole state read "no overlay" (Fern Tree on kunanyi included).
        # CODE_NO 13 = "Bushfire-prone Areas Code".
        ("Bushfire-prone Areas",
         "https://services.thelist.tas.gov.au/arcgis/rest/services"
         "/Public/PlanningOnline/MapServer/14",
         "moderate"),
    ],
    # Verified 2026-06-11 (recon agents, point probes pasted in the audit log)
    "ACT": [
        # Binary in/out Strategic Bushfire Management Plan BPA (ACTmapi,
        # CC-BY). Chapman hits; 2003-firestorm Duffy core is officially
        # outside the CURRENT BPA, which is the honest answer.
        ("Bushfire Prone Area (ACT SBMP)",
         "https://services1.arcgis.com/E5n4f1VY84i0xSjy/arcgis/rest/services"
         "/SBMP_BPA_current/FeatureServer/0",
         "high"),
    ],
    "QLD": [
        # QFD Bushfire Prone Area public proxy (class attribute carries the
        # tier; vintage July 2017 per the service, named so the UI shows it).
        ("Bushfire Prone Area (QLD QFD 2017)",
         "https://utility.arcgis.com/usrsvcs/servers"
         "/8ac1ba8eccee472fbd0e7a57bf3ad320/rest/services/Hosted/BPA/FeatureServer/0",
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
    """Shared border-true state detection (common.au_state).

    The old private overlapping-bbox copy routed southern inland NSW
    (Albury, Wagga, Goulburn, Griffith, Cooma) into VIC, so those towns
    were checked against the wrong state register (2026-06-11 audit).
    """
    from property_scores.common.au_state import detect_state
    return detect_state(lat, lng)


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
        headers = SA_HEADERS if "geohub.sa.gov.au" in url else None
        resp = requests.get(f"{url}/query", params=params, timeout=TIMEOUT,
                            headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return None if "error" in data else data
    except (requests.RequestException, ValueError):
        return None


def _check_layer(state: str, layer_name: str, url: str, severity: str,
                 lat: float, lng: float) -> tuple[str | None, str | None, bool]:
    where = None
    if state == "TAS":
        where = "CODE_NO = 13"

    data = _query_arcgis(url, lat, lng, where=where)
    if data is None:
        # request failed: "we don't know" must never read as "officially clear"
        return None, None, False
    if not data.get("features"):
        return None, None, True

    attrs = data["features"][0].get("attributes", {})

    if state == "NSW":
        cat = attrs.get("d_Category", "")
        return NSW_CATEGORY_MAP.get(cat, severity), cat, True

    if state == "TAS":
        return severity, attrs.get("OV_NAME") or "Bushfire-prone areas", True

    if state == "QLD":
        cls = str(attrs.get("class") or attrs.get("CLASS") or "")
        low = cls.lower()
        if "very high" in low:
            sev = "high"
        elif "high" in low:
            sev = "moderate"
        elif "medium" in low:
            sev = "moderate"
        else:
            sev = "low"
        return sev, cls or layer_name, True

    if state == "ACT":
        return severity, "Bushfire Prone Area (ACT SBMP)", True

    detail = attrs.get("ZONE_CODE") or attrs.get("classvalue") or layer_name
    return severity, str(detail), True


def _overlay_check(state: str, lat: float, lng: float) -> tuple[str | None, list[str], str | None, bool, str | None]:
    """Returns (worst_severity, hit_zones, worst_category, all_queried_ok, basis).

    all_queried_ok is True only when every layer responded, so a timeout can
    never masquerade as "officially outside the mapped bushfire zones".

    Local layer library first (the same features.duckdb that serves the
    customer-visible hazards block, so score and layers cannot contradict;
    VIC/QLD/NSW/WA/SA/TAS as of 2026-07-06 — full-precision downloads of
    the same datasets the remote endpoints serve, classified identically,
    so values match and only the basis changes). Remote ArcGIS endpoints
    remain for ACT and as fallback whenever the library cannot answer.
    """
    from property_scores.flood.local_overlays import check_bushfire as _local_check

    local = _local_check(state, lat, lng)
    if local is not None:
        return (local["worst"], local["hit_zones"], local["category"],
                True, "local_library")

    layers = ENDPOINTS.get(state)
    if not layers:
        return None, [], None, False, None

    hits = []
    worst_severity = None
    worst_category = None
    all_ok = True
    severity_rank = {"extreme": 0, "high": 1, "moderate": 2, "low": 3}

    for layer_name, url, default_severity in layers:
        sev, detail, ok = _check_layer(state, layer_name, url, default_severity, lat, lng)
        if not ok:
            all_ok = False
        if sev:
            hits.append(layer_name)
            if worst_severity is None or severity_rank.get(sev, 99) < severity_rank.get(worst_severity, 99):
                worst_severity = sev
                worst_category = detail
            if state == "SA":
                break

    return worst_severity, hits, worst_category, all_ok, "state_service"


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


# ---------------------------------------------------------------------------
# ESA WorldCover 10m land cover (local tiles, same data the noise model uses)
# ---------------------------------------------------------------------------
import os as _os  # noqa: E402

from property_scores.common.config import data_path as _data_path  # noqa: E402

_LC_VRT = str(_data_path("global/lc.vrt"))
_LC_CLASSES = list(FUEL_RISK)

VEG_RADIUS_M = 200            # window around the point for fuel assessment
INTERFACE_WOODY_SLOPE = 2.5   # interface fuel floor = woody_frac * this ...
INTERFACE_FLOOR_CAP = 0.55    # ... capped here (full bushland still scores ~0.9)


def _raster_sampler():
    """Shared generic raster sampler (single process-wide instance via
    common.landcover, so bushfire / view-quality / heat-island don't each
    double-open the WorldCover handles per thread)."""
    from property_scores.common import landcover as _lc
    return _lc.sampler()


def lc_vrt_available() -> bool:
    """Whether the ESA WorldCover mosaic is present (else fuel degrades to proxy)."""
    return _os.path.exists(_LC_VRT)


def landcover_grid(lat: float, lng: float, radius_m: int = 500) -> dict | None:
    """Return the WorldCover class grid + bbox around a point for map display.

    Used by the bushfire map to render the actual 10m land cover surrounding a
    property (the visible basis for the fuel-load score). Returns None outside
    tile coverage.
    """
    try:
        rs = _raster_sampler()
        src = rs._src(_LC_VRT)
        if src is None:
            return None
        import numpy as np
        from rasterio.windows import transform as _win_transform

        x, y = rs._to_raster_xy(src, lat, lng)  # WorldCover is EPSG:4326 → x=lng, y=lat
        px = abs(src.transform.a)
        # Row and column half-widths separately: an equal-degree window covers
        # cos(lat) less ground east-west, which painted as an ellipse on the
        # map and under-covered the promised radius (Bo, 2026-06-12).
        import math
        half_row = max(int((radius_m / 111_320.0) / px), 1)
        half_col = max(int((radius_m / (111_320.0 * max(
            math.cos(math.radians(lat)), 0.1))) / px), 1)
        row, col = src.index(x, y)
        r0, r1 = max(row - half_row, 0), row + half_row + 1
        c0, c1 = max(col - half_col, 0), col + half_col + 1
        win = ((r0, r1), (c0, c1))
        arr = src.read(1, window=win)
        if arr.size == 0 or not np.any(arr > 0):
            return None

        wt = _win_transform(win, src.transform)
        h, w = arr.shape
        west, north = wt.c, wt.f
        east = west + w * wt.a
        south = north + h * wt.e  # wt.e is negative
        return {
            "bbox": [round(west, 6), round(south, 6), round(east, 6), round(north, 6)],
            "nrows": h,
            "ncols": w,
            "radius_m": radius_m,
            "classes": arr.astype(int).tolist(),
        }
    except Exception as e:
        logger.debug("landcover_grid failed: %s", e)
        return None


def _vegetation_fuel(lat: float, lng: float) -> dict | None:
    """Estimate vegetation fuel load from ESA WorldCover 10m land cover.

    Samples the local WorldCover mosaic in a window around the point and
    computes an area-weighted fuel risk from the actual land-cover mix. An
    urban-bushland interface floor lifts the risk where built-up area would
    otherwise dilute nearby woody vegetation. Falls back to the Overture
    building-density proxy outside tile coverage.
    """
    try:
        rs = _raster_sampler()
        frac = rs.window_stats(_LC_VRT, lat, lng, radius_m=VEG_RADIUS_M,
                               categorical=True, classes=_LC_CLASSES,
                               cos_correct=True)
    except Exception as e:
        logger.debug("WorldCover sampling failed: %s", e)
        frac = None

    if not frac:  # outside tile coverage / all nodata
        return _vegetation_fuel_proxy(lat, lng)

    fractions = {c: frac.get(f"frac_{c}", 0.0) for c in _LC_CLASSES}
    total = sum(fractions.values())
    if total <= 0:
        return _vegetation_fuel_proxy(lat, lng)

    # Area-weighted fuel risk over the window.
    area_weighted = sum(fractions[c] * FUEL_RISK[c][0] for c in _LC_CLASSES) / total

    woody = fractions[10] + fractions[20]   # tree + shrub = ember / radiant threat
    flammable = woody + fractions[30]       # + grassland

    # Urban-bushland interface: built-up dominant but real woody vegetation
    # nearby. Area-weighting alone under-calls the ember risk, so floor the
    # fuel up in proportion to how much woody vegetation is actually present.
    interface_floor = min(INTERFACE_FLOOR_CAP, woody * INTERFACE_WOODY_SLOPE)
    fuel_risk = round(min(max(area_weighted, interface_floor), 0.98), 3)

    dom = max(_LC_CLASSES, key=lambda c: fractions[c])
    if dom == 50 and woody >= 0.15:
        label = "Urban-bushland interface"
    else:
        label = FUEL_RISK[dom][1]

    return {
        "land_cover_class": dom,
        "land_cover_label": label,
        "fuel_risk": fuel_risk,
        "has_nearby_trees": fractions[10] >= 0.05,
        "source": "esa_worldcover_10m",
        "tree_shrub_frac": round(woody, 3),
        "vegetated_frac": round(flammable, 3),
    }


def _vegetation_fuel_proxy(lat: float, lng: float) -> dict | None:
    """Fallback: estimate vegetation fuel from local Overture data.

    Used only outside WorldCover tile coverage. Uses building density + water
    proximity as a coarse proxy for land cover: dense buildings = built-up
    (low fuel), no buildings = vegetation (high fuel).
    """
    try:
        from property_scores.common.overture import get_db, buildings_near, water_near

        db = get_db()
        buildings = buildings_near(db, lat, lng, radius_m=300)
        building_count = len(buildings)

        water = water_near(db, lat, lng, radius_m=300)
        near_water = any(w[0] in ("ocean", "sea", "river", "lake") for w in water)

        if near_water and building_count < 3:
            return {"land_cover_class": 80, "land_cover_label": "Water/wetland",
                    "fuel_risk": 0.05, "has_nearby_trees": False}
        if building_count >= 20:
            return {"land_cover_class": 50, "land_cover_label": "Built-up (dense)",
                    "fuel_risk": 0.10, "has_nearby_trees": False}
        if building_count >= 10:
            return {"land_cover_class": 50, "land_cover_label": "Built-up (suburban)",
                    "fuel_risk": 0.20, "has_nearby_trees": True}
        if building_count >= 3:
            return {"land_cover_class": 20, "land_cover_label": "Semi-rural (scattered buildings)",
                    "fuel_risk": 0.55, "has_nearby_trees": True}
        if building_count >= 1:
            return {"land_cover_class": 20, "land_cover_label": "Rural fringe",
                    "fuel_risk": 0.75, "has_nearby_trees": True}
        return {"land_cover_class": 10, "land_cover_label": "Bushland (no buildings)",
                "fuel_risk": 0.95, "has_nearby_trees": True}
    except Exception as e:
        logger.debug("Local vegetation estimation failed: %s", e)
        return None


def _terrain_slope(lat: float, lng: float) -> dict | None:
    """Slope from the local terrain stack via common.terrain.elevation().

    NOTE (2026-07-26): terrain.elevation() prefers the baked national 5 m LiDAR
    bare-earth VRT (common.lidar_local, ~245,000 km2 populated footprint) and only
    falls back to the 30 m dem.vrt outside it (wired 2026-07-16, commit
    b0f6509); that VRT has been GA DEM-H bare-earth for every AU tile since
    2026-07-15 (its leftover Copernicus tiles are EU/US training-region cells).
    A stale "Copernicus" label here almost failed one asset-claim audit and DID
    mislead a second (2026-08-04). Read common/terrain.py, not this line, for
    the ruler.

    Replaces the per-state government contour REST endpoints, which were slow,
    intermittent, and by 2026-07 partly dead (QLD ContoursQLD returned
    "Service not found", NSW Planning_Portal_Hazard returned 0 features, so
    NSW/QLD silently fell to not_assessed while VIC/TAS depended on flaky remote
    calls). The local DEM covers every downloaded populated region uniformly with
    no external dependency and gives every state a real slope. Samples elevation
    at the point plus an 8-point ring (~250m radius) and derives slope from the
    elevation range across the window. Returns None outside DEM tile coverage so
    the caller flags slope_assessment=not_assessed rather than a fake flat 0.
    """
    try:
        from property_scores.common import terrain
        e0 = terrain.elevation(lat, lng)
        if e0 is None:
            return None
        r = 0.0023  # ~250m in latitude degrees
        coslat = max(math.cos(math.radians(lat)), 0.1)
        diag = r * 0.7071
        offsets = [
            (r, 0.0), (-r, 0.0), (0.0, r / coslat), (0.0, -r / coslat),
            (diag, diag / coslat), (diag, -diag / coslat),
            (-diag, diag / coslat), (-diag, -diag / coslat),
        ]
        elevs = [e0]
        for dla, dln in offsets:
            e = terrain.elevation(lat + dla, lng + dln)
            if e is not None:
                elevs.append(e)
        if len(elevs) < 3:
            return None
        distance_m = r * 2 * 111320
        mean_slope = math.degrees(math.atan((max(elevs) - min(elevs)) / distance_m))
        return {
            "slope_deg": round(mean_slope, 1),
            "mean_slope_deg": round(mean_slope, 1),
            "max_slope_deg": round(mean_slope * 1.5, 1),
            "elevation_m": round(e0),
        }
    except Exception as e:
        logger.debug("DEM slope failed: %s", e)
        return None


def _fire_history_local(state: str | None, lat: float, lng: float) -> dict | None:
    """Query state fire history APIs (same sources as DA Leads tiles).

    VIC: DEECA fire_history_scar WFS (87k+ records since 1903)
    NSW: RFS Wild Fire History ArcGIS REST
    """
    import datetime as _dt
    window_start = _dt.date.today().year - 30  # risk window: last 30 seasons
    recent_start = _dt.date.today().year - 10

    try:
        if state == "VIC":
            buf = 0.02  # ~2km
            url = (
                "https://opendata.maps.vic.gov.au/geoserver/wfs"
                f"?service=WFS&version=2.0.0&request=GetFeature"
                f"&typeNames=open-data-platform:fire_history_scar"
                f"&outputFormat=application/json"
                f"&BBOX={lng-buf},{lat-buf},{lng+buf},{lat+buf},EPSG:4326"
                f"&count=100&propertyName=firetype,season,area_ha"
            )
            resp = requests.get(url, timeout=8)
            if not resp.ok:
                return None
            data = resp.json()
            features = data.get("features", [])
            # 30-year window: the unfiltered source goes back to 1903, so a
            # 1927+1939+1962 history capped a town at 15 while Black Summer
            # suburbs (frozen 2017 source) showed nothing (2026-06-11 audit,
            # perfect inversion). Old ash is not a current-risk signal.
            seasons = sorted({f["properties"].get("season") for f in features
                              if f["properties"].get("firetype") == "Bushfire"
                              and (f["properties"].get("season") or 0) >= window_start})
            recent = [f for f in features
                      if (f["properties"].get("season") or 0) >= recent_start]
            return {
                "seasons_with_fire": len(seasons),
                "window_years": 30,
                "last_fire_season": str(seasons[-1]) if seasons else None,
                "total_fires": len(features),
                "recent_fires": len(recent),
            }

        if state == "NSW":
            # NPWS Fire History (wildfires + prescribed burns): includes Black
            # Summer. The old CrownLands source froze at YEAROFFIRE=2017, so
            # Gospers Mountain did not exist and burned-out Bilpin read
            # "No recorded fires" (2026-06-11 audit).
            nsw_url = (
                "https://mapprod3.environment.nsw.gov.au/arcgis/rest/services"
                "/Fire/NPWS_Fire_History/MapServer/0/query"
            )
            buf = 0.02
            resp = requests.get(nsw_url, params={
                "geometry": f"{lng-buf},{lat-buf},{lng+buf},{lat+buf}",
                "geometryType": "esriGeometryEnvelope",
                "inSR": "4326", "outSR": "4326",
                "where": "FireType = 1",
                "outFields": "FireName,FireYear,Label,AreaHa",
                "returnGeometry": "false", "f": "json",
            }, timeout=10)
            if not resp.ok:
                return None
            data = resp.json()
            features = data.get("features", [])
            if not features:
                return {"seasons_with_fire": 0, "window_years": 30,
                        "last_fire_season": None, "total_fires": 0,
                        "recent_fires": 0}
            def _season_start(fy):
                # FireYear is a season code like 201920 -> 2019
                try:
                    fy = int(fy)
                except (TypeError, ValueError):
                    return None
                return fy // 100 if fy > 99999 else fy

            seasons = sorted({y for y in
                              (_season_start(f["attributes"].get("FireYear"))
                               for f in features)
                              if y and y >= window_start})
            recent = [1 for f in features
                      if (_season_start(f["attributes"].get("FireYear")) or 0)
                      >= recent_start]
            labels = [f["attributes"].get("Label") for f in features
                      if f["attributes"].get("Label")]
            return {
                "seasons_with_fire": len(seasons),
                "window_years": 30,
                "last_fire_season": f"{seasons[-1]}-{(seasons[-1] + 1) % 100:02d}" if seasons else None,
                "total_fires": len(features),
                "recent_fires": len(recent),
                "largest_label": max(labels, default=None),
            }
    except Exception as e:
        logger.debug("Local fire history failed: %s", e)
    return None


def _terrain_slope_fallback(lat: float, lng: float) -> dict | None:
    """No contour endpoint for this state: slope cannot be measured.

    Previously returned a fake flat slope 0 (= lowest fire-spread factor),
    silently treating real hillsides as flat. Now returns None so scoring uses
    a neutral mid slope and the caller flags slope_assessment=not_assessed.
    """
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

    UNUSED / DEAD CODE (2026-07-02 audit): bushfire_score() calls
    _fire_history_local() (VIC/NSW only), NOT this remote MODIS path. Do NOT
    cite "MODIS fire history (8 states)" as a live data source in docs or
    marketing. Kept for reference only.

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

        for year in range(2024, 2022, -1):  # 2 seasons max
            if _time.time() - t_start > 8:  # hard timeout
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

# Officially outside the mapped bushfire prone land: satellite fuel can pull
# the score down to this floor (a fuel-proximity caution) but can no longer
# call the lot High/Very High against the official determination. The NSW BPL
# map already encodes vegetation extent rules (>1 ha contiguous hazard
# vegetation + buffers), which is exactly what raw WorldCover fuel cannot see
# (2026-06-10: "High bushfire risk, but outside of council fire zone").
_OFFICIAL_CLEAR_FLOOR = 55

# In-zone floors per official severity: the official map already grades the
# hazard, so satellite fuel may position a lot below its band midpoint but
# must not crater the mildest categories beneath the band itself. Without
# this, a "Vegetation Buffer" hit (officially the lightest category, band
# 50-65) fell to min(58, sat~20) = 20 while the lot across the polygon edge
# floored at 55: a 35-point cliff that inverts the official severity order
# (2026-06-12: Ku-ring-gai buffer-edge, fuel 0.893 in-buffer vs 0.845 outside).
_SEVERITY_FLOORS = {
    "extreme": 5,
    "high": 15,
    "moderate": 30,
    "low": 45,
}


def _combine_scores(overlay_score: int | None, sat_score: int | None,
                    overlay_clear: bool,
                    worst_severity: str | None = None) -> int:
    """min(official, satellite), except an official all-clear floors the
    satellite-only pessimism at _OFFICIAL_CLEAR_FLOOR and an official zone
    hit floors at its severity's band floor (_SEVERITY_FLOORS)."""
    if overlay_score is not None and sat_score is not None:
        score = min(overlay_score, sat_score)
        if overlay_clear:
            score = max(score, _OFFICIAL_CLEAR_FLOOR)
        elif worst_severity in _SEVERITY_FLOORS:
            score = max(score, _SEVERITY_FLOORS[worst_severity])
    elif overlay_score is not None:
        score = overlay_score
    elif sat_score is not None:
        score = sat_score
    else:
        score = 85
    return max(0, min(100, score))


def bushfire_score(lat: float, lng: float, *, quick: bool = False) -> dict:
    """Compute bushfire risk score for an Australian coordinate.

    Combines official planning overlays with satellite-derived vegetation
    and terrain data for full Australia coverage.
    """
    if not quick:
        from property_scores.bushfire.cache import get as cache_get, put as cache_put
        cached = cache_get(lat, lng)
        if cached:
            return cached

    state = _detect_state(lat, lng)
    if not state:
        return {
            "score": None,
            "label": "Outside Australia",
            "bushfire_zones": [],
            "state": None,
            "category": None,
        }

    # --- All phases in parallel ---
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=4) as pool:
        f_overlay = pool.submit(_overlay_check, state, lat, lng)
        f_veg = None if quick else pool.submit(_vegetation_fuel, lat, lng)
        f_slope = None if quick else pool.submit(_terrain_slope, lat, lng)
        f_fire = None if quick else pool.submit(_fire_history_local, state, lat, lng)

    worst_severity, hits, worst_category, overlay_ok, overlay_basis = f_overlay.result()

    overlay_score: int | None = None
    if worst_severity:
        lo, hi = SEVERITY_SCORES[worst_severity]
        overlay_score = round((lo + hi) / 2)
    elif overlay_basis and overlay_ok:
        overlay_score = 90
    # Mapped state (local library or state service), every layer answered, no
    # zone hit = the official mapping says this lot is OUTSIDE bushfire prone
    # land. A failed query never counts as clear (overlay_ok gate).
    overlay_clear = worst_severity is None and overlay_ok and overlay_basis is not None

    veg = f_veg.result() if f_veg else None
    slope = f_slope.result() if f_slope else None
    fire = f_fire.result() if f_fire else None

    sat_score = _satellite_to_score(veg, slope, fire)

    # --- Combine ---
    score = _combine_scores(overlay_score, sat_score, overlay_clear,
                            worst_severity)

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
        "disclaimer": ("Estimate based on open data. The indicative BAL is a "
                       "pre-screen, not a certified Bushfire Attack Level "
                       "assessment (which requires an accredited assessor on site)."),
        "bushfire_zones": hits,
        "state": state,
        "category": worst_category,
        # outside | in_zone | unavailable: what the OFFICIAL state mapping
        # says, so the UI can show "Outside mapped bushfire prone land"
        # instead of leaving a fuel-based score to speak for the government.
        "official_zone_status": ("in_zone" if hits else
                                 "outside" if overlay_clear else "unavailable"),
    }
    if overlay_basis:
        result["overlay_basis"] = overlay_basis
    if veg:
        result["vegetation"] = veg
    if slope:
        result["slope"] = slope
        result["slope_assessment"] = "measured"
    elif not quick:
        # Non-quick call with no slope = no contour endpoint for this state
        # (SA/WA/ACT/NT) or the query failed. Scoring used a neutral mid slope,
        # not a measured flat 0.
        result["slope_assessment"] = "not_assessed"
    if fire:
        result["fire_history"] = fire

    # Indicative BAL (AS 3959 Method 1 pre-screen). Reuses the overlay + slope +
    # state already computed above, so it adds only one land-cover window read.
    # This is the domain-language layer on top of the 0-100 risk score: designers
    # and buyers in bushfire-prone areas think in BAL, not an abstract number.
    if not quick:
        try:
            from property_scores.bal_prescreen import bal_prescreen
            bal = bal_prescreen(
                lat, lng, state=state,
                elevation=slope.get("elevation_m") if slope else None,
                slope_deg=slope.get("mean_slope_deg") if slope else None,
                overlay=(worst_severity, hits, worst_category, overlay_ok, overlay_basis),
            )
            if bal.get("indicative_bal"):
                result["indicative_bal"] = {
                    "bal": bal["indicative_bal"],
                    "range": bal.get("bal_range"),
                    "confidence": bal.get("confidence"),
                    "fdi": bal.get("fdi"),
                    "fdi_basis": bal.get("fdi_basis"),
                    "inputs": bal.get("inputs"),
                    "method": bal.get("method"),
                }
        except Exception:
            logger.exception("indicative BAL computation failed")

    if not quick:
        cache_put(lat, lng, result)

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
