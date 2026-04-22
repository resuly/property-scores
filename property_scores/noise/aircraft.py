"""
Aircraft noise penalty from Victorian planning overlays.

Queries the VicPlan ArcGIS REST service for two airport overlay types:

- **MAEO** (Melbourne Airport Environs Overlay) - Layer 27
  Schedule 1 (MAEO1): within the 25 ANEF contour - severe noise exposure
  Schedule 2 (MAEO2): within the 20-25 ANEF contour - moderate noise exposure

- **AEO** (Airport Environs Overlay) - Layer 22
  AEO1: within 25 ANEF for regional airports
  AEO2: within 20-25 ANEF for regional airports
  AEO (unscheduled): general airport environs

ANEF (Australian Noise Exposure Forecast) is the statutory metric for
aircraft noise in Australia.  The ANEF contour numbers map approximately to:

  ANEF 20  ~  55 dB Leq(24h)  -- noticeable, acoustic treatment advised
  ANEF 25  ~  60 dB Leq(24h)  -- significant, residential not recommended
  ANEF 30  ~  65 dB Leq(24h)  -- severe, only commercial/industrial
  ANEF 35  ~  70 dB Leq(24h)  -- extreme
  ANEF 40  ~  75 dB Leq(24h)  -- airport immediate vicinity

Data source: VicPlan Planning Scheme Overlays (MapServer), updated weekly.
API is free, no auth required, CC BY 4.0 license.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# VicPlan ArcGIS REST endpoints
# ---------------------------------------------------------------------------
_VICPLAN_BASE = (
    "https://plan-gis.mapshare.vic.gov.au/arcgis/rest/services"
    "/Planning/Vicplan_PlanningSchemeOverlays/MapServer"
)
_MAEO_LAYER = f"{_VICPLAN_BASE}/27"  # Melbourne Airport Environs Overlay
_AEO_LAYER = f"{_VICPLAN_BASE}/22"   # Airport Environs Overlay (regional)

_TIMEOUT = 10  # seconds

# ---------------------------------------------------------------------------
# Zone → noise mapping
# ---------------------------------------------------------------------------
# ANEF values are based on the Victorian Planning Provisions:
#   MAEO1 / AEO1: land within the 25 ANEF contour
#   MAEO2 / AEO2: land within the 20-25 ANEF contour
#   AEO (generic): assumed 20-25 ANEF (conservative)
#
# Approximate Leq(24h) equivalents from AS2021-2015 Table 2.1 interpolation.

_ZONE_PROFILES: dict[str, dict[str, Any]] = {
    "MAEO1": {
        "anef_min": 25,
        "anef_max": None,       # up to the airport boundary
        "penalty_db": 12.0,     # severe: ~60+ dB Leq
        "description": "Melbourne Airport Environs Overlay - Schedule 1 (>= 25 ANEF)",
        "impact": "Significant aircraft noise. Residential not recommended.",
    },
    "MAEO2": {
        "anef_min": 20,
        "anef_max": 25,
        "penalty_db": 7.0,      # moderate: ~55-60 dB Leq
        "description": "Melbourne Airport Environs Overlay - Schedule 2 (20-25 ANEF)",
        "impact": "Moderate aircraft noise. Acoustic treatment required for dwellings.",
    },
    "AEO1": {
        "anef_min": 25,
        "anef_max": None,
        "penalty_db": 10.0,     # slightly less than MAEO1 (smaller regional airports)
        "description": "Airport Environs Overlay Schedule 1 (>= 25 ANEF)",
        "impact": "Significant aircraft noise near regional airport.",
    },
    "AEO2": {
        "anef_min": 20,
        "anef_max": 25,
        "penalty_db": 6.0,
        "description": "Airport Environs Overlay Schedule 2 (20-25 ANEF)",
        "impact": "Moderate aircraft noise near regional airport.",
    },
    "AEO": {
        "anef_min": 20,
        "anef_max": 25,
        "penalty_db": 5.0,      # conservative estimate for generic AEO
        "description": "Airport Environs Overlay (unscheduled, ~20-25 ANEF)",
        "impact": "Aircraft noise zone near regional airport.",
    },
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _query_overlay(layer_url: str, lat: float, lng: float) -> list[dict]:
    """Point-in-polygon query against a VicPlan overlay layer.

    Returns list of feature attribute dicts that intersect the point.
    """
    params = {
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "outSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "where": "1=1",
        "outFields": "ZONE_CODE,ZONE_DESCRIPTION,LGA,SCHEME_CODE",
        "f": "json",
        "returnGeometry": "false",
    }
    try:
        resp = requests.get(
            f"{layer_url}/query", params=params, timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return [f["attributes"] for f in data.get("features", [])]
    except (requests.RequestException, ValueError, KeyError) as exc:
        logger.warning("VicPlan query failed for (%s, %s): %s", lat, lng, exc)
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def aircraft_noise_penalty(lat: float, lng: float) -> dict:
    """Query ANEF zone for a location via VicPlan planning overlays.

    Returns:
        dict with keys:
            penalty_db    - float, noise penalty in dB to add to ambient
                            (0.0 if outside all airport overlays)
            zone_code     - str or None, e.g. "MAEO1", "AEO2"
            zone_desc     - str, human-readable zone description
            anef_min      - int or None, lower ANEF bound
            anef_max      - int or None, upper ANEF bound
            lga           - str or None, local government area
            impact        - str, plain-English impact statement
            source        - str, data source identifier
            airport_type  - str, "melbourne" | "regional" | None
    """
    # Query both overlay layers (MAEO for Melbourne Airport, AEO for regional)
    maeo_hits = _query_overlay(_MAEO_LAYER, lat, lng)
    aeo_hits = _query_overlay(_AEO_LAYER, lat, lng)

    all_hits = maeo_hits + aeo_hits

    if not all_hits:
        return {
            "penalty_db": 0.0,
            "zone_code": None,
            "zone_desc": "Outside airport noise overlay",
            "anef_min": None,
            "anef_max": None,
            "lga": None,
            "impact": "No aircraft noise overlay applies to this location.",
            "source": "vicplan",
            "airport_type": None,
        }

    # Find the worst (highest penalty) zone among all hits
    worst_penalty = 0.0
    worst_hit: dict | None = None
    worst_profile: dict | None = None

    for hit in all_hits:
        zone_code = hit.get("ZONE_CODE", "")
        profile = _ZONE_PROFILES.get(zone_code)
        if profile is None:
            # Unknown zone code -- assign a conservative default
            logger.warning("Unknown airport overlay zone: %s", zone_code)
            profile = {
                "anef_min": 20,
                "anef_max": None,
                "penalty_db": 5.0,
                "description": hit.get("ZONE_DESCRIPTION", zone_code),
                "impact": "Aircraft noise overlay (unknown schedule).",
            }
        if profile["penalty_db"] > worst_penalty:
            worst_penalty = profile["penalty_db"]
            worst_hit = hit
            worst_profile = profile

    # Determine airport type
    scheme = worst_hit.get("SCHEME_CODE", "") if worst_hit else ""
    if scheme == "MAEO":
        airport_type = "melbourne"
    elif scheme == "AEO":
        airport_type = "regional"
    else:
        airport_type = "unknown"

    return {
        "penalty_db": worst_profile["penalty_db"],
        "zone_code": worst_hit.get("ZONE_CODE"),
        "zone_desc": worst_profile["description"],
        "anef_min": worst_profile["anef_min"],
        "anef_max": worst_profile["anef_max"],
        "lga": worst_hit.get("LGA"),
        "impact": worst_profile["impact"],
        "source": "vicplan",
        "airport_type": airport_type,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(
        description="Query aircraft noise penalty from VicPlan overlays",
    )
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lng", type=float, required=True)
    args = parser.parse_args()

    result = aircraft_noise_penalty(args.lat, args.lng)

    if result["zone_code"]:
        print(f"Zone: {result['zone_code']} ({result['zone_desc']})")
        print(f"ANEF: {result['anef_min']}"
              + (f"-{result['anef_max']}" if result['anef_max'] else "+"))
        print(f"Penalty: +{result['penalty_db']} dB")
        print(f"LGA: {result['lga']}")
        print(f"Impact: {result['impact']}")
        print(f"Airport: {result['airport_type']}")
    else:
        print("No aircraft noise overlay at this location.")

    print(f"\n--- Raw ---\n{json.dumps(result, indent=2)}")
