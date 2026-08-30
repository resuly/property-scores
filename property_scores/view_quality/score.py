"""Landscape Openness context, retained under the legacy ``view_quality`` key.

Six factors are weighted and combined into a 0-100 score:
1. Ocean/coast proximity (weight 3.0) — Overture water features
2. Inland water proximity (weight 1.5) — rivers, lakes, reservoirs
3. Elevation advantage (weight 2.5) — local 5 m/30 m terrain
4. Green space proximity (weight 2.0) — parks/gardens from Overture POIs
5. Building openness (weight 2.0) — inverse of nearby building density
6. Terrain-horizon openness (weight 2.5) — eight directional terrain profiles

The model does not know an observer's storey, window orientation or building
occlusion along a target sightline, so it must never be described as actual
line-of-sight or a guaranteed view.
"""

import logging
import math
import time as _time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from property_scores.common import landcover as lc
from property_scores.common.config import data_path
from property_scores.common.overture import (
    POIS_FILE,
    WATER_FILE,
    buildings_near,
    get_db,
    pois_near,
    water_near,
)

log = logging.getLogger(__name__)


def _sample_elevations(lats: list, lngs: list) -> list | None:
    """Batch local bare-earth elevations: 5 m LiDAR, then 30 m fallback."""
    try:
        from property_scores.common import terrain
        out = [terrain.elevation(la, lo) for la, lo in zip(lats, lngs)]
        if any(v is not None for v in out):
            return out
    except Exception:
        pass
    return None

OCEAN_CLASSES = {"ocean", "sea", "bay", "strait", "tidal_channel", "lagoon"}
INLAND_WATER_CLASSES = {"lake", "river", "reservoir", "water", "stream"}

GREEN_KEYWORDS = {
    "park", "garden", "recreation", "playground", "nature",
    "reserve", "botanical", "forest", "national_park",
}

FACTORS: dict[str, float] = {
    "ocean_proximity": 3.0,
    "inland_water": 1.5,
    "elevation_advantage": 2.5,
    "green_space": 2.0,
    "building_openness": 2.0,
    "horizon_openness": 2.5,
}

_M_PER_DEG_LAT = 111_320.0
_ELEVATION_DIRECTIONS = (
    ("N", 0.0), ("S", 180.0), ("E", 90.0), ("W", 270.0),
    ("NE", 45.0), ("NW", 315.0), ("SE", 135.0), ("SW", 225.0),
)
_HORIZON_DIRECTIONS = (
    ("N", 0.0), ("NE", 45.0), ("E", 90.0), ("SE", 135.0),
    ("S", 180.0), ("SW", 225.0), ("W", 270.0), ("NW", 315.0),
)


def _offset_point(lat: float, lng: float, distance_m: float,
                  bearing_deg: float) -> tuple[float, float]:
    """Small-distance WGS84 offset with equal ground distance in every bearing."""
    bearing = math.radians(bearing_deg)
    north_m = math.cos(bearing) * distance_m
    east_m = math.sin(bearing) * distance_m
    cos_lat = max(abs(math.cos(math.radians(lat))), 1e-9)
    return (
        lat + north_m / _M_PER_DEG_LAT,
        lng + east_m / (_M_PER_DEG_LAT * cos_lat),
    )


def _data_file_available(filename: str) -> bool:
    return data_path(filename).exists()


def _coastal_escarpment_floor(factors: dict[str, dict]) -> int | None:
    """Great-view floor for an elevated, open site immediately above ocean.

    The additive average treats all-direction building/green density as if it
    could cancel a strong one-direction coastal outlook.  A site is only in
    this class when three independent signals agree: ocean within 200 m,
    >=30 m relative terrain advantage, and at least half the horizon open.
    Flat beaches, dense waterfront streets without an open horizon, and high
    inland sites do not qualify.  The floor says *potential* only; the API's
    line-of-sight caveat remains unchanged.
    """
    ocean = factors.get("ocean_proximity") or {}
    elevation = factors.get("elevation_advantage") or {}
    horizon = factors.get("horizon_openness") or {}
    if (ocean.get("distance_m") is not None
            and ocean["distance_m"] <= 200
            and elevation.get("advantage_m", 0) >= 30
            and horizon.get("open_directions", 0) >= 4):
        return 68
    return None


