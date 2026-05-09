"""
Walkability score using Walk Score-style distance decay.

For each of 13 amenity categories, find the nearest POI within 1.5 km and
apply a linear distance decay. Categories are weighted and summed to produce
a 0-100 score where 100 = walker's paradise.

Uses straight-line distance as a baseline. Road-network distance (via Valhalla
or OSRM) can be substituted for higher accuracy.
"""

import math

from property_scores.common.overture import get_db, pois_near, pois_near_detailed, roads_near

# Exact Overture category → walkability scenario mapping.
# Keys are exact Overture category strings; values are (scenario, sub_type).
CATEGORY_MAP: dict[str, tuple[str, str]] = {
    # Supermarket (weekly shop)
    "supermarket": ("supermarket", "supermarket"),
    "superstore": ("supermarket", "supermarket"),
    "grocery_store": ("convenience", "grocery"),
    "specialty_grocery_store": ("convenience", "specialty"),
    "wholesale_club": ("supermarket", "wholesale"),
    # Convenience (quick buy)
    "convenience_store": ("convenience", "convenience"),
    # Childcare
    "child_care_and_day_care": ("childcare", "childcare"),
    "preschool": ("childcare", "preschool"),
    "kindergarten": ("childcare", "kindergarten"),
    # Primary school
    "elementary_school": ("primary_school", "primary"),
    "primary_school": ("primary_school", "primary"),
    # Secondary school
    "high_school": ("secondary_school", "secondary"),
    "secondary_school": ("secondary_school", "secondary"),
    "middle_school": ("secondary_school", "middle"),
    # Train station
    "train_station": ("train", "train"),
    "railway_station": ("train", "train"),
    "subway_station": ("train", "metro"),
    # Tram / bus
    "tram_stop": ("tram_bus", "tram"),
    "bus_stop": ("tram_bus", "bus"),
    "bus_station": ("tram_bus", "bus"),
    "transit_station": ("tram_bus", "transit"),
    # GP / medical clinic
    "doctor": ("gp_clinic", "gp"),
    "general_practitioner": ("gp_clinic", "gp"),
    "medical_center": ("gp_clinic", "clinic"),
    "medical_clinic": ("gp_clinic", "clinic"),
    "urgent_care": ("gp_clinic", "urgent"),
    # Hospital
    "hospital": ("hospital", "hospital"),
    "emergency_room": ("hospital", "emergency"),
    # Pharmacy
    "pharmacy": ("pharmacy", "pharmacy"),
    "drugstore": ("pharmacy", "drugstore"),
    # Park / green space
    "park": ("park", "park"),
    "playground": ("park", "playground"),
    "garden": ("park", "garden"),
    "botanical_garden": ("park", "botanical"),
    "recreation_area": ("park", "recreation"),
    "nature_reserve": ("park", "nature"),
    # Cafe
    "cafe": ("cafe", "cafe"),
    "coffee_shop": ("cafe", "coffee"),
    # Restaurant
    "restaurant": ("restaurant", "restaurant"),
    "fast_food_restaurant": ("restaurant", "fast_food"),
    # Gym / fitness
    "gym": ("fitness", "gym"),
    "fitness_center": ("fitness", "fitness"),
    "swimming_pool": ("fitness", "pool"),
    "recreation_center": ("fitness", "recreation"),
    # Shopping
    "shopping_center": ("shopping", "mall"),
    "department_store": ("shopping", "department"),
    "clothing_store": ("shopping", "clothing"),
    "shopping_mall": ("shopping", "mall"),
    # Bank
    "bank": ("bank", "bank"),
    "atm": ("bank", "atm"),
    # Library
    "library": ("library", "library"),
    "public_library": ("library", "public"),
    # Post office
    "post_office": ("post_office", "post"),
}

