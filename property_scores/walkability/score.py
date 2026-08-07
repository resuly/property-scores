"""
Walkability score using Walk Score-style distance decay.

For each of 13 amenity categories, find the nearest POI within 1.5 km and
apply a linear distance decay. Categories are weighted and summed to produce
a 0-100 score where 100 = walker's paradise.

Uses straight-line distance as a baseline. Road-network distance (via Valhalla
or OSRM) can be substituted for higher accuracy.
"""

import math

from property_scores.common.overture import (get_db, osm_amenities_near, pois_near,
                                              pois_near_detailed, rail_stops_near,
                                              roads_near, sports_fields_near,
                                              transit_stops_near, water_crossings)

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
    "memorial_park": ("park", "memorial"),
    "garden": ("park", "garden"),
    "botanical_garden": ("park", "botanical"),
    "recreation_area": ("park", "recreation"),
    "nature_reserve": ("park", "nature"),
    "national_park": ("park", "national"),
    # Playground / kids
    "playground": ("playground", "playground"),
    "skate_park": ("playground", "skate"),
    "kids_recreation_and_party": ("playground", "kids"),
    # Dog park
    "dog_park": ("dog_park", "dog"),
    # Sports / oval
    "sports_and_recreation_venue": ("sports", "venue"),
    "soccer_field": ("sports", "soccer"),
    "sports_club_and_league": ("sports", "club"),
    # Walking trails
    "hiking_trail": ("walking_trail", "hiking"),
    "mountain_bike_trails": ("walking_trail", "trail"),
    # Water / beach
    "beach": ("beach", "beach"),
    "lake": ("beach", "lake"),
    "swimming_pool": ("pool", "pool"),
    # Cafe
    "cafe": ("cafe", "cafe"),
    "coffee_shop": ("cafe", "coffee"),
    # Restaurant
    "restaurant": ("restaurant", "restaurant"),
    "fast_food_restaurant": ("restaurant", "fast_food"),
    # Gym / fitness
    "gym": ("fitness", "gym"),
    "fitness_center": ("fitness", "fitness"),
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
    "playground":      {"weight": 1.5, "icon": "playground",     "label": "Playground",           "group": "essential"},
    "dog_park":        {"weight": 1.0, "icon": "dog",            "label": "Dog Park",             "group": "lifestyle"},
    "walking_trail":   {"weight": 1.0, "icon": "trail",          "label": "Walking / Hiking Trail","group": "lifestyle"},
    "beach":           {"weight": 1.0, "icon": "beach",          "label": "Beach / Lake",         "group": "lifestyle"},
    "sports":          {"weight": 1.0, "icon": "sports",         "label": "Sports Oval / Field",  "group": "lifestyle"},
    "pool":            {"weight": 0.5, "icon": "pool",           "label": "Swimming Pool",        "group": "extra"},
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

# Known supermarket chain names (AU)
_SUPERMARKET_NAMES = [
    "woolworths", "coles", "aldi", "iga", "supa iga", "costco",
    "foodworks", "drakes", "harris farm", "fresh market",
    "nqr", "save on", "supabarn", "ritchies", "romeo's",
]

# Names that are definitely NOT what the category says
_FALSE_POSITIVES = {
    "supermarket": ["safety", "rsea", "auto", "car wash", "pet", "hardware", "timber"],
    "primary_school": ["learning centre", "tutor", "coaching", "cpd", "tj7"],
    "gp_clinic": ["imaging", "radiology", "pathology", "veterinary", "dental",
                  "physiotherapy", "chiropractic", "osteopath", "podiatry"],
    "hospital": ["medical centre", "medical clinic", "health centre", "imaging"],
    "pharmacy": ["nursing", "midwifery", "association", "federation"],
    "childcare": ["doncaster", "ringwood", "berwick", "frankston"],  # wrong suburb in POI name = bad coords
}

MAX_WALK_DISTANCE_M = 1500.0
BARRIER_CLASSES = {"motorway", "trunk"}
BARRIER_PENALTY = 2.5
# These are data rows in a licensed property response, not a presentation
# carousel. Foundit caught the old contradiction directly: `count: 8` beside
# three `options`. Keep the compact three-option boundary for high-volume
# amenity categories (restaurants, cafes, stops, etc.), but return every
# distinct school the radius query found.
_UNCAPPED_OPTION_SCENARIOS = frozenset({"primary_school", "secondary_school"})
_DEFAULT_OPTIONS_LIMIT = 3


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
    if name_lower and scenario in _FALSE_POSITIVES:
        if any(fp in name_lower for fp in _FALSE_POSITIVES[scenario]):
            return None
    return scenario


def _decay(distance_m: float) -> float:
    if distance_m >= MAX_WALK_DISTANCE_M:
        return 0.0
    return 1.0 - distance_m / MAX_WALK_DISTANCE_M