# ---------------------------------------------------------------------------
# Factor computations
# ---------------------------------------------------------------------------

def _ocean_proximity_factor(db, lat: float, lng: float) -> dict | None:
    """Score based on distance to nearest ocean/coastline."""
    if not _data_file_available(WATER_FILE):
        return None
    rows = water_near(db, lat, lng, radius_m=10_000, strict=True)

    ocean_dist = None
    for cls, _sub, dist_m in rows:
        if cls and cls.lower() in OCEAN_CLASSES:
            ocean_dist = dist_m
            break

    if ocean_dist is None:
        return {
            "value": 0.0,
            "distance_m": None,
            "searched_radius_m": 10_000,
            "coverage_status": "checked_clear",
        }

    if ocean_dist < 200:
        decay = 1.0
    elif ocean_dist < 500:
        decay = 0.90
    elif ocean_dist < 1000:
        decay = 0.75
    elif ocean_dist < 2000:
        decay = 0.55
    elif ocean_dist < 5000:
        decay = 0.30
    else:
        decay = max(0.0, 0.15 * (1 - (ocean_dist - 5000) / 5000))

    return {"value": decay, "distance_m": round(ocean_dist),
            "searched_radius_m": 10_000, "coverage_status": "data_returned"}


def _inland_water_factor(db, lat: float, lng: float) -> dict | None:
    """Score based on distance to nearest river/lake/reservoir."""
    if not _data_file_available(WATER_FILE):
        return None
    rows = water_near(db, lat, lng, radius_m=3000, strict=True)

    water_dist = None
    for cls, _sub, dist_m in rows:
        if cls and cls.lower() in INLAND_WATER_CLASSES:
            water_dist = dist_m
            break

    if water_dist is None:
        return {
            "value": 0.0,
            "distance_m": None,
            "searched_radius_m": 3000,
            "coverage_status": "checked_clear",
        }

    if water_dist < 100:
        decay = 1.0
    elif water_dist < 300:
        decay = 0.80
    elif water_dist < 500:
        decay = 0.60
    elif water_dist < 1000:
        decay = 0.35
    elif water_dist < 2000:
        decay = 0.15
    else:
        decay = 0.0

    return {"value": decay, "distance_m": round(water_dist),
            "searched_radius_m": 3000, "coverage_status": "data_returned"}