# Scenarios ordered by importance for a home buyer
SCENARIO_CONFIG: dict[str, dict] = {
    "supermarket":     {"weight": 3.0, "icon": "supermarket",     "label": "Supermarket",          "group": "essential"},
    "train":           {"weight": 3.0, "icon": "train",          "label": "Train Station",        "group": "essential"},
    "primary_school":  {"weight": 2.5, "icon": "school",         "label": "Primary School",       "group": "essential"},
    "gp_clinic":       {"weight": 2.5, "icon": "medical",        "label": "GP / Medical Clinic",  "group": "essential"},
    "park":            {"weight": 2.0, "icon": "park",           "label": "Park / Green Space",   "group": "essential"},
    "childcare":       {"weight": 2.0, "icon": "childcare",      "label": "Childcare / Preschool","group": "essential"},
    "pharmacy":        {"weight": 2.0, "icon": "pharmacy",       "label": "Pharmacy",             "group": "lifestyle"},
    "tram_bus":        {"weight": 2.0, "icon": "bus",            "label": "Tram / Bus Stop",      "group": "essential"},
    "cafe":            {"weight": 1.5, "icon": "cafe",           "label": "Cafe",                 "group": "lifestyle"},
    "restaurant":      {"weight": 1.5, "icon": "restaurant",     "label": "Restaurant",           "group": "lifestyle"},
    "convenience":     {"weight": 1.0, "icon": "convenience",    "label": "Convenience Store",    "group": "lifestyle"},
    "secondary_school":{"weight": 1.5, "icon": "school2",        "label": "Secondary School",     "group": "lifestyle"},
    "hospital":        {"weight": 1.5, "icon": "hospital",       "label": "Hospital",             "group": "lifestyle"},
    "fitness":         {"weight": 1.0, "icon": "fitness",        "label": "Gym / Fitness",        "group": "extra"},
    "shopping":        {"weight": 1.0, "icon": "shopping",       "label": "Shopping Centre",      "group": "extra"},
    "bank":            {"weight": 1.0, "icon": "bank",           "label": "Bank / ATM",           "group": "extra"},
    "library":         {"weight": 0.5, "icon": "library",        "label": "Library",              "group": "extra"},
    "post_office":     {"weight": 0.5, "icon": "post",           "label": "Post Office",          "group": "extra"},
}

# Overture categories to always skip (false positives)
_EXCLUDE_CATS = {
    "marketing_agency", "dance_school", "driving_school", "music_school",
    "language_school", "cooking_school", "art_school", "flight_school",
    "trade_school", "adult_education", "medical_supply", "medical_equipment",
}

# Name substrings that indicate a POI is miscategorized
_NAME_BLACKLIST = [
    "post office", "lpo", "visa", "immigration", "consulting",
    "massage", "holistic", "healing", "therapy", "beauty",
    "supply", "equipment", "wholesale", "federation", "association",
    "council", "institute", "foundation", "society", "union",
    "cosmetic", "plastic surgery", "aesthetic", "laser",
    "online", "virtual", "digital", "interactive", "software",
    "training", "academy", "coaching", "tutoring",
]

# Known supermarket chain names (AU) - if grocery_store name doesn't match, demote to convenience
_SUPERMARKET_NAMES = [
    "woolworths", "coles", "aldi", "iga", "supa iga", "costco",
    "foodworks", "drakes", "harris farm", "fresh market",
]

MAX_WALK_DISTANCE_M = 1500.0
BARRIER_CLASSES = {"motorway", "trunk"}
BARRIER_PENALTY = 2.5


