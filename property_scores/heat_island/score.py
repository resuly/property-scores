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

"No coverage" is narrower than it used to be: MODIS water-masks whole 1km
pixels, so waterfront addresses landed on NODATA and lost the score even
though the mosaic covers them and land pixels sit a few hundred metres away.
Since 2026-08-05 those read the nearest ring of land pixels within 2 km (see
`_nearest_land_pixel`), report `lst_source="nearest_land_pixel"` with the
distance, and drop the UHI term. "Data unavailable" is now reserved for real
gaps: outside tile coverage, >2 km of water/fill, or a point that WorldCover
says is water.
"""

import json
import math
import os
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
_MOSAIC_METADATA = str(_data_path("global/modis_lst_metadata.json"))
_ACTIVE_MOSAIC_DIR = str(_data_path("global/modis_lst_current"))
_MOSAIC_RELEASES_DIR = str(_data_path("global/modis_lst_releases"))

_MODIS_ATTRIBUTION = (
    "NASA LP DAAC MOD11A2 Version 6.1 land-surface temperature, accessed "
    "through Microsoft Planetary Computer; DA Leads summer-median processing."
)
_HEAT_SOURCES = [
    {
        "source": "NASA MOD11A2 Version 6.1",
        "licence": "Public domain (United States government work)",
        "attribution": _MODIS_ATTRIBUTION,
        "role": "day/night land-surface temperature",
    },
    {
        "source": "ESA WorldCover",
        "licence": "CC BY 4.0",
        "attribution": "ESA WorldCover project 2021",
        "role": "10 m canopy, built-surface and water context",
    },
    {
        "source": "Overture Maps buildings",
        "licence": "mixed CC BY 4.0 and ODbL-1.0 inputs; derived aggregate only",
        "attribution": "Overture Maps Foundation and contributing data providers",
        "role": "derived building-density aggregate",
    },
]

_mosaic_metadata_cache: tuple[str, float | None, str | None, dict] | None = None


def _resolve_mosaic_paths() -> tuple[str, str, str]:
    """Pin one active generation for a score call, with legacy fallback."""
    if os.path.lexists(_ACTIVE_MOSAIC_DIR):
        release_dir = os.path.realpath(_ACTIVE_MOSAIC_DIR)
        return (
            os.path.join(release_dir, "modis_lst_day.vrt"),
            os.path.join(release_dir, "modis_lst_night.vrt"),
            os.path.join(release_dir, "modis_lst_metadata.json"),
        )
    return _DAY_VRT, _NIGHT_VRT, _MOSAIC_METADATA


def _mosaic_vintage(metadata_path: str | None = None) -> dict:
    """Machine-readable vintage, never inferred from a file mtime.

    The original bake produced only GeoTIFFs and a VRT, so production cannot
    prove which summers were selected.  That state is reported as unverified
    until the refreshed downloader atomically writes the sidecar manifest.
    """
    global _mosaic_metadata_cache
    metadata_path = metadata_path or _resolve_mosaic_paths()[2]
    release_dir = os.path.dirname(os.path.realpath(metadata_path))
    releases_root = os.path.realpath(_MOSAIC_RELEASES_DIR)
    try:
        in_published_release = (
            os.path.commonpath((release_dir, releases_root)) == releases_root
            and release_dir != releases_root)
    except ValueError:
        in_published_release = False
    try:
        mtime = os.path.getmtime(metadata_path)
    except OSError:
        mtime = None
    if (_mosaic_metadata_cache
            and _mosaic_metadata_cache[:3] == (
                metadata_path, mtime, release_dir)):
        return dict(_mosaic_metadata_cache[3])
    if not in_published_release:
        out = {
            "status": "unverified",
            "note": (
                "The installed mosaic is not an atomically published "
                "generation."),
        }
    elif mtime is None:
        out = {
            "status": "unverified",
            "note": "The installed mosaic predates the machine-readable vintage manifest.",
        }
    else:
        try:
            with open(metadata_path, encoding="utf-8") as handle:
                raw = json.load(handle)
            required = {
                "collection", "seasons", "stat", "generated_at",
                "release_id", "tile_count",
            }
            if not isinstance(raw, dict) or not required.issubset(raw):
                raise ValueError("incomplete manifest")
            if raw["release_id"] != os.path.basename(release_dir):
                raise ValueError("manifest release does not match active directory")
            out = {"status": "verified", **raw}
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            out = {
                "status": "unverified",
                "note": "The installed mosaic vintage manifest is unreadable or incomplete.",
            }
    _mosaic_metadata_cache = (metadata_path, mtime, release_dir, out)
    return dict(out)

# MODIS LST is water-masked at source: any 1km pixel MODIS classes as water is
# written as fill, so a beachfront address sits on a NODATA pixel even though
# the mosaic covers it and the land pixels a few hundred metres inland are fine.
# Before 2026-08-05 that returned "Data unavailable" for the whole heat score.
# Production, 1/1 Cavill Avenue Surfers Paradise QLD (-28.0027, 153.4296):
# centre pixel NODATA, two pixels in the 927 m ring carry data (raw samples
# 31.80999755859375 north and 32.290008544921875 west), so the address now
# reads their mean, reported as 32.1C. Score 48, "Moderate Heat".
_MODIS_PIXEL_M = 926.625  # MODIS sinusoidal grid step (~1 km)
# 2 km is the radius of the window this module already averages to define "the
# surrounding area", so every candidate is a pixel the score was already
# reading. Rings inside it: 927 m (edge-adjacent), 1310 m (diagonal), 1853 m
# (two pixels out). Measured 2026-08-05 over 6000 random AU DA coordinates:
# 149 addresses recovered here, 142 in the first ring and 7 in the second,
# none in the third; 3 more stayed unavailable.
_MODIS_NEIGHBOUR_MAX_M = 2000.0


# ---------------------------------------------------------------------------
# MODIS LST helpers
# ---------------------------------------------------------------------------

def _wgs84_to_sinusoidal(lat: float, lng: float) -> tuple[float, float]:
    lat_r = math.radians(lat)
    lng_r = math.radians(lng)
    return MODIS_R * lng_r * math.cos(lat_r), MODIS_R * lat_r


def _sinusoidal_to_wgs84(x: float, y: float) -> tuple[float, float]:
    """Inverse of _wgs84_to_sinusoidal (same sphere, same false origin)."""
    lat = math.degrees(y / MODIS_R)
    cos_lat = math.cos(math.radians(lat))
    if abs(cos_lat) < 1e-9:
        return lat, 0.0
    return lat, math.degrees(x / (MODIS_R * cos_lat))


@lru_cache(maxsize=4)
def _neighbour_rings(max_m: float, pixel_m: float
                     ) -> tuple[tuple[float, tuple[tuple[int, int], ...]], ...]:
    """Pixel offsets grouped into equidistant rings, nearest ring first.

    Grouping matters: picking one pixel out of several at the same distance
    would need a tie-break, and any fixed tie-break is a compass bias (always
    the west neighbour on an east-facing coast). Every pixel in the nearest
    ring that has data is averaged instead.

    The two constants are required arguments rather than globals read inside
    the cached body, so a caller (or a test) that changes them gets a fresh
    ring set instead of a silently stale cached one. They are required, not
    defaulted, because a default would reintroduce exactly that hole: the
    no-argument call would cache the globals under one key.
    """
    by_dist: dict[float, list[tuple[int, int]]] = {}
    span = int(max_m // pixel_m) + 1
    for dy in range(-span, span + 1):
        for dx in range(-span, span + 1):
            if dx == 0 and dy == 0:
                continue
            dist = math.hypot(dx, dy) * pixel_m
            if dist > max_m:
                continue
            by_dist.setdefault(round(dist, 3), []).append((dx, dy))
    return tuple((d, tuple(sorted(by_dist[d]))) for d in sorted(by_dist))


def _point_is_water(lat: float, lng: float) -> bool:
    """True only when ESA WorldCover says this exact 10 m cell is water.

    The fallback must not turn a geocode that landed in the ocean (or a lake)
    into a land temperature. Unknown / mosaic missing returns False so the
    fallback still works where WorldCover is unavailable.
    """
    try:
        from property_scores.common import landcover as _lc
        if not _lc.available():
            return False
        val = _lc.sampler().sample(_lc.LC_VRT, lat, lng)
        if val is None or val != val:
            return False
        return int(val) == _lc.WATER
    except Exception:
        return False


def _nearest_land_pixel(rs, lat: float, lng: float, day_vrt: str | None = None
                        ) -> tuple[float, list[tuple[float, float]], float] | None:
    """Day LST from the nearest ring of MODIS pixels that carries data.

    Returns (mean day LST, the coordinates of the pixels averaged, distance in
    metres), or None when nothing within _MODIS_NEIGHBOUR_MAX_M has data. The
    coordinates come back so the night reading is taken from the SAME pixels.
    A day value from one place and a night value from another would not be a
    single site's diurnal behaviour.
    """
    day_vrt = day_vrt or _DAY_VRT
    sx, sy = _wgs84_to_sinusoidal(lat, lng)
    for dist, offsets in _neighbour_rings(_MODIS_NEIGHBOUR_MAX_M, _MODIS_PIXEL_M):
        vals: list[float] = []
        coords: list[tuple[float, float]] = []
        for dx, dy in offsets:
            nlat, nlng = _sinusoidal_to_wgs84(sx + dx * _MODIS_PIXEL_M,
                                              sy + dy * _MODIS_PIXEL_M)
            v = rs.sample(day_vrt, nlat, nlng)
            if v is None or v != v:
                continue
            vals.append(float(v))
            coords.append((nlat, nlng))
        if vals:
            return sum(vals) / len(vals), coords, dist
    return None


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
    neighbourhood.

    When the centre pixel itself has no reading, the value comes instead from
    the nearest ring of 1km pixels that do, within 2 km, reported as
    ``lst_source="nearest_land_pixel"`` with its distance and the number of
    pixels averaged. The commonest cause is MODIS water-masking a waterfront
    address's own pixel, but persistent cloud and tile gaps land in the same
    NODATA, so nothing here claims to know which it was.

    On that path there is no like-for-like point/area comparison to make: the
    borrowed pixels are members of the same window the "area" is averaged over.
    ``uhi_delta_c`` and ``area_lst_c`` are both returned as None so no consumer
    can render a difference between the two.

    Returns None outside tile coverage, when nothing within 2 km has data, when
    the point itself is water, or on any sampler error, so the caller reports
    "Data unavailable".
    """
    day_vrt, night_vrt, metadata_path = _resolve_mosaic_paths()
    if not (os.path.exists(day_vrt) and os.path.exists(night_vrt)):
        return None
    try:
        from property_scores.common import landcover as _lc
        rs = _lc.sampler()

        lst_source = "pixel"
        offset_m = 0.0
        pixels = 1
        night_points = [(lat, lng)]

        # Area LST over the 5x5 1km-pixel window (radius 2km on the ~926m
        # sinusoidal grid -> +/-2 pixels), which is also the window every
        # fallback candidate lives in.
        st = rs.window_stats(day_vrt, lat, lng, radius_m=2000)
        n = int(st.get("count", 0)) if st else 0

        day = rs.sample(day_vrt, lat, lng)
        if day is None or day != day:  # None or NaN (outside coverage / nodata)
            if n == 0:
                return None  # nothing within 2 km has data; skip the ring search
            if _point_is_water(lat, lng):
                return None  # the address really is on water; do not invent land
            hit = _nearest_land_pixel(rs, lat, lng, day_vrt)
            if hit is None:
                return None  # only corner pixels had data, outside the 2 km cap
            day, night_points, offset_m = hit
            lst_source = "nearest_land_pixel"
            pixels = len(night_points)
        else:
            day = float(day)

        # window_stats INCLUDES the centre pixel, but the remote path averages
        # the 24 neighbours only; back the centre out with
        # (mean*n - centre)/(n-1) so uhi_delta matches remote. On the fallback
        # path the centre pixel is NODATA, so window_stats never counted it and
        # there is no area figure worth reporting anyway.
        mean_incl = float(st.get("mean", day)) if st else day
        if lst_source == "pixel":
            area = (mean_incl * n - day) / (n - 1) if n > 1 else mean_incl
            uhi_delta = day - area
        else:
            area = None
            uhi_delta = None

        nights = []
        for nlat, nlng in night_points:
            v = rs.sample(night_vrt, nlat, nlng)
            if v is not None and v == v:
                nights.append(float(v))
        night = sum(nights) / len(nights) if nights else None
    except Exception:
        return None  # any sampler / IO / CRS error -> "Data unavailable"

    result: dict = {
        "point_lst_c": round(day, 1),
        "area_lst_c": round(area, 1) if area is not None else None,
        "uhi_delta_c": round(uhi_delta, 1) if uhi_delta is not None else None,
        "samples": 1,  # composites averaged; the local mosaic is one composite
        "lst_source": lst_source,
        "_mosaic_metadata_path": metadata_path,
    }
    if lst_source != "pixel":
        result["lst_offset_m"] = int(round(offset_m))
        result["lst_pixels_averaged"] = pixels
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


