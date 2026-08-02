"""
Urban Heat Island score combining satellite + land cover data.

Two signal layers:
1. MODIS LST 1km — satellite surface temperature (daytime, 8-day composite)
2. Local factors — building density + greenspace from Overture

Score 0-100 where 100 = coolest / lowest heat island effect.

Used to carry a third layer, Open-Meteo ERA5 25km air temperature, as the
fallback for points with no MODIS coverage. Removed 2026-08-02: DA Leads is
a paid commercial product and that endpoint's free tier is non-commercial-
use-only (open-meteo.com/en/terms) — see the note above
`_building_density_proxy`. Points with no MODIS coverage now return "Data
unavailable" instead of an ERA5-derived estimate.
"""

import math
import time as _time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache

import requests

PC_STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
PC_SIGN = "https://planetarycomputer.microsoft.com/api/sas/v1/sign"

SH_SUMMER_MONTHS = (12, 1, 2)
# LST anchors set against the AU summer urban distribution (350-point
# sweep, 2026-06-11: residential LST p5=25.2 / p50=32.9 / p99=47.9°C) AND
# the hot truth anchors (Penrith hardstand, Oran Park / Tarneit treeless
# estates, Darwin). The original 22-42 span branded half the country
# "Hot/Extreme Heat" (median address 41, Paddington QLD read 13); a wider
# 26-50 trial fixed the median but lifted genuine hot spots into "Cool"
# (Tarneit 61). 25-45 holds both ends: sample median 59, all ten heat
# anchors pass, treeless estates stay sub-50.
TEMP_COOL = 25.0
TEMP_HOT = 45.0
MODIS_R = 6371007.181

_signed_cache: dict[str, tuple[str, float]] = {}

# Local MODIS LST summer mosaic (built by scripts/download_modis_lst.py).
# Sampling the local sinusoidal VRT is ~0.1s vs ~18s for the remote signed-COG
# path (2026-07-02: remote MODIS was 17.7s of a 19.7s cold call). The shared
# raster sampler reprojects lat/lng into the MODIS sinusoidal CRS automatically,
# so no warp is needed. Points outside tile coverage read back NODATA -> None
# -> "Data unavailable" in heat_island_score(), same as a remote MODIS miss.
from property_scores.common.config import data_path as _data_path

_DAY_VRT = str(_data_path("global/modis_lst_day.vrt"))
_NIGHT_VRT = str(_data_path("global/modis_lst_night.vrt"))


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
    """MODIS LST from the local summer mosaic (day/night + 5x5 UHI delta).

    Center pixel = point LST; the 24 surrounding 1km pixels (the 2km window with
    the centre pixel backed out) = area LST, matching the remote path's
    neighbourhood. Returns None outside tile coverage, on a NODATA/water pixel,
    or on any sampler error, so the caller reports "Data unavailable".
    """
    import os as _os
    if not _os.path.exists(_DAY_VRT):
        return None
    try:
        from property_scores.common import landcover as _lc
        rs = _lc.sampler()

        day = rs.sample(_DAY_VRT, lat, lng)
        if day is None or day != day:  # None or NaN (outside coverage / nodata)
            return None
        day = float(day)

        # Area LST over the 5x5 1km-pixel window (radius 2km on the ~926m
        # sinusoidal grid -> +/-2 pixels). window_stats INCLUDES the centre
        # pixel, but the remote path averages the 24 neighbours only; back the
        # centre out with (mean*n - centre)/(n-1) so uhi_delta matches remote.
        st = rs.window_stats(_DAY_VRT, lat, lng, radius_m=2000)
        n = int(st.get("count", 0)) if st else 0
        mean_incl = float(st.get("mean", day)) if st else day
        area = (mean_incl * n - day) / (n - 1) if n > 1 else mean_incl
        uhi_delta = day - area

        night = rs.sample(_NIGHT_VRT, lat, lng)
        night = float(night) if (night is not None and night == night) else None
    except Exception:
        return None  # any sampler / IO / CRS error -> "Data unavailable"

    result: dict = {
        "point_lst_c": round(day, 1),
        "area_lst_c": round(area, 1),
        "uhi_delta_c": round(uhi_delta, 1),
        "samples": 1,
    }
    if night is not None:
        result["night_lst_c"] = round(night, 1)
        result["night_retention_c"] = round(night - (day - 15), 1)
    return result


