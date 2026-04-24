"""
Walkability score using Walk Score-style distance decay.

For each of 13 amenity categories, find the nearest POI within 1.5 km and
apply a linear distance decay. Categories are weighted and summed to produce
a 0-100 score where 100 = walker's paradise.

Uses straight-line distance as a baseline. Road-network distance (via Valhalla
or OSRM) can be substituted for higher accuracy.
"""

import math

from property_scores.common.overture import get_db, pois_near, roads_near

CATEGORY_MAP: dict[str, list[str]] = {
    "grocery": ["grocery", "supermarket", "food_store", "convenience_store"],
    "restaurant": ["restaurant", "fast_food", "food"],
    "shopping": ["shopping", "clothing_store", "department_store", "mall"],
    "cafe": ["cafe", "coffee_shop", "coffee"],
    "bank": ["bank", "atm", "financial_service"],
    "park": ["park", "playground", "garden", "recreation"],
    "school": ["school", "education", "university", "college"],
    "entertainment": ["entertainment", "cinema", "theater", "museum", "arts"],
    "fitness": ["fitness", "gym", "sports", "swimming_pool"],
    "pharmacy": ["pharmacy", "drugstore"],
    "healthcare": ["hospital", "clinic", "doctor", "healthcare", "medical"],
    "transit": ["bus_station", "train_station", "transit", "subway", "tram_stop"],
    "bookstore": ["bookstore", "library", "book_store"],
}

CATEGORY_WEIGHTS: dict[str, float] = {
    "grocery": 3.0,
    "restaurant": 2.0,
    "shopping": 1.5,
    "cafe": 1.5,
    "bank": 1.0,
    "park": 2.0,
    "school": 2.0,
    "entertainment": 1.0,
    "fitness": 1.0,
    "pharmacy": 1.5,
    "healthcare": 2.0,
    "transit": 3.0,
    "bookstore": 0.5,
}

MAX_WALK_DISTANCE_M = 1500.0
BARRIER_CLASSES = {"motorway", "trunk"}
BARRIER_PENALTY = 2.5  # effective distance multiplier when crossing a barrier


def _match_category(poi_category: str | None) -> str | None:
    if not poi_category:
        return None
    poi_lower = poi_category.lower().replace(" ", "_")
    for cat, keywords in CATEGORY_MAP.items():
        for kw in keywords:
            if kw in poi_lower:
                return cat
    return None


OPEN_METEO_ELEV = "https://api.open-meteo.com/v1/elevation"


def _decay(distance_m: float) -> float:
    if distance_m >= MAX_WALK_DISTANCE_M:
        return 0.0
    return 1.0 - distance_m / MAX_WALK_DISTANCE_M


def _slope_penalty(lat: float, lng: float) -> float:
    """Estimate average walking slope from DEM. Returns 0-1 penalty multiplier.

    Samples elevation at 500m in 4 cardinal directions. Steep terrain
    makes walking harder — 10%+ grade roughly doubles effective distance.
    """
    import requests
    offset = 0.0045  # ~500m
    lats = [lat, lat + offset, lat - offset, lat, lat]
    lngs = [lng, lng, lng, lng + offset, lng - offset]
    try:
        resp = requests.get(OPEN_METEO_ELEV, params={
            "latitude": ",".join(f"{x:.6f}" for x in lats),
            "longitude": ",".join(f"{x:.6f}" for x in lngs),
        }, timeout=5)
        if not resp.ok:
            return 1.0
        elevs = resp.json().get("elevation", [])
        if len(elevs) < 5:
            return 1.0
    except Exception:
        return 1.0

    center = elevs[0]
    if center is None:
        return 1.0
    diffs = [abs(e - center) for e in elevs[1:] if e is not None]
    if not diffs:
        return 1.0

    avg_rise = sum(diffs) / len(diffs)
    grade_pct = avg_rise / 500 * 100

    if grade_pct < 3:
        return 1.0
    if grade_pct < 6:
        return 0.9
    if grade_pct < 10:
        return 0.75
    return 0.6