def _elevation_advantage_factor(lat: float, lng: float) -> dict | None:
    """Score based on elevation relative to surrounding area.

    Two-scale sampling:
    - Near ring (8 points at ~500m): detects local hilltops/ridges
    - Far ring (8 points at ~2km): detects regional elevation advantage
    Uses the better of the two advantages so hilltops AND elevated plateaus
    both score well. Also gives a baseline bonus for absolute elevation.
    """
    lats = [lat]
    lngs = [lng]
    for distance_m in (500, 2000):
        for _label, bearing in _ELEVATION_DIRECTIONS:
            sample_lat, sample_lng = _offset_point(
                lat, lng, distance_m, bearing)
            lats.append(sample_lat)
            lngs.append(sample_lng)

    elevations = _sample_elevations(lats, lngs)
    if not elevations or len(elevations) < 17:
        return None

    point_elev = elevations[0]
    near_elevs = elevations[1:9]
    far_elevs = elevations[9:17]
    if point_elev is None:
        return None

    near_valid = [e for e in near_elevs if e is not None]
    far_valid = [e for e in far_elevs if e is not None]
    if not near_valid or not far_valid:
        return None

    near_median = sorted(near_valid)[len(near_valid) // 2]
    far_median = sorted(far_valid)[len(far_valid) // 2]
    near_adv = point_elev - near_median
    far_adv = point_elev - far_median

    # Valley-rim advantage: the medians are direction-agnostic, so a ridge
    # suburb with a deep valley on ONE side reads advantage ~0 (half the ring
    # sits on the plateau behind). A sustained directional drop, near point
    # already 5m below and the far point dropping further, is a credible
    # sightline; credit the deepest such drop at a discount (2026-06-12:
    # "uninterrupted views north over National Parks up the valley for 6km"
    # scored as flat terrain).
    rim_drop = 0.0
    for ne, fe in zip(near_elevs, far_elevs):
        if ne is None or fe is None:
            continue
        if point_elev - ne >= 5 and ne - fe >= 5:
            rim_drop = max(rim_drop, point_elev - fe)

    advantage_m = max(near_adv, far_adv, rim_drop * 0.8)

    if advantage_m >= 50:
        decay = 1.0
    elif advantage_m >= 30:
        decay = 0.85
    elif advantage_m >= 15:
        decay = 0.65
    elif advantage_m >= 5:
        decay = 0.45
    elif advantage_m >= 0:
        decay = 0.25
    else:
        decay = max(0.0, 0.15 + advantage_m / 80)

    # Absolute elevation bonus: being high up helps regardless of neighbors
    abs_bonus = min(point_elev / 600.0, 0.15) if point_elev > 50 else 0.0
    decay = min(1.0, decay + abs_bonus)

    return {
        "value": decay,
        "elevation_m": round(point_elev, 1),
        "near_median_m": round(near_median, 1),
        "far_median_m": round(far_median, 1),
        "advantage_m": round(advantage_m, 1),
        "rim_drop_m": round(rim_drop, 1),
    }


def _green_space_factor(db, lat: float, lng: float) -> dict | None:
    """Greenery in the outlook: real tree canopy (ESA WorldCover) combined with
    proximity/density of named parks & gardens.

    The park-POI signal alone misses leafy streets with no tagged park, so we
    take the better of the POI score and the actual woody-canopy fraction within
    500m. Falls back to POI-only when WorldCover is unavailable.
    """
    poi_available = _data_file_available(POIS_FILE)
    pois = pois_near(db, lat, lng, radius_m=1000) if poi_available else []

    green_distances: list[float] = []
    for cat, dist_m in pois:
        if cat and any(kw in cat.lower() for kw in GREEN_KEYWORDS):
            green_distances.append(dist_m)

    if green_distances:
        green_distances.sort()
        count = len(green_distances)
        nearest = green_distances[0]
        proximity_score = max(0.0, 1.0 - nearest / 1000)
        density_score = min(count / 15.0, 1.0)
        poi_value = proximity_score * 0.6 + density_score * 0.4
    else:
        count = 0
        nearest = None
        poi_value = 0.0

    # canopy, not grass: green_fraction counts GRASS+CROP (it was written for
    # bushfire fuel), which made a bare Hay paddock score green 1.0 vs canopy
    # 0.008 and out-rank Dover Heights (2026-06-11 audit). Tree canopy is the
    # honest "green outlook" signal; leafy suburbs keep their score
    # (Warrandyte canopy 0.939).
    green = lc.canopy_fraction(lat, lng, radius_m=500)
    if green is None:
        if not poi_available:
            return None
        if not green_distances:
            return {"value": 0.0, "count": 0, "nearest_m": None,
                    "green_pct": None, "coverage_status": "checked_clear",
                    "signals_used": ["overture_places"]}
        return {"value": round(poi_value, 3), "count": count,
                "nearest_m": round(nearest), "green_pct": None,
                "coverage_status": "data_returned",
                "signals_used": ["overture_places"]}

    # Real vegetation cover is the honest "green outlook" signal (40% within 500m
    # = fully green). Park POIs only act as a floor for destination parks, capped
    # at 0.5 so a POI-dense but treeless CBD no longer maxes out greenery while a
    # genuinely park-adjacent lot still keeps a moderate score.
    green_value = min(green / 0.40, 1.0)
    value = max(green_value, poi_value * 0.5)

    return {
        "value": round(value, 3),
        "count": count,
        "nearest_m": round(nearest) if nearest is not None else None,
        "green_pct": round(green * 100),
        "coverage_status": "data_returned",
        "signals_used": (["esa_worldcover"]
                         + (["overture_places"] if poi_available else [])),
    }


def _building_openness_factor(db, lat: float, lng: float) -> dict | None:
    """Score based on inverse of building density within 300m.

    Calibrated for Australian suburbs: a typical residential street has
    ~100-200 buildings per 300m radius. Only truly dense urban areas
    (CBD, high-rise) should score near 0.
    """
    if not _data_file_available("overture_buildings.parquet"):
        return None
    rows = buildings_near(db, lat, lng, radius_m=300)
    if rows is None:
        return None

    count = len(rows)
    tall_count = sum(1 for h, _d, _f in rows if h and h > 10)

    # Australian calibration: 100 buildings/300m = normal suburban
    if count == 0:
        decay = 1.0
    elif count <= 10:
        decay = 0.95
    elif count <= 40:
        decay = 0.80
    elif count <= 100:
        decay = 0.65
    elif count <= 200:
        decay = 0.45
    elif count <= 350:
        decay = 0.25
    else:
        decay = 0.10

    # Tall buildings (>10m / 3+ storeys) are the real view blockers
    if tall_count > 5:
        tall_penalty = min((tall_count - 5) * 0.04, 0.30)
        decay = max(0.0, decay - tall_penalty)

    return {
        "value": round(decay, 3),
        "buildings_300m": count,
        "tall_buildings": tall_count,
    }


def _horizon_openness_factor(lat: float, lng: float) -> dict | None:
    """Measure how open the horizon is in 8 directions using DEM.

    Samples elevation at 100m/300m/600m/1km/2km in each cardinal direction.
    Low horizon angle = open views. Negative angle = downhill (bonus).
    """
    distances = [100, 300, 600, 1000, 2000]

    lats = [lat]
    lngs = [lng]
    for _label, bearing in _HORIZON_DIRECTIONS:
        for d in distances:
            sample_lat, sample_lng = _offset_point(lat, lng, d, bearing)
            lats.append(sample_lat)
            lngs.append(sample_lng)

    elevs = _sample_elevations(lats, lngs)
    if not elevs or len(elevs) < 1 + len(_HORIZON_DIRECTIONS) * len(distances):
        return None

    center_elev = elevs[0]
    if center_elev is None:
        return None

    idx = 1
    open_dirs = 0
    downhill_dirs = 0
    max_angles = {}
    missing_directions = []
    partial_directions = []
    valid_directions = 0
    valid_samples = 0

    for label, _bearing in _HORIZON_DIRECTIONS:
        max_angle = -90
        direction_samples = 0
        for d in distances:
            e = elevs[idx]
            idx += 1
            if e is not None:
                direction_samples += 1
                valid_samples += 1
                angle = math.degrees(math.atan2(e - center_elev, d))
                if angle > max_angle:
                    max_angle = angle
        if max_angle == -90:
            max_angles[label] = None
            missing_directions.append(label)
            continue
        if direction_samples < len(distances):
            partial_directions.append(label)
        valid_directions += 1
        max_angles[label] = round(max_angle, 1)
        if max_angle < 3:
            open_dirs += 1
        if max_angle < -2:
            downhill_dirs += 1

    # A direction-normalised score becomes misleading when only a small arc
    # has DEM coverage: one clear direction used to become 1.0 and receive the
    # full 2.5 horizon weight.  Require at least six of eight compass sectors;
    # below that, omit the factor and let the public completeness contract show
    # it as missing.  Above the threshold, scale its effective weight by actual
    # directional coverage.
    min_directions = 6
    if valid_directions < min_directions:
        return None
    openness = open_dirs / valid_directions
    downhill_bonus = min(downhill_dirs * 0.05, 0.15)
    decay = min(1.0, openness + downhill_bonus)

    return {
        "value": round(decay, 3),
        "open_directions": open_dirs,
        "downhill_directions": downhill_dirs,
        "sampled_directions": valid_directions,
        "coverage_fraction": round(
            valid_samples
            / (len(_HORIZON_DIRECTIONS) * len(distances)), 3),
        "missing_directions": missing_directions,
        "partial_directions": partial_directions,
        "degraded": bool(missing_directions or partial_directions),
        "horizon_angles": max_angles,
    }


def _source_rows(factors: dict[str, dict]) -> list[dict]:
    rows = []
    if factors.keys() & {"ocean_proximity", "inland_water"}:
        rows.append({
            "source": "Overture Maps water",
            "licence": "ODbL-1.0; derived distances only",
            "attribution": "Overture Maps Foundation and OpenStreetMap contributors",
            "role": "derived proximity to ocean and inland water",
        })
    if "building_openness" in factors:
        rows.append({
            "source": "Overture Maps buildings",
            "licence": "mixed CC BY 4.0 and ODbL-1.0 inputs; derived aggregate only",
            "attribution": "Overture Maps Foundation and contributing data providers",
            "role": "derived nearby-building counts and height bands",
        })
    green = factors.get("green_space") or {}
    signals = set(green.get("signals_used") or [])
    if "overture_places" in signals:
        rows.append({
            "source": "Overture Maps places",
            "licence": "CDLA-Permissive-2.0 / Apache-2.0 / CC0-1.0",
            "attribution": "Overture Maps Foundation and contributing data providers",
            "role": "derived park and green-destination proximity",
        })
    if "esa_worldcover" in signals:
        rows.append({
            "source": "ESA WorldCover",
            "licence": "CC BY 4.0",
            "attribution": "ESA WorldCover project 2021",
            "role": "10 m tree-canopy context",
        })
    if factors.keys() & {"elevation_advantage", "horizon_openness"}:
        rows.append({
            "source": "Geoscience Australia elevation products",
            "licence": "CC BY 4.0",
            "attribution": "Commonwealth of Australia (Geoscience Australia)",
            "role": "5 m LiDAR where available, 30 m bare-earth DEM fallback",
        })
    return rows


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------

_vq_cache: OrderedDict[tuple[float, float], tuple[dict, float]] = OrderedDict()
_VQ_CACHE_MAX = 2000
_VQ_CACHE_TTL = 3600


def view_quality_score(lat: float, lng: float) -> dict:
    """Compute Landscape Openness under the legacy API function name.

    Returns dict with score (0-100), label, and per-factor details.
    Factors without data are excluded from the weighted average.
    """
    # round(3) = ~110 m grid. round(2) (~1.1 km) served a neighbour's whole
    # payload: a house 96 m from the ocean showed "Ocean proximity 706m"
    # (2026-06-11 audit, prod md5-identical across two addresses 1.06 km apart).
    # Ocean-proximity bands are 200 m wide, so the cache cell must be smaller.
    key = (round(lat, 3), round(lng, 3))
    now = _time.time()
    if key in _vq_cache:
        cached, ts = _vq_cache[key]
        if now - ts < _VQ_CACHE_TTL:
            _vq_cache.move_to_end(key)
            return {**cached, "cached": True}
        else:
            del _vq_cache[key]

    def _run_ocean(la, ln):
        return _ocean_proximity_factor(get_db(), la, ln)
    def _run_water(la, ln):
        return _inland_water_factor(get_db(), la, ln)
    def _run_green(la, ln):
        return _green_space_factor(get_db(), la, ln)
    def _run_open(la, ln):
        return _building_openness_factor(get_db(), la, ln)

    with ThreadPoolExecutor(max_workers=6) as pool:
        f_ocean = pool.submit(_run_ocean, lat, lng)
        f_water = pool.submit(_run_water, lat, lng)
        f_elev = pool.submit(_elevation_advantage_factor, lat, lng)
        f_green = pool.submit(_run_green, lat, lng)
        f_open = pool.submit(_run_open, lat, lng)
        f_horiz = pool.submit(_horizon_openness_factor, lat, lng)

    def resolved(name, future):
        try:
            return future.result()
        except Exception:
            log.exception("landscape factor %s failed", name)
            return None

    factor_map = {
        "ocean_proximity": resolved("ocean_proximity", f_ocean),
        "inland_water": resolved("inland_water", f_water),
        "elevation_advantage": resolved("elevation_advantage", f_elev),
        "green_space": resolved("green_space", f_green),
        "building_openness": resolved("building_openness", f_open),
        "horizon_openness": resolved("horizon_openness", f_horiz),
    }

    factor_results: dict[str, dict] = {}
    active_weight = 0.0
    weighted_sum = 0.0

    for name, result in factor_map.items():
        if result:
            factor_results[name] = result
            coverage = result.get("coverage_fraction", 1.0)
            w = FACTORS[name] * max(0.0, min(1.0, coverage))
            weighted_sum += result["value"] * w
            active_weight += w

    if active_weight == 0:
        return {
            "product": "landscape_openness",
            "legacy_score_key": "view_quality",
            "assessment_level": "location_context",
            "score": None,
            "label": "Data unavailable",
            "factors": {},
            "active_factors": 0,
            "missing_factors": sorted(FACTORS),
            "partial_factors": [],
            "factor_weight_completeness": 0.0,
            "degraded": True,
            "line_of_sight": {
                "modelled": False,
                "observer_height_modelled": False,
                "window_orientation_modelled": False,
                "building_occlusion_modelled": False,
            },
            "sources": [],
        }

    score = max(0, min(100, round(weighted_sum / active_weight * 100)))
    score_floor = _coastal_escarpment_floor(factor_results)
    if score_floor is not None:
        score = max(score, score_floor)

    # Label cuts trimmed to the observed AU distribution (350-point sweep,
    # 2026-06-11): the factor sum tops out near 83 nationally, so 85+ was
    # effectively unreachable (0.9% of addresses).
    if score >= 80:
        label = "Exceptional Landscape Openness"
    elif score >= 68:
        label = "High Landscape Openness"
    elif score >= 55:
        label = "Good Landscape Openness"
    elif score >= 40:
        label = "Moderate Landscape Openness"
    elif score >= 25:
        label = "Limited Landscape Openness"
    else:
        label = "Low Landscape Openness"

    # Terrain factors silently renormalising away is exactly how prod served
    # "80 Great Views" with both DEM factors dead (Open-Meteo 429,
    # 2026-06-11). Surface the degradation instead of hiding it.
    missing = sorted(set(FACTORS) - set(factor_results))
    partial = sorted(
        name for name, value in factor_results.items()
        if value.get("degraded") is True)
    degraded = bool(missing or partial)
    result = {
        "product": "landscape_openness",
        "legacy_score_key": "view_quality",
        "assessment_level": "location_context",
        "score": score,
        "caveat": "Based on proximity to landscape features, not actual line-of-sight. Does not guarantee unobstructed views.",
        "label": label,
        "factors": factor_results,
        "active_factors": len(factor_results),
        "missing_factors": missing,
        "partial_factors": partial,
        "factor_weight_completeness": round(
            active_weight / sum(FACTORS.values()), 3),
        "degraded": degraded,
        "line_of_sight": {
            "modelled": False,
            "observer_height_modelled": False,
            "window_orientation_modelled": False,
            "building_occlusion_modelled": False,
            "terrain_horizon_directions": 8,
        },
        "sources": _source_rows(factor_results),
    }
    if score_floor is not None:
        result["score_floor"] = score_floor
        result["score_floor_reason"] = "elevated_open_coastal_escarpment"
    if degraded:
        unavailable = ", ".join(missing + partial)
        result["caveat"] = (
            f"One or more inputs were unavailable or partial ({unavailable}); "
            "the score reweights the remaining factors. " + result["caveat"])

    _vq_cache[key] = (result, _time.time())
    if len(_vq_cache) > _VQ_CACHE_MAX:
        _vq_cache.popitem(last=False)

    return result


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Compute view quality score")
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lng", type=float, required=True)
    args = parser.parse_args()

    result = view_quality_score(args.lat, args.lng)
    print(f"View Quality: {result['score']}/100 ({result['label']})")
    print(f"Active factors: {result['active_factors']}/{len(FACTORS)}")
    for name, info in result["factors"].items():
        print(f"  {name}: {info['value']:.2f} — {info}")
