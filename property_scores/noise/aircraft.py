"""
Aircraft noise penalty from national ANEF data.

Three data layers, queried by state:
1. VIC — VicPlan ArcGIS (MAEO/AEO overlays, Melbourne + regional airports)
2. NSW — ePlanning SEPP layer 280 (Western Sydney Airport)
3. QLD — Brisbane Open Data API (Brisbane + Archerfield airports)
4. WA  — SLIP MapServer layer 77 (Perth Airport SPP 5.1)
5. ALL — Defence ANEF GeoJSON (14 military airfields nationally, via DuckDB)

ANEF → dB mapping (AS2021-2015):
  ANEF 15  ~  52 dB Leq(24h)
  ANEF 20  ~  55 dB Leq(24h) — noticeable
  ANEF 25  ~  60 dB Leq(24h) — significant
  ANEF 30  ~  65 dB Leq(24h) — severe
  ANEF 35  ~  70 dB Leq(24h) — extreme
  ANEF 40  ~  75 dB Leq(24h) — airport vicinity
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any

import requests

from property_scores.common.au_state import detect_state
from property_scores.common.config import data_path

logger = logging.getLogger(__name__)

_TIMEOUT = 10

# ---------------------------------------------------------------------------
# ANEF → penalty mapping
# ---------------------------------------------------------------------------

def _anef_to_penalty(anef_min: int) -> float:
    if anef_min >= 40:
        return 15.0
    if anef_min >= 35:
        return 12.0
    if anef_min >= 30:
        return 10.0
    if anef_min >= 25:
        return 7.0
    if anef_min >= 20:
        return 5.0
    if anef_min >= 15:
        return 3.0
    return 0.0


def _anef_impact(anef_min: int) -> str:
    if anef_min >= 30:
        return "Severe aircraft noise. Only commercial/industrial recommended."
    if anef_min >= 25:
        return "Significant aircraft noise. Residential not recommended."
    if anef_min >= 20:
        return "Moderate aircraft noise. Acoustic treatment required for dwellings."
    if anef_min >= 15:
        return "Noticeable aircraft noise."
    return "Minimal aircraft noise impact."


# ---------------------------------------------------------------------------
# VIC — VicPlan MAEO/AEO (existing, enhanced)
# ---------------------------------------------------------------------------

_VICPLAN_BASE = (
    "https://plan-gis.mapshare.vic.gov.au/arcgis/rest/services"
    "/Planning/Vicplan_PlanningSchemeOverlays/MapServer"
)

_VIC_ZONE_ANEF = {
    "MAEO1": 25, "MAEO2": 20,
    "AEO1": 25, "AEO2": 20, "AEO": 20,
}


def _query_vic(lat: float, lng: float) -> dict | None:
    for layer in (27, 22):  # MAEO, AEO
        url = f"{_VICPLAN_BASE}/{layer}"
        try:
            resp = requests.get(f"{url}/query", params={
                "geometry": f"{lng},{lat}",
                "geometryType": "esriGeometryPoint",
                "inSR": "4326",
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": "ZONE_CODE,LGA",
                "f": "json",
                "returnGeometry": "false",
            }, timeout=_TIMEOUT)
            if not resp.ok:
                continue
            features = resp.json().get("features", [])
            for feat in features:
                zone = feat["attributes"].get("ZONE_CODE", "")
                anef = _VIC_ZONE_ANEF.get(zone)
                if anef:
                    return {
                        "anef_min": anef,
                        "zone_code": zone,
                        "airfield": "Melbourne Airport" if "MAEO" in zone else "Regional Airport",
                        "source": "vicplan",
                        "lga": feat["attributes"].get("LGA"),
                    }
        except (requests.RequestException, ValueError, KeyError):
            continue
    return None


# ---------------------------------------------------------------------------
# NSW — ePlanning SEPP (Western Sydney Airport)
# ---------------------------------------------------------------------------

_NSW_SEPP_URL = (
    "https://mapprod3.environment.nsw.gov.au/arcgis/rest/services"
    "/ePlanning/Planning_Portal_SEPP/MapServer/280"
)


def _query_nsw(lat: float, lng: float) -> dict | None:
    try:
        resp = requests.get(f"{_NSW_SEPP_URL}/query", params={
            "geometry": f"{lng},{lat}",
            "geometryType": "esriGeometryPoint",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "ANEF_CODE,LAY_NAME",
            "f": "json",
        }, timeout=_TIMEOUT)
        if not resp.ok:
            return None
        features = resp.json().get("features", [])
        if not features:
            return None
        attrs = features[0]["attributes"]
        anef_code = attrs.get("ANEF_CODE", "")
        # Parse "20 - 25" → 20
        import re
        match = re.search(r"(\d+)", anef_code)
        anef_min = int(match.group(1)) if match else 20
        return {
            "anef_min": anef_min,
            "zone_code": anef_code,
            "airfield": "Western Sydney Airport",
            "source": "nsw_sepp",
        }
    except (requests.RequestException, ValueError, KeyError):
        return None


# ---------------------------------------------------------------------------
# QLD — Brisbane Open Data
# ---------------------------------------------------------------------------

_QLD_BNE_URL = (
    "https://data.brisbane.qld.gov.au/api/explore/v2.1/catalog/datasets"
    "/cp14-airport-environs-overlay-australian-noise-exposure-forecast-anef"
    "/exports/geojson"
)

_qld_cache: list | None = None


def _load_qld_data() -> list:
    global _qld_cache
    if _qld_cache is not None:
        return _qld_cache
    try:
        resp = requests.get(_QLD_BNE_URL, timeout=15)
        if resp.ok:
            _qld_cache = resp.json().get("features", [])
            return _qld_cache
    except (requests.RequestException, ValueError):
        pass
    _qld_cache = []
    return _qld_cache


def _point_in_polygon(lat: float, lng: float, coords: list) -> bool:
    """Ray casting point-in-polygon test."""
    ring = coords[0] if coords and isinstance(coords[0][0], list) else coords
    n = len(ring)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and (lng < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _query_qld(lat: float, lng: float) -> dict | None:
    features = _load_qld_data()
    best_anef = 0
    best_desc = ""
    for feat in features:
        geom = feat.get("geometry", {})
        if geom.get("type") != "Polygon":
            continue
        coords = geom.get("coordinates", [])
        if _point_in_polygon(lat, lng, coords):
            props = feat.get("properties", {})
            desc = props.get("ovl2_desc", "")
            import re
            match = re.search(r"(\d+)", desc)
            anef = int(match.group(1)) if match else 20
            if anef > best_anef:
                best_anef = anef
                best_desc = props.get("description", "Brisbane/Archerfield Airport")
    if best_anef == 0:
        return None
    return {
        "anef_min": best_anef,
        "zone_code": f"ANEF {best_anef}+",
        "airfield": best_desc or "Brisbane Airport",
        "source": "qld_bcc",
    }


# ---------------------------------------------------------------------------
# WA — SLIP Perth Airport
# ---------------------------------------------------------------------------

_WA_SLIP_URL = (
    "https://public-services.slip.wa.gov.au/public/rest/services"
    "/SLIP_Public_Services/Property_and_Planning/MapServer/77"
)


def _query_wa(lat: float, lng: float) -> dict | None:
    try:
        resp = requests.get(f"{_WA_SLIP_URL}/query", params={
            "geometry": f"{lng},{lat}",
            "geometryType": "esriGeometryPoint",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "feature_type,feature_name",
            "f": "json",
        }, timeout=_TIMEOUT)
        if not resp.ok:
            return None
        features = resp.json().get("features", [])
        if not features:
            return None
        attrs = features[0]["attributes"]
        ft = attrs.get("feature_type", "20")
        anef_min = int(ft) if ft.isdigit() else 20
        return {
            "anef_min": anef_min,
            "zone_code": attrs.get("feature_name", f"ANEF {anef_min}"),
            "airfield": "Perth Airport",
            "source": "wa_slip",
        }
    except (requests.RequestException, ValueError, KeyError):
        return None


# ---------------------------------------------------------------------------
# Defence ANEF (national, GeoJSON + DuckDB spatial)
# ---------------------------------------------------------------------------

_defence_loaded = False
_defence_features: list = []


def _load_defence():
    global _defence_loaded, _defence_features
    if _defence_loaded:
        return
    _defence_loaded = True
    geojson_path = data_path("defence_anef.geojson")
    if not geojson_path.exists():
        return
    try:
        with open(geojson_path) as f:
            data = json.load(f)
        _defence_features = data.get("features", [])
    except Exception:
        pass


def _query_defence(lat: float, lng: float) -> dict | None:
    _load_defence()
    if not _defence_features:
        return None

    best_anef = 0
    best_airfield = ""
    for feat in _defence_features:
        props = feat.get("properties", {})
        geom = feat.get("geometry", {})
        coords = geom.get("coordinates", [])
        if not coords:
            continue
        if _point_in_polygon(lat, lng, coords):
            anef = props.get("anef_min", 20)
            if anef > best_anef:
                best_anef = anef
                best_airfield = props.get("airfield", "Defence Airfield")

    if best_anef == 0:
        return None
    return {
        "anef_min": best_anef,
        "zone_code": f"ANEF {best_anef}+",
        "airfield": best_airfield,
        "source": "defence",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def aircraft_noise_penalty(lat: float, lng: float) -> dict:
    """Query ANEF zones from all national data sources.

    Checks state-specific civilian overlays first, then Defence ANEF.
    Returns the worst (highest penalty) match.
    """
    state = detect_state(lat, lng)

    results: list[dict] = []

    # State-specific civilian queries
    if state == "VIC":
        hit = _query_vic(lat, lng)
        if hit:
            results.append(hit)
    elif state == "NSW":
        hit = _query_nsw(lat, lng)
        if hit:
            results.append(hit)
    elif state == "QLD":
        hit = _query_qld(lat, lng)
        if hit:
            results.append(hit)
    elif state == "WA":
        hit = _query_wa(lat, lng)
        if hit:
            results.append(hit)

    # Defence (all states)
    defence = _query_defence(lat, lng)
    if defence:
        results.append(defence)

    if not results:
        return {
            "penalty_db": 0.0,
            "zone_code": None,
            "zone_desc": "Outside airport noise overlay",
            "anef_min": None,
            "anef_max": None,
            "lga": None,
            "impact": "No aircraft noise overlay applies to this location.",
            "source": "national",
            "airport_type": None,
        }

    # Take worst (highest ANEF)
    worst = max(results, key=lambda r: r["anef_min"])
    anef = worst["anef_min"]
    penalty = _anef_to_penalty(anef)

    return {
        "penalty_db": penalty,
        "zone_code": worst.get("zone_code"),
        "zone_desc": f"{worst['airfield']} — ANEF {anef}+",
        "anef_min": anef,
        "anef_max": None,
        "lga": worst.get("lga"),
        "impact": _anef_impact(anef),
        "source": worst.get("source", "national"),
        "airport_type": "defence" if worst.get("source") == "defence" else "civilian",
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Query aircraft noise penalty (national)")
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lng", type=float, required=True)
    args = parser.parse_args()

    result = aircraft_noise_penalty(args.lat, args.lng)
    if result["zone_code"]:
        print(f"Zone: {result['zone_code']}")
        print(f"ANEF: {result['anef_min']}+")
        print(f"Penalty: +{result['penalty_db']} dB")
        print(f"Airport: {result['zone_desc']}")
        print(f"Impact: {result['impact']}")
        print(f"Source: {result['source']}")
    else:
        print("No aircraft noise overlay at this location.")
