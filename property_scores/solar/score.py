"""Regional solar-resource context using Global Solar Atlas data.

This module does not model a roof.  It returns the long-term, open-horizon
resource at a location: GHI/DNI at roughly 250 m, PVOUT at roughly 1 km and
optimum tilt at roughly 4 km.  A caller may supply an area and coarse
orientation to obtain an explicitly labelled *scenario*, but that scenario is
not usable-roof area, roof-plane segmentation or a shading calculation.
"""

import math
import time as _time
from collections import OrderedDict

import requests

GSA_API = "https://api.globalsolaratlas.info/data/lta"

GSA_SOURCE = "Global Solar Atlas 2.0"
GSA_LICENSE = "CC BY 4.0"
GSA_LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
GSA_TERMS_URL = "https://globalsolaratlas.info/support/terms-of-use"
GSA_ATTRIBUTION = (
    "Data obtained from the Global Solar Atlas 2.0, developed and operated "
    "by Solargis on behalf of the World Bank Group, with funding from ESMAP."
)

# Official GSA output resolutions.  These are deliberately per field: saying
# the whole response is "250 m" makes the 1 km PVOUT and 4 km optimum-tilt
# values look four to sixteen times more local than they are.
GSA_RESOLUTION_M = {
    "ghi_kwh_m2_year": 250,
    "dni_kwh_m2_year": 250,
    "gti_kwh_m2_year": 250,
    "pvout_kwh_kwp_year": 1000,
    "optimal_tilt_deg": 4000,
    "temp_avg_c": 1000,
    "elevation_m": 1000,
}

# Cache the GSA fetch: irradiance is an annual long-term average (time-invariant)
# and the API has no local equivalent, so every map click was a 0.6-1.6s network
# round trip with a 15s timeout and no graceful degrade (would 502 under load the
# same way noise/terrain did, 2026-06-13). Keyed on a ~110m grid; mirrors the
# heat_island cache pattern.
_cache: "OrderedDict[tuple[float, float], tuple[dict | None, float]]" = OrderedDict()
_CACHE_MAX = 2000
_CACHE_TTL = 86400  # 1 day; LTA values do not change intra-day


def _cache_key(lat: float, lng: float) -> tuple[float, float]:
    return (round(lat, 3), round(lng, 3))

# Orientation efficiency relative to optimal (north in southern hemisphere)
ORIENTATION_FACTOR = {
    "optimal": 1.0,
    "east": 0.85,
    "west": 0.85,
    "suboptimal": 0.65,
}


