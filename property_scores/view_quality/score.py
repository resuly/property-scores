"""
View Quality score — estimates visual amenity of a location.

Five factors weighted and combined into a 0-100 score:
1. Ocean/coast proximity (weight 3.0) — Overture water features
2. Inland water proximity (weight 1.5) — rivers, lakes, reservoirs
3. Elevation advantage (weight 2.5) — Open-Meteo DEM, higher than neighbors
4. Green space proximity (weight 2.0) — parks/gardens from Overture POIs
5. Building openness (weight 2.0) — inverse of nearby building density

Score 100 = best views, 0 = most obstructed. Factors without data are
excluded from the weighted average rather than penalized.
"""

import math
import requests

from property_scores.common.overture import (
    get_db, water_near, buildings_near, pois_near,
)

OPEN_METEO_ELEV = "https://api.open-meteo.com/v1/elevation"

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


# ---------------------------------------------------------------------------
# Factor computations
# ---------------------------------------------------------------------------

def _ocean_proximity_factor(db, lat: float, lng: float) -> dict | None:
    """Score based on distance to nearest ocean/coastline."""
    rows = water_near(db, lat, lng, radius_m=10_000)
    if not rows:
        return None

    ocean_dist = None
    for cls, _sub, dist_m in rows:
        if cls and cls.lower() in OCEAN_CLASSES:
            ocean_dist = dist_m
            break

    if ocean_dist is None:
        return None

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

    return {"value": decay, "distance_m": round(ocean_dist)}


def _inland_water_factor(db, lat: float, lng: float) -> dict | None:
    """Score based on distance to nearest river/lake/reservoir."""
    rows = water_near(db, lat, lng, radius_m=3000)
    if not rows:
        return None

    water_dist = None
    for cls, _sub, dist_m in rows:
        if cls and cls.lower() in INLAND_WATER_CLASSES:
            water_dist = dist_m
            break

    if water_dist is None:
        return None

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

    return {"value": decay, "distance_m": round(water_dist)}


def _elevation_advantage_factor(lat: float, lng: float) -> dict | None:
    """Score based on elevation relative to surrounding area.

    Two-scale sampling:
    - Near ring (8 points at ~500m): detects local hilltops/ridges
    - Far ring (8 points at ~2km): detects regional elevation advantage
    Uses the better of the two advantages so hilltops AND elevated plateaus
    both score well. Also gives a baseline bonus for absolute elevation.
    """
    near_offset = 0.0045  # ~500m
    far_offset = 0.018    # ~2km
    lats = [lat]
    lngs = [lng]
    for off in (near_offset, far_offset):
        for dlat, dlng in [
            (off, 0), (-off, 0), (0, off), (0, -off),
            (off, off), (off, -off), (-off, off), (-off, -off),
        ]:
            lats.append(lat + dlat)
            lngs.append(lng + dlng)

    try:
        resp = requests.get(OPEN_METEO_ELEV, params={
            "latitude": ",".join(f"{x:.6f}" for x in lats),
            "longitude": ",".join(f"{x:.6f}" for x in lngs),
        }, timeout=10)
        resp.raise_for_status()
        elevations = resp.json().get("elevation", [])
        if not elevations or len(elevations) < 17:
            return None
    except (requests.RequestException, ValueError, KeyError):
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
    advantage_m = max(near_adv, far_adv)

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
    }


def _green_space_factor(db, lat: float, lng: float) -> dict | None:
    """Score based on number and proximity of parks/gardens within 1km."""
    pois = pois_near(db, lat, lng, radius_m=1000)

    green_distances: list[float] = []
    for cat, dist_m in pois:
        if cat and any(kw in cat.lower() for kw in GREEN_KEYWORDS):
            green_distances.append(dist_m)

    if not green_distances:
        return {"value": 0.0, "count": 0, "nearest_m": None}

    green_distances.sort()
    count = len(green_distances)
    nearest = green_distances[0]

    proximity_score = max(0.0, 1.0 - nearest / 1000)
    density_score = min(count / 15.0, 1.0)
    decay = proximity_score * 0.6 + density_score * 0.4

    return {
        "value": round(decay, 3),
        "count": count,
        "nearest_m": round(nearest),
    }


