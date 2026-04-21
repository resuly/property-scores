"""
Solar potential score using Global Solar Atlas data.

Fetches GHI (Global Horizontal Irradiance) and PVOUT (photovoltaic output
potential) from the Global Solar Atlas API, then adjusts for building
orientation and nearby shading.
"""

import requests

GSA_API = "https://api.globalsolaratlas.info/data/lta"

# Orientation efficiency relative to optimal (north in southern hemisphere)
ORIENTATION_FACTOR = {
    "optimal": 1.0,
    "east": 0.85,
    "west": 0.85,
    "suboptimal": 0.65,
}


def _fetch_solar_data(lat: float, lng: float) -> dict | None:
    try:
        resp = requests.get(GSA_API, params={"loc": f"{lat},{lng}"}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        annual = data.get("annual", {}).get("data", {})
        return {
            "ghi_kwh_m2": annual.get("GHI_opta"),
            "dni_kwh_m2": annual.get("DNI_opta"),
            "pvout_kwh_kwp": annual.get("PVOUT_csi"),
            "optimal_tilt_deg": annual.get("OPTA"),
            "temp_avg_c": annual.get("TEMP"),
        }
    except (requests.RequestException, KeyError, ValueError):
        return None


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

    # Score based on PVOUT relative to global range
    # Global PVOUT range: ~600 (Nordic) to ~2400 (Sahara) kWh/kWp/year
    # Good solar: >1600, Excellent: >2000
    score_raw = (pvout - 600) / (2400 - 600) * 100 * orient_factor
    score = max(0, min(100, round(score_raw)))

    estimated_kwh = None
    if roof_area_m2:
        panel_efficiency = 0.20
        performance_ratio = 0.80
        capacity_kwp = roof_area_m2 * panel_efficiency
        estimated_kwh = round(capacity_kwp * pvout * orient_factor * performance_ratio)

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
        "ghi_kwh_m2_year": ghi,
        "pvout_kwh_kwp_year": pvout,
        "orientation_factor": orient_factor,
        "estimated_annual_kwh": estimated_kwh,
        "optimal_tilt_deg": solar["optimal_tilt_deg"],
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