def _modis_lst_remote(lat: float, lng: float) -> dict | None:
    """Fetch MODIS LST 1km surface temperature for recent summers (remote COG).

    Queries 8-day composites from Jan-Feb of recent year.
    Compares point value to 5x5 neighborhood for UHI detection.

    Retained as the source-of-truth reference for scripts/download_modis_lst.py
    (which bakes this exact signal into the local mosaic) and as an optional
    slow fallback. Not called on the hot path.
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
# Open-Meteo ERA5 fallback — removed 2026-08-02. This used to fire on every
# heat_island_score() call (submitted to the thread pool unconditionally
# alongside MODIS, its result only discarded if MODIS succeeded), hitting
# api.open-meteo.com/v1/archive on every scored address. DA Leads is a paid
# commercial product and that endpoint's free tier is non-commercial-use-only
# (open-meteo.com/en/terms). There is no local substitute wired up for this
# yet, so heat_island_score() now falls straight through to the existing
# "Data unavailable" branch when MODIS has no coverage for a point, instead
# of estimating from ERA5 air temperature. See property-scores-openmeteo-
# noncommercial-tos followup: option (2), a paid Open-Meteo Historical
# Weather API plan, or option (3), a different climate source (SILO/BOM),
# would restore coverage for the MODIS-miss case — that decision needs Bo.
# ---------------------------------------------------------------------------


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
    """Vegetation cover that mitigates urban heat.

    Prefers the real ESA WorldCover green fraction within 500m (vegetation cover
    is the direct UHI driver via shade + evapotranspiration). Falls back to the
    park-POI count proxy when WorldCover is unavailable.
    """
    try:
        from property_scores.common import landcover as lc
        green = lc.green_fraction(lat, lng, radius_m=500)
        if green is not None:
            return green
    except Exception:
        pass
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

_cache: OrderedDict[tuple[float, float], tuple[dict, float]] = OrderedDict()
_CACHE_MAX = 2000
_CACHE_TTL = 3600


def _cache_key(lat: float, lng: float) -> tuple[float, float]:
    """The ADDRESS, not its neighbourhood. round(5) is ~1 m.

    This used to be round(2), a ~1.1 km grid, justified by "MODIS is 1km
    resolution". That reasoning covered one input and was applied to the whole
    result, so the two LOCAL terms travelled with it: whichever address in a
    cell computed first that hour decided `building_density` and
    `greenspace_factor` for every other address in the cell.

    Measured in production 2026-07-25: open grass in Royal Park
    (-37.7845, 144.9530) scored 60 with greenspace 0.78 and density 0.47, and
    terrace housing 450 m away (-37.7805, 144.9545) was served that same row
    with cached=True. Its own values, 130 m further east in the next cell, are
    56 / 0.65 / 1.00. Dense housing was reported as parkland, and the two
    fields describe a location the caller never asked about. It also churned
    over time, since the answer depended on which neighbour arrived first.

    The premise had also expired: `_modis_lst` reads a local VRT mosaic now
    (`_modis_lst_remote` is explicitly off the hot path), so nothing here is
    worth a kilometre of smearing. The one genuinely remote call keeps a grid
    memo of its own below, at its own resolution.
    """
    return (round(lat, 5), round(lng, 5))


def heat_island_score(lat: float, lng: float) -> dict:
    """Compute urban heat island score for a coordinate.

    Uses MODIS 1km surface temperature when available. Adjusted by building
    density and greenspace factors. Points with no MODIS coverage return
    "Data unavailable" (see the module-level note above `_building_density_proxy`
    about the removed Open-Meteo ERA5 fallback).
    """
    key = _cache_key(lat, lng)
    now = _time.time()
    if key in _cache:
        cached, ts = _cache[key]
        if now - ts < _CACHE_TTL:
            _cache.move_to_end(key)
            return {**cached, "cached": True}
        else:
            del _cache[key]

    with ThreadPoolExecutor(max_workers=3) as pool:
        f_modis = pool.submit(_modis_lst, lat, lng)
        f_density = pool.submit(_building_density_proxy, lat, lng)
        f_green = pool.submit(_greenspace_proxy, lat, lng)

    modis = f_modis.result()
    building_density = f_density.result()
    greenspace = f_green.result()

    # MODIS is water-masked (sea pixels are fill), so a coastal or
    # forest-edge point compares against only the sea-breeze/forest-cooled
    # land pixels left in its 5x5 ring: Bondi rendered "+1.9C compared with
    # surrounding area" and took a 3x penalty for being on the beach
    # (2026-06-11 audit). When >30% of the 500m surrounds are water or tree,
    # the UHI comparison is not like-for-like; skip it.
    uhi_context_ok = True
    try:
        from property_scores.common import landcover as _lc
        _fr = _lc.fractions(lat, lng, radius_m=500)
        if _fr and (_fr.get(80, 0.0) + _fr.get(10, 0.0)) > 0.30:
            uhi_context_ok = False  # 80=water, 10=tree (ESA WorldCover)
    except Exception:
        pass

    source = None
    if modis and modis["point_lst_c"] > 0:
        # MODIS-based scoring: use actual surface temperature
        source = "modis"
        lst = modis["point_lst_c"]
        uhi = modis["uhi_delta_c"]

        # Score from absolute surface temperature
        temp_score = max(0.0, min(100.0, (TEMP_HOT - lst) / (TEMP_HOT - TEMP_COOL) * 100))

        # UHI penalty: hotter than surroundings = urban heat island
        uhi_penalty = max(0, uhi) * 3 if uhi_context_ok else 0

        # Night heat retention penalty: high nighttime temp = poor cooling
        if modis.get("night_lst_c") is not None and modis["night_lst_c"] > 18:
            night_penalty = min((modis["night_lst_c"] - 18) * 1.5, 10)
            uhi_penalty += night_penalty
    else:
        # No MODIS coverage for this point. Used to fall back to Open-Meteo
        # ERA5 air temperature here (removed 2026-08-02, see the note above
        # `_building_density_proxy` — that endpoint's free tier is
        # non-commercial-use-only and DA Leads is a paid product).
        return {
            "score": None,
            "label": "Data unavailable",
            "error": "Could not fetch temperature data",
        }

    # --- Local adjustments (already fetched in parallel) ---
    density_penalty = building_density * 6 if building_density is not None else 0.0
    # Tree/green cover is the primary UHI lever, so weight it as a CENTRED
    # adjustment rather than a bonus-only term: above ~0.35 green fraction
    # (typical AU suburban cover) reads cooler, below it reads hotter. The old
    # bonus-only x5 term meant a nearly treeless, fully-paved CBD cell still read
    # "Cool" off its moderate 1km MODIS temperature — the canopy reality could
    # not pull it down (Bo, 2026-07-15). Because it is centred, the median
    # address (green ~0.35) is unchanged; only the tails separate — leafy sites
    # reward more, low-canopy paved sites penalise. Validated against 11 hot/cool
    # anchors: fixes the CBD outlier (60->54 Moderate) and lifts genuinely leafy
    # sites, without pushing ordinary suburbia into Hot or the hot-truth anchors
    # (Oran Park / Penrith / Tarneit) into Extreme.
    GREEN_BASELINE = 0.35
    green_adjust = (greenspace - GREEN_BASELINE) * 22 if greenspace is not None else 0.0

    score = max(0, min(100, round(temp_score - uhi_penalty - density_penalty + green_adjust)))

    # Very Cool at 85 keeps the top label to ~15% of addresses (80 let 32%
    # of the 350-point sweep in; Bo opted for the tighter cut 2026-06-11).
    if score >= 85:
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
        "disclaimer": "Based on satellite surface temperature (1km resolution). Block-level variations may differ significantly.",
    }

    # `source` is always "modis" here: the only other branch (no MODIS
    # coverage) returns early above. Kept as a field for API stability —
    # it used to also read "era5" before that fallback was removed
    # 2026-08-02 (see the note above `_building_density_proxy`).
    result["source"] = source
    if modis and modis.get("night_lst_c") is not None:
        result["night_lst_c"] = modis["night_lst_c"]
    if modis:
        result["modis_lst_c"] = modis["point_lst_c"]
        result["modis_area_c"] = modis["area_lst_c"]
        # The "+X.XC compared with surrounding area" sentence renders off
        # this field; omit it where the neighbourhood is sea/forest and the
        # comparison is meaningless.
        if uhi_context_ok:
            result["uhi_delta_c"] = modis["uhi_delta_c"]
    if building_density is not None:
        result["building_density"] = round(building_density, 2)
    if greenspace is not None:
        result["greenspace_factor"] = round(greenspace, 2)

    _cache[key] = (result, _time.time())
    if len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)

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
    if result.get("building_density") is not None:
        print(f"Building density: {result['building_density']}")
    if result.get("greenspace_factor") is not None:
        print(f"Green space: {result['greenspace_factor']}")