def _match_category(poi_category: str | None, poi_name: str | None = None) -> str | None:
    """Map exact Overture category to our scenario. Returns scenario key."""
    if not poi_category:
        return None
    cat_lower = poi_category.lower().replace(" ", "_")
    if cat_lower in _EXCLUDE_CATS:
        return None
    entry = CATEGORY_MAP.get(cat_lower)
    if not entry:
        return None
    scenario = entry[0]
    name_lower = (poi_name or "").lower()
    if name_lower and any(bl in name_lower for bl in _NAME_BLACKLIST):
        return None
    if cat_lower in ("grocery_store", "specialty_grocery_store") and scenario == "convenience":
        if any(sn in name_lower for sn in _SUPERMARKET_NAMES):
            return "supermarket"
    if scenario == "primary_school" and name_lower:
        if "school" not in name_lower and "primary" not in name_lower:
            return None
    if scenario == "gp_clinic" and name_lower:
        if any(w in name_lower for w in ["cosmetic", "plastic", "aesthetic", "laser", "online", "montu"]):
            return None
    return scenario


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
    if source:
        pois = pois_near(db, lat, lng, radius_m, source=source)
        detailed = False
    else:
        pois_full = pois_near_detailed(db, lat, lng, radius_m)
        pois = [(cat, dist) for cat, dist, *_ in pois_full]
        detailed = True

    # Detect major road/rail barriers within the search area
    barriers = roads_near(db, lat, lng, radius_m, source=source)
    barrier_segments = [
        (dist_m, near_lng, near_lat)
        for road_class, dist_m, _, near_lng, near_lat in barriers
        if road_class in BARRIER_CLASSES
    ]

    nearest: dict[str, float] = {}
    nearest_detail: dict[str, dict] = {}
    top_pois: dict[str, list] = {}
    cat_counts: dict[str, int] = {}
    MAX_TOP = 3

    items = pois_full if detailed else [(c, d, None, None, None) for c, d in pois]
    seen_names: dict[str, set] = {}
    for poi_cat, dist_m, plng, plat, pname in items:
        matched = _match_category(poi_cat, pname)
        if matched:
            cat_counts[matched] = cat_counts.get(matched, 0) + 1
            if matched not in nearest or dist_m < nearest[matched]:
                nearest[matched] = dist_m
                if plng is not None:
                    nearest_detail[matched] = {
                        "lng": round(plng, 6), "lat": round(plat, 6),
                        "name": pname or poi_cat,
                    }
            if plng is not None and matched not in seen_names:
                seen_names[matched] = set()
            if plng is not None:
                norm = (pname or "").lower().strip()
                if norm not in seen_names.get(matched, set()):
                    if matched not in top_pois:
                        top_pois[matched] = []
                    if len(top_pois[matched]) < MAX_TOP:
                        top_pois[matched].append({
                            "lng": round(plng, 6), "lat": round(plat, 6),
                            "name": pname or poi_cat,
                            "distance_m": round(dist_m),
                        })
                        seen_names.setdefault(matched, set()).add(norm)

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

    total_weight = sum(cfg["weight"] for cfg in SCENARIO_CONFIG.values())
    weighted_sum = 0.0
    category_scores = {}
    barriers_crossed = 0

    for scenario, cfg in SCENARIO_CONFIG.items():
        weight = cfg["weight"]
        if scenario in nearest:
            raw_dist = nearest[scenario]
            eff_dist = _effective_distance(raw_dist)
            if eff_dist > raw_dist:
                barriers_crossed += 1
            d = _decay(eff_dist)
            count = cat_counts.get(scenario, 0)
            if count <= 1:
                d *= 0.7
            elif count <= 2:
                d *= 0.85
            cs = {
                "distance_m": round(raw_dist),
                "decay": round(d, 2),
                "count": count,
                "barrier": eff_dist > raw_dist,
                "icon": cfg["icon"],
                "label": cfg["label"],
                "group": cfg["group"],
            }
            if scenario in nearest_detail:
                cs["nearest"] = nearest_detail[scenario]
            if scenario in top_pois:
                cs["options"] = top_pois[scenario]
            category_scores[scenario] = cs
        else:
            d = 0.0
            category_scores[scenario] = {
                "distance_m": None, "decay": 0.0, "count": 0,
                "icon": cfg["icon"], "label": cfg["label"],
                "group": cfg["group"],
            }
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
