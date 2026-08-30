"""
Walkability score using Walk Score-style distance decay.

For each of 13 amenity categories, find the nearest POI within 1.5 km and
apply a linear distance decay. Categories are weighted and summed to produce
a 0-100 score where 100 = walker's paradise.

Uses straight-line distance as a baseline. Road-network distance (via Valhalla
or OSRM) can be substituted for higher accuracy.
"""

from property_scores.common.overture import (get_db, osm_amenities_near, pois_near,
                                              pois_near_detailed, rail_stops_near,
                                              road_crossings, sports_fields_near,
                                              transit_stops_near, walking_trails_near,
                                              water_crossings)

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
    "day_care_preschool": ("childcare", "childcare"),
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
    # Exact elementary_school / primary_school taxonomy is stronger than a
    # display name.  Requiring the words "school" or "primary" in the name
    # discarded hundreds of valid campuses (the truth audit found 642), e.g.
    # a campus branded only with its institution name.
    if scenario == "gp_clinic" and name_lower:
        if any(w in name_lower for w in ["cosmetic", "plastic", "aesthetic", "laser", "online", "montu"]):
            return None
    if name_lower and scenario in _FALSE_POSITIVES:
        if any(fp in name_lower for fp in _FALSE_POSITIVES[scenario]):
            return None
    return scenario


def _dedupe_named_trail_segments(items: list[tuple]) -> list[tuple]:
    """Keep the nearest segment for each named OSM trail facility."""
    out: list[tuple] = []
    positions: dict[tuple[str, str], int] = {}
    for item in items:
        poi_cat, dist_m, _lng, _lat, pname, source_id = item
        name = str(pname or "").strip().casefold()
        if source_id != "osm_named_trails" or not name:
            out.append(item)
            continue
        key = (str(poi_cat or "").strip().casefold(), name)
        position = positions.get(key)
        if position is None:
            positions[key] = len(out)
            out.append(item)
        elif float(dist_m) < float(out[position][1]):
            out[position] = item
    return out


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


def _slope_penalty(lat: float, lng: float, *, return_status: bool = False):
    """Estimate average walking slope from DEM. Returns 0-1 penalty multiplier.

    Samples elevation at 500m in 4 cardinal directions. Steep terrain
    makes walking harder — 10%+ grade roughly doubles effective distance.
    """
    offset = 0.0045  # ~500m
    coords = [(lat, lng), (lat + offset, lng), (lat - offset, lng),
              (lat, lng + offset), (lat, lng - offset)]
    elevs = _elevations(coords)
    if not elevs:
        result = (1.0, "unavailable_neutral", None)
        return result if return_status else result[0]

    center = elevs[0]
    if center is None:
        result = (1.0, "unavailable_neutral", None)
        return result if return_status else result[0]
    diffs = [abs(e - center) for e in elevs[1:] if e is not None]
    if not diffs:
        result = (1.0, "unavailable_neutral", None)
        return result if return_status else result[0]

    avg_rise = sum(diffs) / len(diffs)
    grade_pct = avg_rise / 500 * 100

    if grade_pct < 3:
        multiplier = 1.0
    elif grade_pct < 6:
        multiplier = 0.9
    elif grade_pct < 10:
        multiplier = 0.75
    else:
        multiplier = 0.6
    result = (multiplier, "data_returned", round(grade_pct, 2))
    return result if return_status else result[0]


