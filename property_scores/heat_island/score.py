"""
Urban Heat Island score combining satellite + climate + land cover data.

Three signal layers:
1. MODIS LST 1km — satellite surface temperature (daytime, 8-day composite)
2. Open-Meteo ERA5 — 5-year summer air temperature (25km, fallback)
3. Local factors — building density + greenspace from Overture

Score 0-100 where 100 = coolest / lowest heat island effect.
"""

import math
import time as _time

import requests

OPEN_METEO_HIST = "https://archive-api.open-meteo.com/v1/archive"
PC_STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
PC_SIGN = "https://planetarycomputer.microsoft.com/api/sas/v1/sign"

SH_SUMMER_MONTHS = (12, 1, 2)
TEMP_COOL = 22.0
TEMP_HOT = 42.0
MODIS_R = 6371007.181

_signed_cache: dict[str, tuple[str, float]] = {}


# ---------------------------------------------------------------------------
# MODIS LST helpers
# ---------------------------------------------------------------------------

def _wgs84_to_sinusoidal(lat: float, lng: float) -> tuple[float, float]:
    lat_r = math.radians(lat)
    lng_r = math.radians(lng)
    return MODIS_R * lng_r * math.cos(lat_r), MODIS_R * lat_r


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


def _modis_lst(lat: float, lng: float) -> dict | None:
    """Fetch MODIS LST 1km surface temperature for recent summers.

    Queries 8-day composites from Jan-Feb of recent year.
    Compares point value to 5x5 neighborhood for UHI detection.
    """
    try:
        import rasterio

        resp = requests.post(f"{PC_STAC}/search", json={
            "collections": ["modis-11A2-061"],
            "bbox": [lng - 0.1, lat - 0.1, lng + 0.1, lat + 0.1],
            "datetime": "2024-01-01/2024-02-28",
            "limit": 4,
        }, timeout=15)
        if not resp.ok:
            return None
        items = resp.json().get("features", [])
        if not items:
            return None

        sx, sy = _wgs84_to_sinusoidal(lat, lng)
        pixel = 926.625  # ~1km MODIS pixel

        center_day: list[float] = []
        neighbor_day: list[float] = []
        center_night: list[float] = []

        for item in items[:3]:
            # Day LST
            day_href = item.get("assets", {}).get("LST_Day_1km", {}).get("href")
            if day_href:
                signed = _get_signed(day_href)
                if signed:
                    with rasterio.open(signed) as ds:
                        val = list(ds.sample([(sx, sy)]))[0][0]
                        if val > 0:
                            center_day.append(val * 0.02 - 273.15)
                        for dy in range(-2, 3):
                            for dx in range(-2, 3):
                                if dx == 0 and dy == 0:
                                    continue
                                nval = list(ds.sample([(sx + dx * pixel, sy + dy * pixel)]))[0][0]
                                if nval > 0:
                                    neighbor_day.append(nval * 0.02 - 273.15)

            # Night LST
            night_href = item.get("assets", {}).get("LST_Night_1km", {}).get("href")
            if night_href:
                signed = _get_signed(night_href)
                if signed:
                    with rasterio.open(signed) as ds:
                        val = list(ds.sample([(sx, sy)]))[0][0]
                        if val > 0:
                            center_night.append(val * 0.02 - 273.15)

        if not center_day:
            return None

        point_day = sum(center_day) / len(center_day)
        area_day = sum(neighbor_day) / len(neighbor_day) if neighbor_day else point_day
        uhi_delta = point_day - area_day
        point_night = sum(center_night) / len(center_night) if center_night else None

        result = {
            "point_lst_c": round(point_day, 1),
            "area_lst_c": round(area_day, 1),
            "uhi_delta_c": round(uhi_delta, 1),
            "samples": len(center_day),
        }
        if point_night is not None:
            result["night_lst_c"] = round(point_night, 1)
            result["night_retention_c"] = round(point_night - (point_day - 15), 1)
        return result
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Open-Meteo ERA5 fallback
# ---------------------------------------------------------------------------