def walkability_score(lat: float, lng: float, radius_m: int = 1500,
                      *, source: str | None = None) -> dict:
    """Compute walkability score for a coordinate.

    Returns:
        dict with score (0-100), label, category_scores, poi_count.
    """
    db = get_db()
    pois = pois_near(db, lat, lng, radius_m, source=source)

    # Detect major road/rail barriers within the search area
    barriers = roads_near(db, lat, lng, radius_m, source=source)
    barrier_segments = [
        (dist_m, near_lng, near_lat)
        for road_class, dist_m, _, near_lng, near_lat in barriers
        if road_class in BARRIER_CLASSES
    ]

    nearest: dict[str, float] = {}
    cat_counts: dict[str, int] = {}
    for poi_cat, dist_m in pois:
        matched = _match_category(poi_cat)
        if matched:
            cat_counts[matched] = cat_counts.get(matched, 0) + 1
            if matched not in nearest or dist_m < nearest[matched]:
                nearest[matched] = dist_m

    def _effective_distance(poi_dist_m: float) -> float:
        """Check if a highway barrier lies between property and POI.

        Barrier must be at 20-80% of the distance (not at the property's
        feet or beyond the POI) to count as a genuine crossing obstacle.
        """
        if not barrier_segments or poi_dist_m < 100:
            return poi_dist_m
        lo = poi_dist_m * 0.15
        hi = poi_dist_m * 0.85
        for b_dist, _, _ in barrier_segments:
            if lo < b_dist < hi:
                return poi_dist_m * BARRIER_PENALTY
        return poi_dist_m

    total_weight = sum(CATEGORY_WEIGHTS.values())
    weighted_sum = 0.0
    category_scores = {}
    barriers_crossed = 0

    for cat, weight in CATEGORY_WEIGHTS.items():
        if cat in nearest:
            raw_dist = nearest[cat]
            eff_dist = _effective_distance(raw_dist)
            if eff_dist > raw_dist:
                barriers_crossed += 1
            d = _decay(eff_dist)
            count = cat_counts.get(cat, 0)
            if count <= 1:
                d *= 0.7
            elif count <= 2:
                d *= 0.85
            category_scores[cat] = {
                "distance_m": round(raw_dist),
                "decay": round(d, 2),
                "count": count,
                "barrier": eff_dist > raw_dist,
            }
        else:
            d = 0.0
            category_scores[cat] = {"distance_m": None, "decay": 0.0, "count": 0}
        weighted_sum += weight * d

    raw_score = round(weighted_sum / total_weight * 100)

    # Slope penalty: hilly terrain reduces walkability
    slope_mult = _slope_penalty(lat, lng)
    score = max(0, min(100, round(raw_score * slope_mult)))

    if score >= 90:
        label = "Walker's Paradise"
    elif score >= 70:
        label = "Very Walkable"
    elif score >= 50:
        label = "Somewhat Walkable"
    elif score >= 25:
        label = "Car-Dependent"
    else:
        label = "Almost All Errands Require a Car"

    result = {
        "score": score,
        "label": label,
        "disclaimer": "Based on straight-line distance to amenities with highway barrier detection. Does not use road-network routing or account for pedestrian infrastructure.",
        "category_scores": category_scores,
        "poi_count": len(pois),
    }
    if barriers_crossed > 0:
        result["barriers_crossed"] = barriers_crossed
    if slope_mult < 1.0:
        result["slope_penalty"] = round(slope_mult, 2)
    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Compute walkability score")
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lng", type=float, required=True)
    parser.add_argument("--radius", type=int, default=1500)
    parser.add_argument("--source", type=str, default=None)
    args = parser.parse_args()

    result = walkability_score(args.lat, args.lng, args.radius, source=args.source)
    print(f"Walkability: {result['score']}/100 ({result['label']})")
    print(f"POIs found: {result['poi_count']}")
    for cat, info in result["category_scores"].items():
        dist = f"{info['distance_m']}m" if info["distance_m"] else "not found"
        print(f"  {cat}: {dist} (decay: {info['decay']})")
