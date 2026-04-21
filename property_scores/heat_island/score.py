"""
Urban Heat Island score using Open-Meteo climate data + land cover proxies.

Estimates relative heat exposure at a location by combining:
1. Summer mean temperature from Open-Meteo historical API
2. Building density from Overture (proxy for impervious surface)
3. Distance to nearest park/water from Overture POIs (cooling effect)

Score 0-100 where 100 = coolest / lowest heat island effect.
"""

import math
import requests

OPEN_METEO_HIST = "https://archive-api.open-meteo.com/v1/archive"
OPEN_METEO_ELEV = "https://api.open-meteo.com/v1/elevation"

# Southern hemisphere: summer = Dec-Feb; Northern: Jun-Aug
SH_SUMMER_MONTHS = (12, 1, 2)

# Reference temperature range for scoring (Celsius, summer daily max)
# Melbourne outer suburb ~27°C, CBD ~32°C, inland Australia ~40°C+
TEMP_COOL = 22.0
TEMP_HOT = 35.0


def _fetch_summer_temp(lat: float, lng: float) -> tuple[float | None, float | None]:
    """Fetch mean and p90 summer daily max temperature over 5 recent summers."""
    try:
        resp = requests.get(OPEN_METEO_HIST, params={
            "latitude": lat,
            "longitude": lng,
            "start_date": "2019-12-01",
            "end_date": "2024-02-29",
            "daily": "temperature_2m_max",
            "timezone": "auto",
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        dates = data.get("daily", {}).get("time", [])
        temps = data.get("daily", {}).get("temperature_2m_max", [])

        summer_temps = []
        for d, t in zip(dates, temps):
            if t is None:
                continue
            month = int(d.split("-")[1])
            if month in (12, 1, 2):
                summer_temps.append(t)

        if not summer_temps:
            return None, None
        mean_t = sum(summer_temps) / len(summer_temps)
        sorted_t = sorted(summer_temps)
        p90_idx = int(len(sorted_t) * 0.9)
        p90_t = sorted_t[min(p90_idx, len(sorted_t) - 1)]
        return mean_t, p90_t
    except (requests.RequestException, ValueError, KeyError):
        return None, None


def _building_density_proxy(lat: float, lng: float) -> float | None:
    """Estimate building density using Overture buildings within 500m.

    Returns a 0-1 value where 1 = very dense. Uses DuckDB if data is available,
    otherwise returns None.
    """
    try:
        from property_scores.common.overture import get_db
        from property_scores.common.config import data_path

        buildings_file = data_path("overture_buildings.parquet")
        if not buildings_file.exists():
            return None

        db = get_db()
        m_per_deg = 111_320 * math.cos(math.radians(lat))
        delta = 500 / 111_000 * 1.5
        deg_thresh = 500 / m_per_deg

        sql = f"""
            SELECT COUNT(*) as cnt
            FROM read_parquet('{buildings_file}')
            WHERE bbox.xmin BETWEEN {lng - delta} AND {lng + delta}
              AND bbox.ymin BETWEEN {lat - delta} AND {lat + delta}
              AND ST_Distance(geometry, ST_Point({lng}, {lat})) < {deg_thresh}
        """
        result = db.sql(sql).fetchone()
        count = result[0] if result else 0
        # Normalize: 0 buildings = 0.0, 500+ buildings in 500m = 1.0
        return min(count / 500.0, 1.0)
    except Exception:
        return None


def _greenspace_proxy(lat: float, lng: float) -> float | None:
    """Estimate green space coverage using Overture POIs (parks/gardens within 1km).

    Returns a 0-1 value where 1 = lots of green space.
    """
    try:
        from property_scores.common.overture import get_db, pois_near
        db = get_db()
        pois = pois_near(db, lat, lng, radius_m=1000)
        green_keywords = {"park", "garden", "recreation", "playground",
                          "nature", "reserve", "botanical", "forest"}
        green_count = sum(
            1 for cat, _ in pois
            if cat and any(kw in cat.lower() for kw in green_keywords)
        )
        # Normalize: 0 parks = 0.0, 20+ parks within 1km = 1.0
        return min(green_count / 20.0, 1.0)
    except Exception:
        return None


def heat_island_score(lat: float, lng: float) -> dict:
    """Compute urban heat island score for a coordinate.

    Returns:
        dict with score (0-100, 100=coolest), label, temperature data, factors.
    """
    mean_temp, p90_temp = _fetch_summer_temp(lat, lng)

    if mean_temp is None:
        return {
            "score": None,
            "label": "Data unavailable",
            "error": "Could not fetch temperature data from Open-Meteo",
        }

    # Use weighted blend of mean and P90 (extreme heat days matter more)
    effective_temp = mean_temp * 0.4 + p90_temp * 0.6
    temp_score = max(0.0, min(100.0, (TEMP_HOT - effective_temp) / (TEMP_HOT - TEMP_COOL) * 100))

    # Adjust with building density (more buildings = hotter)
    building_density = _building_density_proxy(lat, lng)
    density_penalty = 0.0
    if building_density is not None:
        density_penalty = building_density * 15  # up to -15 points

    # Adjust with green space (more parks = cooler)
    greenspace = _greenspace_proxy(lat, lng)
    green_bonus = 0.0
    if greenspace is not None:
        green_bonus = greenspace * 5  # up to +5 points

    score = max(0, min(100, round(temp_score - density_penalty + green_bonus)))

    if score >= 80:
        label = "Very Cool"
    elif score >= 60:
        label = "Cool"
    elif score >= 40:
        label = "Moderate Heat"
    elif score >= 20:
        label = "Hot"
    else:
        label = "Extreme Heat"

    result = {
        "score": score,
        "label": label,
        "summer_mean_c": round(mean_temp, 1),
        "summer_p90_c": round(p90_temp, 1),
    }
    if building_density is not None:
        result["building_density"] = round(building_density, 2)
    if greenspace is not None:
        result["greenspace_factor"] = round(greenspace, 2)

    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Compute urban heat island score")
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lng", type=float, required=True)
    args = parser.parse_args()

    result = heat_island_score(args.lat, args.lng)
    print(f"Heat Island Score: {result['score']}/100 ({result['label']})")
    if result.get('summer_mean_c'):
        print(f"Summer avg max: {result['summer_mean_c']}°C | P90: {result.get('summer_p90_c')}°C")
    if result.get('building_density') is not None:
        print(f"Building density: {result['building_density']}")
    if result.get('greenspace_factor') is not None:
        print(f"Green space factor: {result['greenspace_factor']}")