def _elevations(coords: list[tuple[float, float]]) -> list | None:
    """Elevation at each (lat,lng) from the local 30 m DEM (GA DEM-H bare-earth
    for all AU tiles since 2026-07-15; the leftover Copernicus tiles are EU/US
    training-region cells no AU address samples).

    2026-08-02: dropped the api.open-meteo.com fallback that used to cover
    points outside local DEM coverage (data/global/dem.vrt, populated AU) —
    DA Leads is a paid commercial product and Open-Meteo's free-tier
    elevation endpoint is non-commercial-use-only (open-meteo.com/en/terms).
    Outside coverage this now returns None; `_slope_penalty` already treats
    that as "no penalty" (1.0), the same honest-degradation pattern used
    elsewhere in this module.
    """
    try:
        from property_scores.common import terrain
        if terrain.available():
            local = [terrain.elevation(la, ln) for la, ln in coords]
            if local[0] is not None and any(e is not None for e in local[1:]):
                return local
    except Exception:
        pass
    return None


def _slope_penalty(lat: float, lng: float) -> float:
    """Estimate average walking slope from DEM. Returns 0-1 penalty multiplier.

    Samples elevation at 500m in 4 cardinal directions. Steep terrain
    makes walking harder — 10%+ grade roughly doubles effective distance.
    """
    offset = 0.0045  # ~500m
    coords = [(lat, lng), (lat + offset, lng), (lat - offset, lng),
              (lat, lng + offset), (lat, lng - offset)]
    elevs = _elevations(coords)
    if not elevs:
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
    # Categories whose delivered name and coordinate may come from
    # OpenStreetMap (ODbL-1.0) rather than the permissively licensed Overture
    # places stream. Collected as the streams merge, so the attribution
    # downstream follows the data instead of a hardcoded list. Initialised
    # outside the branch: the `source` path never fills it, but the result
    # block below reads it either way.
    _osm_cats: set = set()
    if source:
        pois = pois_near(db, lat, lng, radius_m, source=source)
        detailed = False
    else:
        pois_full = pois_near_detailed(db, lat, lng, radius_m)
        # GTFS bus/tram stops: Overture places have essentially no AU bus
        # stops (zero within 1500 m of Turramurra's bus interchange), so the
        # tram_bus scenario reads official GTFS stops. Same 5-tuple shape,
        # categories bus_stop/tram_stop already map via CATEGORY_MAP.
        pois_full = pois_full + transit_stops_near(db, lat, lng, radius_m)
        # OSM leisure polygons: council ovals are polygons, not commercial
        # POIs, so Overture misses most of them ("no sports ovals near us").
        _sports_rows = sports_fields_near(db, lat, lng, radius_m)
        pois_full = pois_full + _sports_rows
        _osm_cats |= {row[0] for row in _sports_rows}
        # Beaches/lakes come from OSM natural=beach EXCLUSIVELY: Overture's
        # beach/lake places are spam pages pinned to arbitrary coordinates
        # ("Bondi Beach" in Carlton, "Whitehaven Beach" in Brisbane CBD), so
        # both categories are dropped before the OSM beaches merge in below.
        # Must happen BEFORE the OSM merge — OSM rows share the 'beach' key.
        _ghost_beach = {"beach", "lake"}
        pois_full = [p for p in pois_full if p[0] not in _ghost_beach]
        # OSM public amenities (playground/dog park/public pool/beach):
        # commercial POI recall on public infrastructure is 26-44% holes.
        _osm_rows = osm_amenities_near(db, lat, lng, radius_m)
        pois_full = pois_full + _osm_rows
        # Which categories drew on OpenStreetMap. The delivered payload names
        # and locates the nearest place in each category, and OSM is ODbL-1.0,
        # so the consumer has to be able to credit it. Recording the categories
        # here rather than hardcoding a list downstream keeps the credit tied to
        # the data: change what the OSM streams cover and the attribution
        # follows. Deliberately errs toward crediting -- for playground, dog
        # park and pool the OSM rows are merged alongside Overture's, so the
        # specific nearest may have come from either, and over-crediting is
        # harmless where under-crediting is not. Beach is OSM-only by the drop
        # above, sports by the merge below.
        _osm_cats |= {row[0] for row in _osm_rows}
        # Train stations come from GTFS EXCLUSIVELY: Overture both misses
        # whole new lines (Perth Morley-Ellenbrook 2024) and keeps stations
        # closed in 2014 (Newcastle) as "open", so its rail categories are
        # dropped before the GTFS stations are merged in.
        _ghost_train = {"train_station", "railway_station", "subway_station"}
        pois_full = [p for p in pois_full if p[0] not in _ghost_train]
        pois_full = pois_full + rail_stops_near(db, lat, lng, radius_m)
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
                    option_limit = (None if matched in _UNCAPPED_OPTION_SCENARIOS
                                    else _DEFAULT_OPTIONS_LIMIT)
                    if option_limit is None or len(top_pois[matched]) < option_limit:
                        top_pois[matched].append({
                            "lng": round(plng, 6), "lat": round(plat, 6),
                            "name": pname or poi_cat,
                            "distance_m": round(dist_m),
                        })
                        seen_names.setdefault(matched, set()).add(norm)

    def _bearing(to_lng: float, to_lat: float) -> float:
        return math.degrees(math.atan2(to_lng - lng, to_lat - lat)) % 360

    def _effective_distance(poi_dist_m: float, scenario: str = "",
                            poi_lng: float | None = None,
                            poi_lat: float | None = None) -> float:
        """Check if a highway barrier lies between property and POI.

        Barrier must be at 15-85% of the distance AND, when the POI position
        is known, within +-50 degrees of the POI's bearing. The old scalar
        test penalised a supermarket due north for a motorway due south
        (Hughesdale: all 11 qualifying segments at 165-210 degrees behind the
        property; Bay Run scored 16/100 with 21 phantom barriers,
        2026-06-11 audit).

        Transit destinations (train / tram / bus) sit on or across the road
        and rail corridors by nature, and crossing a main road to reach them
        is a normal, signalised part of the walk, so transit stays exempt.
        """
        if not barrier_segments or poi_dist_m < 100:
            return poi_dist_m
        if scenario in ("train", "tram_bus"):
            return poi_dist_m
        lo = poi_dist_m * 0.15
        hi = poi_dist_m * 0.85
        poi_b = (_bearing(poi_lng, poi_lat)
                 if poi_lng is not None and poi_lat is not None else None)
        for b_dist, b_lng, b_lat in barrier_segments:
            if not (lo < b_dist < hi):
                continue
            if poi_b is not None and b_lng is not None:
                diff = abs((_bearing(b_lng, b_lat) - poi_b + 180) % 360 - 180)
                if diff > 50:
                    continue
            return poi_dist_m * BARRIER_PENALTY
        return poi_dist_m

    # Major-water crossing test for each scenario's nearest POI: a cafe
    # across the Hunter River is not a walkable cafe. One spatial query for
    # all scenario-nearests; crossed scenarios take the barrier penalty.
    water_blocked: set = set()
    targets = [(sc, d["lng"], d["lat"]) for sc, d in nearest_detail.items()]
    if targets:
        water_blocked = water_crossings(db, lat, lng, targets)

    total_weight = sum(cfg["weight"] for cfg in SCENARIO_CONFIG.values())
    weighted_sum = 0.0
    category_scores = {}
    barriers_crossed = 0

    for scenario, cfg in SCENARIO_CONFIG.items():
        weight = cfg["weight"]
        if scenario in nearest:
            raw_dist = nearest[scenario]
            nd = nearest_detail.get(scenario) or {}
            eff_dist = _effective_distance(raw_dist, scenario,
                                           nd.get("lng"), nd.get("lat"))
            if scenario in water_blocked:
                eff_dist = max(eff_dist, raw_dist * BARRIER_PENALTY)
            if eff_dist > raw_dist:
                barriers_crossed += 1
            d = _decay(eff_dist)
            scoring_count = cat_counts.get(scenario, 0)
            if scoring_count <= 1:
                d *= 0.7
            elif scoring_count <= 2:
                d *= 0.85
            # On the detailed production path, school `options` are the
            # distinct, locatable schools we can actually deliver. Publish a
            # count that describes that list. Keep the pre-existing raw count
            # for score weighting above so this disclosure fix does not move
            # customers' walkability baselines.
            count = scoring_count
            if scenario in _UNCAPPED_OPTION_SCENARIOS and scenario in top_pois:
                count = len(top_pois[scenario])
            cs = {
                "distance_m": round(raw_dist),
                "decay": round(d, 2),
                "count": count,
                "barrier": eff_dist > raw_dist,
                "water_barrier": scenario in water_blocked,
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

    # Generate summary
    essentials = ["supermarket", "train", "primary_school", "gp_clinic", "park", "tram_bus"]
    close = [SCENARIO_CONFIG[s]["label"] for s in essentials
             if s in nearest and nearest[s] < 400]
    far = [SCENARIO_CONFIG[s]["label"] for s in essentials
           if s not in nearest or nearest[s] >= 1000]
    summary_parts = []
    if close:
        summary_parts.append(f"{', '.join(close[:3])} within 5 min walk")
    if far:
        summary_parts.append(f"no {' or '.join(far[:2])} within walking distance")
    summary = '. '.join(summary_parts) + '.' if summary_parts else None

    result = {
        "score": score,
        "label": label,
        "disclaimer": "Based on straight-line distance to amenities with highway barrier detection.",
        "category_scores": category_scores,
        "poi_count": len(pois),
    }
    # Categories whose delivered `nearest` name and coordinate may have come
    # from OpenStreetMap. Only those actually present in this response, so a
    # consumer can credit exactly what it received. See the merge points above
    # for why this is collected rather than hardcoded.
    _delivered_osm = sorted(c for c in _osm_cats
                            if (category_scores.get(c) or {}).get("nearest"))
    if _delivered_osm:
        result["osm_amenity_categories"] = _delivered_osm
    if summary:
        result["summary"] = summary
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