def walkability_score(lat: float, lng: float, radius_m: int = 1500,
                      *, source: str | None = None) -> dict:
    """Compute walkability score for a coordinate.

    Returns:
        dict with score (0-100), label, category_scores, poi_count.
    """
    db = get_db()
    source_coverage: dict[str, str] = {}

    def tagged(rows, source_id: str) -> list[tuple]:
        if rows is None:
            source_coverage[source_id] = "unavailable"
            return []
        source_coverage[source_id] = (
            "data_returned" if rows else "checked_clear_within_radius")
        return [(*row, source_id) for row in rows]

    if source:
        pois = pois_near(db, lat, lng, radius_m, source=source)
        source_coverage["custom_overture_places"] = (
            "data_returned" if pois else "checked_clear_within_radius")
        detailed = False
    else:
        pois_full = tagged(
            pois_near_detailed(db, lat, lng, radius_m), "overture_places")
        # GTFS bus/tram stops: Overture places have essentially no AU bus
        # stops (zero within 1500 m of Turramurra's bus interchange), so the
        # tram_bus scenario reads official GTFS stops. Same 5-tuple shape,
        # categories bus_stop/tram_stop already map via CATEGORY_MAP.
        pois_full += tagged(
            transit_stops_near(db, lat, lng, radius_m), "gtfs_bus_tram")
        # OSM leisure polygons: council ovals are polygons, not commercial
        # POIs, so Overture misses most of them ("no sports ovals near us").
        pois_full += tagged(
            sports_fields_near(db, lat, lng, radius_m), "osm_sports")
        # Beaches/lakes come from OSM natural=beach EXCLUSIVELY: Overture's
        # beach/lake places are spam pages pinned to arbitrary coordinates
        # ("Bondi Beach" in Carlton, "Whitehaven Beach" in Brisbane CBD), so
        # both categories are dropped before the OSM beaches merge in below.
        # Must happen BEFORE the OSM merge — OSM rows share the 'beach' key.
        _ghost_beach = {"beach", "lake"}
        pois_full = [p for p in pois_full if p[0] not in _ghost_beach]
        # OSM public amenities (playground/dog park/public pool/beach):
        # commercial POI recall on public infrastructure is 26-44% holes.
        pois_full += tagged(
            osm_amenities_near(db, lat, lng, radius_m), "osm_public")
        # Overture Places stores a long trail as one representative point.
        # Transportation carries the line geometry, so query the nearest point
        # on named ODbL trail segments instead.  The rows share the POI tuple
        # contract and are credited through the OSM/ODbL category disclosure.
        pois_full += tagged(
            walking_trails_near(db, lat, lng, radius_m), "osm_named_trails")
        # Train stations come from GTFS EXCLUSIVELY: Overture both misses
        # whole new lines (Perth Morley-Ellenbrook 2024) and keeps stations
        # closed in 2014 (Newcastle) as "open", so its rail categories are
        # dropped before the GTFS stations are merged in.
        _ghost_train = {"train_station", "railway_station", "subway_station"}
        pois_full = [p for p in pois_full if p[0] not in _ghost_train]
        pois_full += tagged(
            rail_stops_near(db, lat, lng, radius_m), "gtfs_rail")
        pois = [(cat, dist) for cat, dist, *_ in pois_full]
        detailed = True

    nearest: dict[str, float] = {}
    nearest_detail: dict[str, dict] = {}
    top_pois: dict[str, list] = {}
    cat_counts: dict[str, int] = {}
    unique_facilities: set[tuple] = set()
    items = (pois_full if detailed else
             [(c, d, None, None, None, "custom_source") for c, d in pois])
    items = _dedupe_named_trail_segments(items)
    seen_names: dict[str, set] = {}
    for poi_cat, dist_m, plng, plat, pname, source_id in items:
        matched = _match_category(poi_cat, pname)
        if matched:
            facility_key = (
                (pname or poi_cat or "").lower().strip(),
                round(float(plng), 5) if plng is not None else None,
                round(float(plat), 5) if plat is not None else None,
            )
            unique_facilities.add(facility_key)
            cat_counts[matched] = cat_counts.get(matched, 0) + 1
            if matched not in nearest or dist_m < nearest[matched]:
                nearest[matched] = dist_m
                if plng is not None:
                    nearest_detail[matched] = {
                        "lng": round(plng, 6), "lat": round(plat, 6),
                        "name": pname or poi_cat,
                        "source": source_id,
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
                            "source": source_id,
                        })
                        seen_names.setdefault(matched, set()).add(norm)

    def _effective_distance(poi_dist_m: float, scenario: str = "") -> float:
        """Apply the precomputed exact road-crossing result for this POI."""
        if scenario in ("train", "tram_bus"):
            return poi_dist_m
        if scenario in road_blocked:
            return poi_dist_m * BARRIER_PENALTY
        return poi_dist_m

    # Major-water crossing test for each scenario's nearest POI: a cafe
    # across the Hunter River is not a walkable cafe. One spatial query for
    # all scenario-nearests; crossed scenarios take the barrier penalty.
    water_blocked: set = set()
    water_barrier_degraded = False
    targets = [(sc, d["lng"], d["lat"]) for sc, d in nearest_detail.items()]
    road_blocked: set = set()
    road_barrier_degraded = False
    if targets:
        checked_road_blocked = road_crossings(db, lat, lng, targets, source=source)
        if checked_road_blocked is None:
            # A broken spatial query must not silently remove every motorway
            # barrier. Conservatively penalise non-transit destinations and
            # expose the degradation in the payload.
            road_barrier_degraded = True
            road_blocked = {sc for sc, _lng, _lat in targets
                            if sc not in ("train", "tram_bus")}
        else:
            road_blocked = checked_road_blocked
        checked_water_blocked = water_crossings(db, lat, lng, targets)
        if checked_water_blocked is None:
            water_barrier_degraded = True
        else:
            water_blocked = checked_water_blocked

    total_weight = sum(cfg["weight"] for cfg in SCENARIO_CONFIG.values())
    weighted_sum = 0.0
    category_scores = {}
    barriers_crossed = 0

    for scenario, cfg in SCENARIO_CONFIG.items():
        weight = cfg["weight"]
        if scenario in nearest:
            raw_dist = nearest[scenario]
            nd = nearest_detail.get(scenario) or {}
            eff_dist = _effective_distance(raw_dist, scenario)
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
                "count_basis": (
                    "named_facilities_nearest_segment"
                    if scenario == "walking_trail"
                    else "source_rows_before_general_deduplication"
                ),
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
                "count_basis": "source_rows_before_general_deduplication",
                "icon": cfg["icon"], "label": cfg["label"],
                "group": cfg["group"],
            }
        weighted_sum += weight * d

    raw_score = round(weighted_sum / total_weight * 100)

    # Slope penalty: hilly terrain reduces walkability
    slope_assessment = _slope_penalty(lat, lng, return_status=True)
    # Older internal callers and tests may monkeypatch _slope_penalty with the
    # historical float-only shape. Preserve their score behaviour while the
    # production implementation supplies the explicit status tuple.
    if isinstance(slope_assessment, tuple):
        slope_mult, slope_status, slope_grade_pct = slope_assessment
    else:
        slope_mult, slope_status, slope_grade_pct = (
            float(slope_assessment), "test_or_legacy_override", None)
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
        summary_parts.append(
            f"{', '.join(close[:3])} within 400 m straight-line")
    if far:
        summary_parts.append(
            f"no {' or '.join(far[:2])} within {radius_m} m straight-line "
            "screening radius")
    summary = '. '.join(summary_parts) + '.' if summary_parts else None

    screening_label = (
        "Exceptional amenity proximity" if score >= 90 else
        "High amenity proximity" if score >= 70 else
        "Moderate amenity proximity" if score >= 50 else
        "Low amenity proximity" if score >= 25 else
        "Very low amenity proximity"
    )
    delivered_source_categories: dict[str, set[str]] = {}
    for scenario, category in category_scores.items():
        delivered = []
        if isinstance(category.get("nearest"), dict):
            delivered.append(category["nearest"])
        delivered.extend(item for item in category.get("options") or []
                         if isinstance(item, dict))
        for item in delivered:
            source_id = item.get("source")
            if source_id:
                delivered_source_categories.setdefault(source_id, set()).add(scenario)
    source_categories = {
        source_id: sorted(categories)
        for source_id, categories in sorted(delivered_source_categories.items())
    }
    auxiliary_unavailable = any(
        status == "unavailable" for key, status in source_coverage.items()
        if key != "custom_overture_places"
    )
    amenity_coverage = (
        "partial" if auxiliary_unavailable else
        "data_returned" if pois else "checked_clear_within_radius"
    )
    result = {
        "score": score,
        "label": label,
        "screening_label": screening_label,
        "disclaimer": (
            "Amenity screening based on straight-line distance, not a walking "
            "route or travel-time model. Motorway, major-water and regional "
            "slope checks are adjustments with their status disclosed below."
        ),
        "category_scores": category_scores,
        "poi_count": len(items),
        "poi_count_basis": "source_rows_with_named_trail_segments_deduplicated",
        "unique_facility_count": len(unique_facilities),
        "amenity_source_categories": source_categories,
        "screening_contract": {
            "schema_version": "amenity-walkability-screening-v1",
            "intended_use": "property, portfolio and neighbourhood amenity screening",
            "distance_basis": "straight_line_metres",
            "route_network_time": "not_computed",
            "search_radius_m": radius_m,
            "scenario_count": len(SCENARIO_CONFIG),
            "count_contract": (
                "category count and poi_count use source rows except named OSM "
                "trails, which keep the nearest segment per named facility; "
                "unique_facility_count deduplicates name/coordinate across scenarios"
            ),
            "transit_mode_boundary": (
                "The current GTFS rail-stop snapshot combines rail, metro and "
                "tram services and does not deliver route_type; train and "
                "tram_bus evidence can overlap. Explicit bus-station, rail-"
                "replacement-bus and tram-stop names are excluded from the "
                "train scenario."
            ),
            "barrier_distance_multiplier": BARRIER_PENALTY,
            "professional_or_statutory_reliance": "not_permitted",
        },
        "coverage": {
            "amenities": amenity_coverage,
            "amenity_sources": source_coverage,
            "road_barrier": (
                "unavailable_conservative" if road_barrier_degraded
                else "data_returned"
            ),
            "water_barrier": (
                "unavailable_unadjusted" if water_barrier_degraded
                else "data_returned"
            ),
            "slope": slope_status,
        },
    }
    if slope_grade_pct is not None:
        result["slope_grade_proxy_pct"] = slope_grade_pct
    _delivered_osm = sorted({
        scenario for source_id, scenarios in source_categories.items()
        if source_id.startswith("osm_") for scenario in scenarios
    })
    if _delivered_osm:
        result["osm_amenity_categories"] = _delivered_osm
    _delivered_gtfs = sorted({
        scenario for source_id, scenarios in source_categories.items()
        if source_id.startswith("gtfs_") for scenario in scenarios
    })
    if _delivered_gtfs:
        result["gtfs_amenity_categories"] = _delivered_gtfs
    if summary:
        result["summary"] = summary
    if barriers_crossed > 0:
        result["barriers_crossed"] = barriers_crossed
    if road_barrier_degraded:
        result["road_barrier_check"] = "unavailable_conservative"
        result["disclaimer"] += (
            " The motorway crossing check was unavailable; non-transit "
            "destinations were conservatively treated as barrier-crossed.")
    if water_barrier_degraded:
        result["water_barrier_check"] = "unavailable_unadjusted"
        result["disclaimer"] += (
            " The major-water crossing check was unavailable; no water "
            "penalty was applied, so inspect the destination evidence before use.")
    if slope_status == "unavailable_neutral":
        result["disclaimer"] += (
            " Local elevation coverage was unavailable; no slope penalty was applied.")
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