def _finite_metric(value):
    """Return an upstream numeric metric or None for malformed JSON values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _fetch_solar_data(lat: float, lng: float) -> dict | None:
    key = _cache_key(lat, lng)
    now = _time.time()
    if key in _cache:
        cached, ts = _cache[key]
        if now - ts < _CACHE_TTL:
            _cache.move_to_end(key)
            return cached
        del _cache[key]

    try:
        resp = requests.get(GSA_API, params={"loc": f"{lat},{lng}"}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        annual_block = data.get("annual") or {}
        annual = annual_block.get("data") or {}
        metadata = annual_block.get("metadata") or {}
        layers = metadata.get("layers") or {}
        if not all(isinstance(row, dict)
                   for row in (annual_block, annual, metadata, layers)):
            return None
        result = {
            "ghi_kwh_m2": _finite_metric(annual.get("GHI")),
            "dni_kwh_m2": _finite_metric(annual.get("DNI")),
            "pvout_kwh_kwp": _finite_metric(annual.get("PVOUT_csi")),
            "gti_kwh_m2": _finite_metric(annual.get("GTI_opta")),
            "optimal_tilt_deg": _finite_metric(annual.get("OPTA")),
            "temp_avg_c": _finite_metric(annual.get("TEMP")),
            "elevation_m": _finite_metric(annual.get("ELE")),
            "source_metadata": {
                "retrieved_at_ms": metadata.get("ts"),
                "dataset_version": (metadata.get("version") or {}).get("data"),
                "layers": layers,
            },
        }
    except (requests.RequestException, AttributeError, KeyError, TypeError,
            ValueError):
        return None  # do not cache failures; a transient outage should retry

    _cache[key] = (result, now)
    if len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)
    return result


def solar_score(lat: float, lng: float, *,
                roof_area_m2: float | None = None,
                orientation: str = "optimal") -> dict:
    """Compute regional solar-resource context for a coordinate.

    Args:
        lat, lng: WGS84 coordinates.
        roof_area_m2: Legacy parameter.  When supplied it is treated only as a
            caller-provided panel-area proxy for a gross open-horizon scenario;
            this function does not establish that the area is usable roof.
        orientation: Coarse scenario assumption: optimal/east/west/suboptimal.

    Returns:
        Compatible dict with the legacy score fields plus an explicit resource
        contract, field-level resolutions, provenance and optional scenario.
    """
    if orientation not in ORIENTATION_FACTOR:
        raise ValueError(
            "orientation must be one of: " + ", ".join(ORIENTATION_FACTOR))
    if roof_area_m2 is not None and (
            not isinstance(roof_area_m2, (int, float))
            or not math.isfinite(roof_area_m2)
            or roof_area_m2 <= 0):
        raise ValueError("roof_area_m2 must be a positive number when supplied")

    solar = _fetch_solar_data(lat, lng)

    if not solar or not solar["pvout_kwh_kwp"]:
        return {
            "product": "solar_resource",
            "assessment_level": "regional_resource",
            "score": None,
            "label": "Data unavailable",
            "error": "Could not fetch Global Solar Atlas data",
            "source": GSA_SOURCE,
            "licence": GSA_LICENSE,
            "licence_url": GSA_LICENSE_URL,
            "attribution": GSA_ATTRIBUTION,
        }

    pvout = solar["pvout_kwh_kwp"]
    ghi = solar["ghi_kwh_m2"]
    orient_factor = ORIENTATION_FACTOR.get(orientation, 0.85)

    # PVOUT anchors: 750 ≈ marginal viability (high-latitude Europe, where
    # rooftop PV still pays back), 2000 ≈ best on Earth. The previous global
    # 600-2400 range squeezed all of Australia (1350 Hobart - 1937 Alice)
    # into 42-74: the sunniest continent had zero "Excellent" addresses
    # (350-point sweep, 2026-06-11 re-anchor).
    # The score describes the LOCATION'S resource, so a caller's roof
    # orientation must not move it.  Orientation is applied only to the
    # optional generation scenario below.
    score_raw = (pvout - 750) / (2000 - 750) * 100
    score = max(0, min(100, round(score_raw)))

    estimated_kwh = None
    generation_scenario = None
    if roof_area_m2 is not None:
        panel_efficiency = 0.20
        capacity_kwp = roof_area_m2 * panel_efficiency
        # PVOUT_csi is a full PV-system simulation: Solargis already applies
        # the performance ratio (Sydney PVOUT/GTI = 0.813). Multiplying by
        # another 0.80 double-counted losses (-14~17% vs CEC expectations;
        # any owner with an inverter app catches it). kWh = kWp x PVOUT.
        estimated_kwh = round(capacity_kwp * pvout * orient_factor)
        generation_scenario = {
            "status": "gross_open_horizon_scenario",
            "caller_area_m2": round(float(roof_area_m2), 2),
            "area_semantics": (
                "caller-provided panel-area proxy; not validated usable roof area"),
            "capacity_kwp_assumption": round(capacity_kwp, 3),
            "panel_power_density_kwp_per_m2": panel_efficiency,
            "orientation": orientation,
            "orientation_factor": orient_factor,
            "annual_generation_kwh": estimated_kwh,
            "not_modelled": [
                "roof planes", "usable roof area", "building shading",
                "tree shading", "obstructions", "existing panels",
                "tariff", "self-consumption", "battery dispatch",
            ],
        }

    layer_metadata = solar.get("source_metadata") or {}
    layer_rows = layer_metadata.get("layers") or {}
    source_vintage = {}
    for public_name, source_name in (
        ("ghi", "GHI"), ("dni", "DNI"), ("pvout", "PVOUT_csi"),
        ("optimal_tilt", "OPTA"),
    ):
        row = layer_rows.get(source_name) or {}
        period = row.get("period") or {}
        source_vintage[public_name] = {
            "period_from": period.get("from"),
            "period_to": period.get("to"),
            "updated": row.get("updated"),
            "version": row.get("version"),
        }

    if score >= 80:
        label = "Excellent Solar Potential"
    elif score >= 60:
        label = "Good Solar Potential"
    elif score >= 40:
        label = "Moderate Solar Potential"
    elif score >= 20:
        label = "Low Solar Potential"
    else:
        label = "Poor Solar Potential"

    return {
        "product": "solar_resource",
        "assessment_level": "regional_resource",
        "score": score,
        "label": label,
        "caveat": (
            "Regional, open-horizon solar-resource estimate. It is not a "
            "roof assessment and does not model roof planes, usable area, "
            "building or tree shading, obstructions, tariffs or batteries."),
        "open_horizon": True,
        "roof_model": {
            "roof_planes_modelled": False,
            "usable_area_modelled": False,
            "shading_modelled": False,
        },
        "spatial_resolution_m": dict(GSA_RESOLUTION_M),
        "ghi_kwh_m2_year": round(ghi, 1) if ghi else None,
        "dni_kwh_m2_year": round(solar["dni_kwh_m2"], 1) if solar.get("dni_kwh_m2") else None,
        "gti_kwh_m2_year": round(solar["gti_kwh_m2"], 1) if solar.get("gti_kwh_m2") else None,
        "pvout_kwh_kwp_year": round(pvout, 1),
        "orientation_factor": orient_factor,
        "estimated_annual_kwh": estimated_kwh,
        "generation_scenario": generation_scenario,
        "optimal_tilt_deg": solar["optimal_tilt_deg"],
        "temp_avg_c": solar.get("temp_avg_c"),
        "elevation_m": solar.get("elevation_m"),
        "source": GSA_SOURCE,
        "licence": GSA_LICENSE,
        "licence_url": GSA_LICENSE_URL,
        "terms_url": GSA_TERMS_URL,
        "attribution": GSA_ATTRIBUTION,
        "source_metadata": {
            "dataset_version": layer_metadata.get("dataset_version"),
            "retrieved_at_ms": layer_metadata.get("retrieved_at_ms"),
            "vintage": source_vintage,
        },
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Compute solar potential score")
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lng", type=float, required=True)
    parser.add_argument("--roof-area", type=float, default=None, help="Usable roof m²")
    parser.add_argument("--orientation", default="optimal",
                        choices=["optimal", "east", "west", "suboptimal"])
    args = parser.parse_args()

    result = solar_score(args.lat, args.lng,
                         roof_area_m2=args.roof_area, orientation=args.orientation)
    print(f"Solar Score: {result['score']}/100 ({result['label']})")
    if result.get("ghi_kwh_m2_year"):
        print(f"GHI: {result['ghi_kwh_m2_year']} kWh/m²/year")
        print(f"PVOUT: {result['pvout_kwh_kwp_year']} kWh/kWp/year")
    if result.get("estimated_annual_kwh"):
        print(f"Estimated annual generation: {result['estimated_annual_kwh']} kWh")
