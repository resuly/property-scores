"""
Walkability score using Walk Score-style distance decay.

For each of 13 amenity categories, find the nearest POI within 1.5 km and
apply a linear distance decay. Categories are weighted and summed to produce
a 0-100 score where 100 = walker's paradise.

Uses straight-line distance as a baseline. Road-network distance (via Valhalla
or OSRM) can be substituted for higher accuracy.
"""

from property_scores.common.overture import get_db, pois_near

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


def _match_category(poi_category: str | None) -> str | None:
    if not poi_category:
        return None
    poi_lower = poi_category.lower().replace(" ", "_")
    for cat, keywords in CATEGORY_MAP.items():
        for kw in keywords:
            if kw in poi_lower:
                return cat
    return None


def _decay(distance_m: float) -> float:
    if distance_m >= MAX_WALK_DISTANCE_M:
        return 0.0
    return 1.0 - distance_m / MAX_WALK_DISTANCE_M


def walkability_score(lat: float, lng: float, radius_m: int = 1500,
                      *, source: str | None = None) -> dict:
    """Compute walkability score for a coordinate.

    Returns:
        dict with score (0-100), label, category_scores, poi_count.
    """
    db = get_db()
    pois = pois_near(db, lat, lng, radius_m, source=source)

    nearest: dict[str, float] = {}
    for poi_cat, dist_m in pois:
        matched = _match_category(poi_cat)
        if matched and (matched not in nearest or dist_m < nearest[matched]):
            nearest[matched] = dist_m

    total_weight = sum(CATEGORY_WEIGHTS.values())
    weighted_sum = 0.0
    category_scores = {}

    for cat, weight in CATEGORY_WEIGHTS.items():
        if cat in nearest:
            d = _decay(nearest[cat])
            category_scores[cat] = {
                "distance_m": round(nearest[cat]),
                "decay": round(d, 2),
            }
        else:
            d = 0.0
            category_scores[cat] = {"distance_m": None, "decay": 0.0}
        weighted_sum += weight * d

    score = max(0, min(100, round(weighted_sum / total_weight * 100)))

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

    return {
        "score": score,
        "label": label,
        "category_scores": category_scores,
        "poi_count": len(pois),
    }


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