def _building_openness_factor(db, lat: float, lng: float) -> dict | None:
    """Score based on inverse of building density within 300m.

    Calibrated for Australian suburbs: a typical residential street has
    ~100-200 buildings per 300m radius. Only truly dense urban areas
    (CBD, high-rise) should score near 0.
    """
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
    dirs = [(0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1), (-1, 0), (-1, 1)]
    labels = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]

    lats = [lat]
    lngs = [lng]
    for dlat, dlng in dirs:
        for d in distances:
            off = d / 111320
            lats.append(lat + dlat * off)
            lngs.append(lng + dlng * off * math.cos(math.radians(lat)))

    try:
        resp = requests.get(OPEN_METEO_ELEV, params={
            "latitude": ",".join(f"{x:.6f}" for x in lats),
            "longitude": ",".join(f"{x:.6f}" for x in lngs),
        }, timeout=10)
        if not resp.ok:
            return None
        elevs = resp.json().get("elevation", [])
        if len(elevs) < 1 + len(dirs) * len(distances):
            return None
    except (requests.RequestException, ValueError):
        return None

    center_elev = elevs[0]
    if center_elev is None:
        return None

    idx = 1
    open_dirs = 0
    downhill_dirs = 0
    max_angles = {}

    for i, label in enumerate(labels):
        max_angle = -90
        for d in distances:
            e = elevs[idx]
            idx += 1
            if e is not None:
                angle = math.degrees(math.atan2(e - center_elev, d))
                if angle > max_angle:
                    max_angle = angle
        max_angles[label] = max_angle
        if max_angle < 3:
            open_dirs += 1
        if max_angle < -2:
            downhill_dirs += 1

    openness = open_dirs / 8
    downhill_bonus = min(downhill_dirs * 0.05, 0.15)
    decay = min(1.0, openness + downhill_bonus)

    return {
        "value": round(decay, 3),
        "open_directions": open_dirs,
        "downhill_directions": downhill_dirs,
        "horizon_angles": {k: round(v, 1) for k, v in max_angles.items()},
    }


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------

def view_quality_score(lat: float, lng: float) -> dict:
    """Compute view quality score for a coordinate.

    Returns dict with score (0-100), label, and per-factor details.
    Factors without data are excluded from the weighted average.
    """
    db = get_db()

    factor_results: dict[str, dict] = {}
    active_weight = 0.0
    weighted_sum = 0.0

    # Ocean proximity
    ocean = _ocean_proximity_factor(db, lat, lng)
    if ocean:
        factor_results["ocean_proximity"] = ocean
        w = FACTORS["ocean_proximity"]
        weighted_sum += ocean["value"] * w
        active_weight += w

    # Inland water
    water = _inland_water_factor(db, lat, lng)
    if water:
        factor_results["inland_water"] = water
        w = FACTORS["inland_water"]
        weighted_sum += water["value"] * w
        active_weight += w

    # Elevation advantage
    elev = _elevation_advantage_factor(lat, lng)
    if elev:
        factor_results["elevation_advantage"] = elev
        w = FACTORS["elevation_advantage"]
        weighted_sum += elev["value"] * w
        active_weight += w

    # Green space
    green = _green_space_factor(db, lat, lng)
    if green:
        factor_results["green_space"] = green
        w = FACTORS["green_space"]
        weighted_sum += green["value"] * w
        active_weight += w

    # Building openness
    openness = _building_openness_factor(db, lat, lng)
    if openness:
        factor_results["building_openness"] = openness
        w = FACTORS["building_openness"]
        weighted_sum += openness["value"] * w
        active_weight += w

    # Horizon openness
    horizon = _horizon_openness_factor(lat, lng)
    if horizon:
        factor_results["horizon_openness"] = horizon
        w = FACTORS["horizon_openness"]
        weighted_sum += horizon["value"] * w
        active_weight += w

    if active_weight == 0:
        return {
            "score": None,
            "label": "Data unavailable",
            "factors": {},
            "active_factors": 0,
        }

    score = max(0, min(100, round(weighted_sum / active_weight * 100)))

    if score >= 85:
        label = "Exceptional Views"
    elif score >= 70:
        label = "Great Views"
    elif score >= 55:
        label = "Good Views"
    elif score >= 40:
        label = "Average Views"
    elif score >= 25:
        label = "Limited Views"
    else:
        label = "Obstructed Views"

    return {
        "score": score,
        "caveat": "Based on proximity to landscape features, not actual line-of-sight. Does not guarantee unobstructed views.",
        "label": label,
        "factors": factor_results,
        "active_factors": len(factor_results),
    }


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