def _score_from_components(temp_score: float, uhi_penalty: float,
                           building_density: float | None,
                           greenspace: float | None) -> int:
    """Combine the same precision the API publishes for local factors.

    Density and greenspace are returned at two decimals.  Scoring on hidden
    extra precision made a caller recomputing the documented formula get a
    different integer at rounding boundaries (Kew: disclosed inputs imply 55,
    hidden 0.326 greenery produced 54).  Quantise once at the contract boundary
    and use those exact values for both score and payload.
    """
    density = round(building_density, 2) if building_density is not None else None
    green = round(greenspace, 2) if greenspace is not None else None
    density_penalty = density * 6 if density is not None else 0.0
    green_adjust = (green - 0.35) * 22 if green is not None else 0.0
    return max(0, min(100, round(
        temp_score - uhi_penalty - density_penalty + green_adjust)))


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

    A waterfront address whose own 1km pixel is water-masked is scored off the
    nearest land pixel within 2km (`_modis_lst`), with `lst_source` /
    `lst_offset_m` in the payload, the distance stated in the disclaimer, and
    no UHI term. The local building-density and greenspace factors are always
    measured at the true address, on both paths.
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
    mosaic_metadata_path = None
    if modis:
        modis = dict(modis)
        mosaic_metadata_path = modis.pop("_mosaic_metadata_path", None)
    mosaic_vintage = _mosaic_vintage(mosaic_metadata_path)
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

    # A borrowed neighbour temperature cannot be compared against the
    # neighbourhood it was borrowed from: the "point" and the "area" are then
    # the same population, and the difference is sampling noise dressed up as
    # an urban heat island. `_modis_lst` already returns uhi_delta_c=None there;
    # this makes the caller-side suppression explicit rather than incidental.
    if modis and modis.get("lst_source", "pixel") != "pixel":
        uhi_context_ok = False

    source = None
    if modis and modis["point_lst_c"] > 0:
        # MODIS-based scoring: use actual surface temperature
        source = "modis"
        lst = modis["point_lst_c"]
        uhi = modis["uhi_delta_c"] or 0.0

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
            "product": "neighbourhood_heat",
            "assessment_level": "neighbourhood_context",
            "score": None,
            "label": "Data unavailable",
            "error": "Could not fetch temperature data",
            "temperature_resolution_m": 1000,
            "land_cover_resolution_m": 10,
            "temperature_vintage": mosaic_vintage,
            "sources": [dict(source) for source in _HEAT_SOURCES],
        }

    # --- Local adjustments (already fetched in parallel) ---
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
    score = _score_from_components(
        temp_score, uhi_penalty, building_density, greenspace)

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

    disclaimer = ("Based on satellite surface temperature (1km resolution). "
                  "Block-level variations may differ significantly.")
    offset_m = modis.get("lst_offset_m") if modis else None
    if modis and modis.get("lst_source", "pixel") != "pixel":
        # Say it on the customer-facing surface, not just in a field: the
        # temperature is measured near the address, not on it. The count is
        # rendered rather than guessed at: over the 149 recovered addresses in
        # the 6000-coordinate sample, 31 read a single pixel and 118 read two
        # to four, so neither a fixed singular nor a fixed plural is honest.
        # It does not name a cause: the satellite's water mask is the usual
        # reason a waterfront pixel is empty, but persistent cloud and tile gaps
        # produce the same empty pixel and this code cannot tell them apart.
        px = (modis.get("lst_pixels_averaged") or 1)
        source_txt = ("the nearest pixel that does" if px == 1
                      else f"the nearest {px} pixels that do")
        disclaimer += (
            f" The satellite's own 1km pixel for this address carries no "
            f"reading, which is common on waterfront sites, so the surface "
            f"temperature is read from {source_txt}, about {offset_m} m away. "
            f"The urban heat island comparison is not reported for this "
            f"address.")

    result: dict = {
        "product": "neighbourhood_heat",
        "assessment_level": "neighbourhood_context",
        "score": score,
        "label": label,
        "disclaimer": disclaimer,
        "temperature_resolution_m": 1000,
        "temperature_native_grid_step_m": _MODIS_PIXEL_M,
        "land_cover_resolution_m": 10,
        "temperature_vintage": mosaic_vintage,
        "sources": [dict(source) for source in _HEAT_SOURCES],
    }

    # `source` is always "modis" here: the only other branch (no MODIS
    # coverage) returns early above. Kept as a field for API stability —
    # it used to also read "era5" before that fallback was removed
    # 2026-08-02 (see the note above `_building_density_proxy`).
    result["source"] = source
    if modis and modis.get("night_lst_c") is not None:
        result["night_lst_c"] = modis["night_lst_c"]
        result["day_night_cooling_c"] = round(
            modis["point_lst_c"] - modis["night_lst_c"], 1)
    if modis:
        result["modis_lst_c"] = modis["point_lst_c"]
        # Withheld on the borrowed-pixel path, because DA Leads' map panel
        # renders its own "vs surroundings" figure from
        # (modis_lst_c - modis_area_c) rather than from uhi_delta_c
        # (frontend/map/components/panel/scores/GenericScore.vue). Suppressing
        # only uhi_delta_c would leave that surface printing the very difference
        # this path says it cannot measure. The same divergence exists on the
        # older sea/forest suppression path and is NOT changed here: those
        # addresses have shipped an area figure for months, and the right fix
        # is in the renderer, not in a field removal that would silently move
        # payloads that are currently correct in every other respect.
        if (modis.get("lst_source", "pixel") == "pixel"
                and modis.get("area_lst_c") is not None):
            result["modis_area_c"] = modis["area_lst_c"]
        result["lst_source"] = modis.get("lst_source", "pixel")
        if offset_m is not None:
            result["lst_offset_m"] = offset_m
        if modis.get("lst_pixels_averaged") is not None:
            result["lst_pixels_averaged"] = modis["lst_pixels_averaged"]
        # The "+X.XC compared with surrounding area" sentence renders off
        # this field; omit it where the neighbourhood is sea/forest and the
        # comparison is meaningless.
        if uhi_context_ok and modis.get("uhi_delta_c") is not None:
            result["uhi_delta_c"] = modis["uhi_delta_c"]
    if building_density is not None:
        result["building_density"] = round(building_density, 2)
    if greenspace is not None:
        result["greenspace_factor"] = round(greenspace, 2)

    _cache[key] = (result, _time.time())
    if len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)

    return result


