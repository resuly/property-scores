"""
Solar potential score using Global Solar Atlas data.

Fetches GHI (Global Horizontal Irradiance) and PVOUT (photovoltaic output
potential) from the Global Solar Atlas API, then adjusts for building
orientation. No shading model: PVOUT is an open-horizon Solargis simulation
(do not claim shading anywhere downstream, 2026-06-11 audit).
"""

import time as _time
from collections import OrderedDict

import requests

GSA_API = "https://api.globalsolaratlas.info/data/lta"

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
        annual = data.get("annual", {}).get("data", {})
        result = {
            "ghi_kwh_m2": annual.get("GHI"),
            "dni_kwh_m2": annual.get("DNI"),
            "pvout_kwh_kwp": annual.get("PVOUT_csi"),
            "gti_kwh_m2": annual.get("GTI_opta"),
            "optimal_tilt_deg": annual.get("OPTA"),
            "temp_avg_c": annual.get("TEMP"),
            "elevation_m": annual.get("ELE"),
        }
    except (requests.RequestException, KeyError, ValueError):
        return None  # do not cache failures; a transient outage should retry

    _cache[key] = (result, now)
    if len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)
    return result


def solar_score(lat: float, lng: float, *,
                roof_area_m2: float | None = None,
                orientation: str = "optimal") -> dict:
    """Compute solar potential score for a coordinate.

    Args:
        lat, lng: WGS84 coordinates.
        roof_area_m2: Estimated usable roof area. If None, returns area-independent score.
        orientation: One of 'optimal', 'east', 'west', 'suboptimal'.

    Returns:
        dict with score (0-100), label, ghi, pvout, estimated_annual_kwh.
    """
    solar = _fetch_solar_data(lat, lng)

    if not solar or not solar["pvout_kwh_kwp"]:
        return {
            "score": None,
            "label": "Data unavailable",
            "error": "Could not fetch Global Solar Atlas data",
        }

    pvout = solar["pvout_kwh_kwp"]
    ghi = solar["ghi_kwh_m2"]
    orient_factor = ORIENTATION_FACTOR.get(orientation, 0.85)

    # PVOUT anchors: 750 ≈ marginal viability (high-latitude Europe, where
    # rooftop PV still pays back), 2000 ≈ best on Earth. The previous global
    # 600-2400 range squeezed all of Australia (1350 Hobart - 1937 Alice)
    # into 42-74: the sunniest continent had zero "Excellent" addresses
    # (350-point sweep, 2026-06-11 re-anchor).
    score_raw = (pvout - 750) / (2000 - 750) * 100 * orient_factor
    score = max(0, min(100, round(score_raw)))

    estimated_kwh = None
    if roof_area_m2:
        panel_efficiency = 0.20
        capacity_kwp = roof_area_m2 * panel_efficiency
        # PVOUT_csi is a full PV-system simulation: Solargis already applies
        # the performance ratio (Sydney PVOUT/GTI = 0.813). Multiplying by
        # another 0.80 double-counted losses (-14~17% vs CEC expectations;
        # any owner with an inverter app catches it). kWh = kWp x PVOUT.
        estimated_kwh = round(capacity_kwp * pvout * orient_factor)

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
        "score": score,
        "label": label,
        "caveat": "Regional solar irradiance estimate. Does not account for roof orientation, shading, or available roof area.",
        "ghi_kwh_m2_year": round(ghi, 1) if ghi else None,
        "dni_kwh_m2_year": round(solar["dni_kwh_m2"], 1) if solar.get("dni_kwh_m2") else None,
        "pvout_kwh_kwp_year": round(pvout, 1),
        "orientation_factor": orient_factor,
        "estimated_annual_kwh": estimated_kwh,
        "optimal_tilt_deg": solar["optimal_tilt_deg"],
        "temp_avg_c": solar.get("temp_avg_c"),
        "elevation_m": solar.get("elevation_m"),
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