def _fetch_summer_temp(lat: float, lng: float) -> tuple[float | None, float | None]:
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

        summer_temps = [t for d, t in zip(dates, temps)
                        if t is not None and int(d.split("-")[1]) in (12, 1, 2)]

        if not summer_temps:
            return None, None
        mean_t = sum(summer_temps) / len(summer_temps)
        sorted_t = sorted(summer_temps)
        p90_t = sorted_t[min(int(len(sorted_t) * 0.9), len(sorted_t) - 1)]
        return mean_t, p90_t
    except (requests.RequestException, ValueError, KeyError):
        return None, None


# ---------------------------------------------------------------------------
# Local factors
# ---------------------------------------------------------------------------

def _building_density_proxy(lat: float, lng: float) -> float | None:
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
        return min(count / 500.0, 1.0)
    except Exception:
        return None


def _greenspace_proxy(lat: float, lng: float) -> float | None:
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
        return min(green_count / 20.0, 1.0)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------

def heat_island_score(lat: float, lng: float) -> dict:
    """Compute urban heat island score for a coordinate.

    Uses MODIS 1km surface temperature when available, falling back to
    Open-Meteo ERA5 25km air temperature. Adjusted by building density
    and greenspace factors.
    """
    # --- MODIS LST (1km satellite surface temp) ---
    modis = _modis_lst(lat, lng)

    # --- Open-Meteo ERA5 fallback ---
    mean_temp, p90_temp = _fetch_summer_temp(lat, lng)

    if modis and modis["point_lst_c"] > 0:
        # MODIS-based scoring: use actual surface temperature
        lst = modis["point_lst_c"]
        uhi = modis["uhi_delta_c"]

        # Score from absolute surface temperature
        temp_score = max(0.0, min(100.0, (TEMP_HOT - lst) / (TEMP_HOT - TEMP_COOL) * 100))

        # UHI penalty: hotter than surroundings = urban heat island
        uhi_penalty = max(0, uhi) * 3

        # Night heat retention penalty: high nighttime temp = poor cooling
        if modis.get("night_lst_c") is not None and modis["night_lst_c"] > 18:
            night_penalty = min((modis["night_lst_c"] - 18) * 1.5, 10)
            uhi_penalty += night_penalty
    elif mean_temp is not None:
        # ERA5 fallback
        effective_temp = mean_temp * 0.4 + p90_temp * 0.6
        temp_score = max(0.0, min(100.0, (TEMP_HOT - effective_temp) / (TEMP_HOT - TEMP_COOL) * 100))
        uhi_penalty = 0
    else:
        return {
            "score": None,
            "label": "Data unavailable",
            "error": "Could not fetch temperature data",
        }

    # --- Local adjustments ---
    building_density = _building_density_proxy(lat, lng)
    density_penalty = building_density * 12 if building_density is not None else 0.0

    greenspace = _greenspace_proxy(lat, lng)
    green_bonus = greenspace * 5 if greenspace is not None else 0.0

    score = max(0, min(100, round(temp_score - uhi_penalty - density_penalty + green_bonus)))

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

    result: dict = {
        "score": score,
        "label": label,
        "disclaimer": "Based on satellite surface temperature (1km resolution) and ERA5 climate data. Block-level variations may differ significantly.",
    }

    if modis and modis.get("night_lst_c") is not None:
        result["night_lst_c"] = modis["night_lst_c"]
    if modis:
        result["modis_lst_c"] = modis["point_lst_c"]
        result["modis_area_c"] = modis["area_lst_c"]
        result["uhi_delta_c"] = modis["uhi_delta_c"]
    if mean_temp is not None:
        result["summer_mean_c"] = round(mean_temp, 1)
        result["summer_p90_c"] = round(p90_temp, 1)
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
    if result.get("modis_lst_c"):
        print(f"MODIS LST: {result['modis_lst_c']}°C (area avg: {result['modis_area_c']}°C, UHI: {result['uhi_delta_c']:+.1f}°C)")
    if result.get("summer_mean_c"):
        print(f"ERA5: mean {result['summer_mean_c']}°C, P90 {result['summer_p90_c']}°C")
    if result.get("building_density") is not None:
        print(f"Building density: {result['building_density']}")
    if result.get("greenspace_factor") is not None:
        print(f"Green space: {result['greenspace_factor']}")