def _print_result(result: dict) -> None:
    """CLI rendering, split out of __main__ so it is reachable by tests.

    It was inside the `if __name__ == "__main__"` block and therefore never
    executed by the suite, which is how it kept indexing keys that this module
    stopped guaranteeing.
    """
    print(f"Heat Island Score: {result['score']}/100 ({result['label']})")
    if result.get("modis_lst_c"):
        # BOTH uhi_delta_c and modis_area_c are absent whenever the comparison
        # is not like-for-like: sea/forest surrounds drop the delta, and the
        # borrowed-pixel path drops the area too. Indexing either one directly
        # is a KeyError on exactly the addresses this path exists for.
        uhi = result.get("uhi_delta_c")
        area = result.get("modis_area_c")
        parts = [f"area avg: {area}°C" if area is not None else "area avg: n/a"]
        parts.append(f"UHI: {uhi:+.1f}°C" if uhi is not None else "UHI: n/a")
        print(f"MODIS LST: {result['modis_lst_c']}°C ({', '.join(parts)})")
        if result.get("lst_offset_m") is not None:
            px = result.get("lst_pixels_averaged", 1)
            print(f"  (own pixel has no reading: LST read from {px} pixel"
                  f"{'' if px == 1 else 's'} {result['lst_offset_m']} m away)")
    if result.get("building_density") is not None:
        print(f"Building density: {result['building_density']}")
    if result.get("greenspace_factor") is not None:
        print(f"Green space: {result['greenspace_factor']}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Compute urban heat island score")
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lng", type=float, required=True)
    args = parser.parse_args()

    _print_result(heat_island_score(args.lat, args.lng))
