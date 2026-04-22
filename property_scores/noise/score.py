"""
Multi-source noise score (v2) — AADT-calibrated.

Data hierarchy:
1. VicRoads AADT (ground truth, 14k+ monitored segments in VIC)
2. Overture speed_limit → calibrated AADT estimate
3. Overture road class → fallback AADT estimate

Sources: road traffic, rail/tram (from Overture rail subtype), aircraft (VicPlan overlays).
Propagation: CRTN L10 + duty-cycle correction + urban excess attenuation.

Score 0-100 where 100 = quietest.
"""

import math

from property_scores.common.overture import get_db, roads_near, rail_near, aadt_near, nfdh_near, gtfs_rail_near
from property_scores.common.au_state import detect_state
from property_scores.noise.buildings import buildings_in_radius, barrier_attenuation
from property_scores.noise.aircraft import aircraft_noise_penalty

# --- Calibrated AADT mappings (from VicRoads 2019 ground truth) ---
# VicRoads monitors arterials/highways, so these are MEDIAN values for
# monitored roads. True quiet residential streets are NOT in the dataset.
SPEED_TO_AADT: dict[int, int] = {
    110: 70_000,
    100: 53_000,  # VicRoads median
    90:  35_000,
    80:  16_000,  # VicRoads median 15,880
    70:  12_000,  # VicRoads median 12,411
    60:  8_000,   # VicRoads median 8,291
    50:  6_000,   # VicRoads median 5,982
    40:  7_000,   # VicRoads median 6,933
    30:  4_000,
    20:  2_000,
    10:  500,
    5:   100,
}

# For roads without speed limit AND without VicRoads match.
# Conservative: assumes these are the quieter roads VicRoads doesn't monitor.
CLASS_TO_AADT: dict[str, int] = {
    "motorway":     50_000,
    "trunk":        19_000,
    "primary":      11_000,
    "secondary":    6_000,
    "tertiary":     3_000,
    "residential":  400,    # true quiet back street, not VicRoads-monitored
    "unclassified": 300,
    "living_street": 100,
    "service":      150,
}

# Rail noise — SEL-based with actual frequency (PTV GTFS)
# (L_peak at ref_dist, ref_dist_m, pass_by_duration_s)
RAIL_EMISSION: dict[str, tuple[float, float, float]] = {
    "train":  (90.0, 25.0, 15.0),
    "vline":  (92.0, 25.0, 30.0),
    "tram":   (80.0, 7.5, 10.0),
}
# Fallback for Overture rail (no PTV match)
RAIL_NOISE_FALLBACK: dict[str, tuple[float, float]] = {
    "standard_gauge": (72.0, 25.0),
    "narrow_gauge":   (68.0, 25.0),
    "tram":           (65.0, 7.5),
}

GROUND_ABSORPTION_DB = 3.0
MIN_DISTANCE_M = 10.0
AMBIENT_DB = 35.0
EXCESS_ATTENUATION_DB_PER_M = 0.06
CONTINUOUS_FLOW_AADT = 15_000

ADAPTIVE_THRESHOLD_DB = 6  # include sources within 6 dB of loudest (>25% energy)
MAX_ROAD_SOURCES = 8
MAX_RAIL_SOURCES = 4
DEFAULT_SPEED_KMH = 60
NUM_FACADE_SECTORS = 8  # 45° each

# L10 → Leq: CRTN predicts L10(18h); Lden and validation use Leq
L10_TO_LEQ_DB = 3.0

# Austroads standard temporal traffic profile (urban arterial)
TRAFFIC_DAY_FRAC = 0.80    # 07:00-19:00 (12h)
TRAFFIC_EVE_FRAC = 0.12    # 19:00-23:00 (4h)
TRAFFIC_NIGHT_FRAC = 0.08  # 23:00-07:00 (8h)
_DAY_ADJ = 10 * math.log10(TRAFFIC_DAY_FRAC * 24 / 12)    # +2.04 dB
_EVE_ADJ = 10 * math.log10(TRAFFIC_EVE_FRAC * 24 / 4)     # -1.43 dB
_NIGHT_ADJ = 10 * math.log10(TRAFFIC_NIGHT_FRAC * 24 / 8)  # -6.20 dB


def _estimate_aadt(road_class: str, speed_kmh: float | None) -> int:
    if speed_kmh is not None and speed_kmh > 0:
        best_key = min(SPEED_TO_AADT.keys(), key=lambda k: abs(k - speed_kmh))
        return SPEED_TO_AADT[best_key]
    return CLASS_TO_AADT.get(road_class, 400)


def _crtn_noise(aadt: int, distance_m: float,
                hv_pct: float = 0.0, speed_kmh: float = 0.0) -> float:
    if aadt <= 0:
        return 0.0
    l10_ref = 42.2 + 10 * math.log10(aadt)
    if hv_pct > 0 and speed_kmh > 0:
        l10_ref += 10 * math.log10(1 + 5 * hv_pct / speed_kmh)
    if distance_m < MIN_DISTANCE_M:
        distance_m = MIN_DISTANCE_M
    geometric = 10 * math.log10(distance_m / 13.5)
    excess = max(0, (distance_m - 50)) * EXCESS_ATTENUATION_DB_PER_M
    duty_cycle = min(1.0, aadt / CONTINUOUS_FLOW_AADT)
    duty_correction = 10 * math.log10(duty_cycle) if duty_cycle > 0 else -30
    return max(l10_ref - geometric - GROUND_ABSORPTION_DB - excess + duty_correction, 0.0)


def _adaptive_select(levels: list[tuple[float, dict]],
                     max_n: int = MAX_ROAD_SOURCES) -> list[tuple[float, dict]]:
    if not levels:
        return []
    sorted_levels = sorted(levels, key=lambda x: x[0], reverse=True)
    peak = sorted_levels[0][0]
    filtered = [(l, d) for l, d in sorted_levels if l >= peak - ADAPTIVE_THRESHOLD_DB]
    return filtered[:max_n]


def _rail_noise_freq(rail_type: str, distance_m: float,
                     services_per_hour: float) -> float:
    """SEL-based rail noise using actual service frequency."""
    if rail_type not in RAIL_EMISSION or services_per_hour <= 0:
        return 0.0
    l_peak, ref_dist, duration = RAIL_EMISSION[rail_type]
    if distance_m < MIN_DISTANCE_M:
        distance_m = MIN_DISTANCE_M
    sel = l_peak + 10 * math.log10(duration)
    leq = sel + 10 * math.log10(services_per_hour / 3600)
    dist_atten = 10 * math.log10(distance_m / ref_dist)
    return max(leq - dist_atten - GROUND_ABSORPTION_DB, 0.0)


def _rail_noise_fallback(rail_class: str, distance_m: float) -> float:
    """Fallback for Overture rail segments without PTV frequency data."""
    if rail_class not in RAIL_NOISE_FALLBACK:
        return 0.0
    l_ref, ref_dist = RAIL_NOISE_FALLBACK[rail_class]
    if distance_m < MIN_DISTANCE_M:
        distance_m = MIN_DISTANCE_M
    return max(l_ref - 10 * math.log10(distance_m / ref_dist) - GROUND_ABSORPTION_DB, 0.0)


def _energy_sum(*levels: float) -> float:
    e = sum(10 ** (l / 10) for l in levels if l > 0)
    return 10 * math.log10(e) if e > 0 else 0.0


def _bearing(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    dx = (lng2 - lng1) * math.cos(math.radians((lat1 + lat2) / 2))
    dy = lat2 - lat1
    return math.atan2(dx, dy) % (2 * math.pi)


def _facade_lden(sources: list[tuple[float, float]], aircraft_db: float) -> dict:
    """Compute Lden per facade sector from directional sources.

    sources: list of (l10_db, bearing_rad) for road sources, or (leq_db, bearing_rad) for rail.
    Returns dict with lden_max, lden_min, and per-sector values.
    """
    sector_width = 2 * math.pi / NUM_FACADE_SECTORS
    sector_road: list[list[float]] = [[] for _ in range(NUM_FACADE_SECTORS)]
    sector_rail: list[list[float]] = [[] for _ in range(NUM_FACADE_SECTORS)]

    for db_val, bearing, is_rail in sources:
        if db_val <= 0:
            continue
        idx = int(bearing / sector_width) % NUM_FACADE_SECTORS
        if is_rail:
            sector_rail[idx].append(db_val)
        else:
            sector_road[idx].append(db_val)

    sector_ldens = []
    for i in range(NUM_FACADE_SECTORS):
        road_e = sum(10 ** (l / 10) for l in sector_road[i])
        road_db = 10 * math.log10(road_e) if road_e > 0 else 0.0
        rail_e = sum(10 ** (l / 10) for l in sector_rail[i])
        rail_db = 10 * math.log10(rail_e) if rail_e > 0 else 0.0

        road_leq = (road_db - L10_TO_LEQ_DB) if road_db > 0 else 0.0
        rail_leq = rail_db
        aircraft_leq = aircraft_db

        leq_d = max(_energy_sum(
            road_leq + _DAY_ADJ if road_leq > 0 else 0,
            rail_leq, aircraft_leq), AMBIENT_DB)
        leq_e = max(_energy_sum(
            road_leq + _EVE_ADJ if road_leq > 0 else 0,
            max(rail_leq - 5, 0) if rail_leq > 0 else 0,
            aircraft_leq), AMBIENT_DB)
        leq_n = max(_energy_sum(
            road_leq + _NIGHT_ADJ if road_leq > 0 else 0,
            0, aircraft_leq), AMBIENT_DB)

        sector_ldens.append(round(_lden(leq_d, leq_e, leq_n), 1))

    if not sector_ldens or max(sector_ldens) <= AMBIENT_DB:
        return {}

    labels = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    max_idx = sector_ldens.index(max(sector_ldens))
    min_idx = sector_ldens.index(min(sector_ldens))

    return {
        "lden_max_facade": max(sector_ldens),
        "lden_min_facade": min(sector_ldens),
        "max_facade_dir": labels[max_idx],
        "min_facade_dir": labels[min_idx],
        "facade_range_db": round(max(sector_ldens) - min(sector_ldens), 1),
    }


def _lden(leq_day: float, leq_eve: float, leq_night: float) -> float:
    """EU/AU Lden from period Leq values."""
    return 10 * math.log10(
        (12 * 10 ** (leq_day / 10)
         + 4 * 10 ** ((leq_eve + 5) / 10)
         + 8 * 10 ** ((leq_night + 10) / 10)) / 24
    )


def noise_score(lat: float, lng: float, radius_m: int = 500,
                *, source: str | None = None) -> dict:
    db = get_db()
    state = detect_state(lat, lng)

    # --- Pre-fetch buildings once for screening calculations ---
    nearby_buildings = buildings_in_radius(db, lat, lng, radius_m)

    # Collect all sources with bearing for facade analysis: (db, bearing, is_rail)
    _all_directional_sources: list[tuple[float, float, bool]] = []

    # --- Measured AADT: VicRoads (VIC) + NFDH (national) ---
    aadt_segments_raw = aadt_near(db, lat, lng, radius_m)
    # Dedup directional counts: same road + 10m distance bucket → keep max AADT
    _seen: dict[tuple, tuple] = {}
    for row in aadt_segments_raw:
        aadt_val, _, road_name, dist_m, _, _ = row
        key = (road_name, round(dist_m, -1))
        if key not in _seen or aadt_val > _seen[key][0]:
            _seen[key] = row
    aadt_segments = list(_seen.values())

    aadt_levels: list[tuple[float, dict]] = []
    building_screening_total = 0.0
    for aadt, hv_pct, road_name, dist_m, src_lng, src_lat in aadt_segments:
        hv_val = (hv_pct * 100) if hv_pct else 0.0
        l_db = _crtn_noise(int(aadt), dist_m, hv_pct=hv_val, speed_kmh=DEFAULT_SPEED_KMH)
        if l_db > 0:
            screening = barrier_attenuation(nearby_buildings, src_lng, src_lat, lng, lat, dist_m)
            l_db_screened = max(l_db - screening, 0.0)
            if screening > building_screening_total:
                building_screening_total = screening
            aadt_levels.append((l_db_screened, {
                "source": "vicroads",
                "road_name": road_name,
                "aadt": int(aadt),
                "hv_pct": round(hv_val),
                "distance_m": round(dist_m, 0),
                "db": round(l_db_screened, 1),
                "screening_db": round(screening, 1),
            }))
            _all_directional_sources.append((l_db_screened, _bearing(lat, lng, src_lat, src_lng), False))

    # NFDH national traffic counts (complements VicRoads outside VIC)
    nfdh_stations = nfdh_near(db, lat, lng, radius_m)
    for aadt, hv_pct, road_name, dist_m, src_lng, src_lat in nfdh_stations:
        if any(abs(dist_m - d) < 80 for _, _, _, d, _, _ in aadt_segments):
            continue
        hv_val = max(hv_pct or 0, 0)
        l_db = _crtn_noise(int(aadt), dist_m, hv_pct=hv_val, speed_kmh=DEFAULT_SPEED_KMH)
        if l_db > 0:
            screening = barrier_attenuation(nearby_buildings, src_lng, src_lat, lng, lat, dist_m)
            l_db_screened = max(l_db - screening, 0.0)
            if screening > building_screening_total:
                building_screening_total = screening
            aadt_levels.append((l_db_screened, {
                "source": "nfdh",
                "road_name": road_name,
                "aadt": int(aadt),
                "hv_pct": round(hv_val),
                "distance_m": round(dist_m, 0),
                "db": round(l_db_screened, 1),
                "screening_db": round(screening, 1),
            }))
            _all_directional_sources.append((l_db_screened, _bearing(lat, lng, src_lat, src_lng), False))

    # --- Overture roads (fill gaps: residential streets not in measured AADT) ---
    # Dedup: skip Overture major roads within 80m of any measured AADT source
    measured_distances = ([d for _, _, _, d, _, _ in aadt_segments]
                         + [d for _, _, _, d, _, _ in nfdh_stations])
    roads = roads_near(db, lat, lng, radius_m, source=source)
    overture_levels: list[tuple[float, dict]] = []
    roads_with_speed = 0

    for road_class, dist_m, speed_kmh, src_lng, src_lat in roads:
        if road_class in ("footway", "path", "steps", "cycleway", "pedestrian", "track"):
            continue
        if speed_kmh:
            roads_with_speed += 1
        if road_class in ("motorway", "trunk", "primary", "secondary"):
            if any(abs(dist_m - vd) < 80 for vd in measured_distances):
                continue
        aadt_est = CLASS_TO_AADT.get(road_class, 400)
        l_db = _crtn_noise(aadt_est, dist_m)
        if l_db > 0:
            screening = barrier_attenuation(nearby_buildings, src_lng, src_lat, lng, lat, dist_m)
            l_db = max(l_db - screening, 0.0)
            if screening > building_screening_total:
                building_screening_total = screening
            if l_db <= 0:
                continue
            overture_levels.append((l_db, {
                "source": "overture",
                "class": road_class,
                "speed_kmh": speed_kmh,
                "aadt_est": aadt_est,
                "distance_m": round(dist_m, 0),
                "db": round(l_db, 1),
            }))
            _all_directional_sources.append((l_db, _bearing(lat, lng, src_lat, src_lng), False))

    # Merge: prefer measured AADT for loud sources, add Overture for minor roads
    all_road_levels = aadt_levels + overture_levels
    top_roads = _adaptive_select(all_road_levels)
    road_energy = sum(10 ** (l / 10) for l, _ in top_roads)
    road_db = 10 * math.log10(road_energy) if road_energy > 0 else 0.0

    # --- Rail/tram (PTV GTFS with real frequencies) ---
    gtfs_routes = gtfs_rail_near(db, lat, lng, radius_m)
    rail_levels: list[tuple[float, dict]] = []
    nearest_tram_m = None
    nearest_train_m = None
    gtfs_found = len(gtfs_routes) > 0

    for route_type, route_name, dist_m, peak_svc, offpeak_svc in gtfs_routes:
        if route_type == 0:
            rail_type = "tram"
            if nearest_tram_m is None or dist_m < nearest_tram_m:
                nearest_tram_m = dist_m
        else:
            rail_type = "vline" if peak_svc < 4 else "train"
            if nearest_train_m is None or dist_m < nearest_train_m:
                nearest_train_m = dist_m
        svc_per_hr = peak_svc * 0.4 + offpeak_svc * 0.6
        l_db = _rail_noise_freq(rail_type, dist_m, svc_per_hr)
        if l_db > 0:
            rail_levels.append((l_db, {
                "source": "gtfs",
                "type": rail_type,
                "route": route_name,
                "distance_m": round(dist_m, 0),
                "peak_svc_hr": round(peak_svc, 1),
                "offpeak_svc_hr": round(offpeak_svc, 1),
                "db": round(l_db, 1),
            }))

    if not gtfs_found:
        rails = rail_near(db, lat, lng, radius_m, source=source)
        for rail_class, dist_m in rails:
            l_db = _rail_noise_fallback(rail_class, dist_m)
            if l_db > 0:
                rail_levels.append((l_db, {
                    "source": "overture",
                    "class": rail_class,
                    "distance_m": round(dist_m, 0),
                    "db": round(l_db, 1),
                }))
            if rail_class == "tram" and (nearest_tram_m is None or dist_m < nearest_tram_m):
                nearest_tram_m = dist_m
            if rail_class in ("standard_gauge", "narrow_gauge") and (nearest_train_m is None or dist_m < nearest_train_m):
                nearest_train_m = dist_m

    top_rails = _adaptive_select(rail_levels, max_n=MAX_RAIL_SOURCES)
    rail_energy = sum(10 ** (l / 10) for l, _ in top_rails)
    rail_db = 10 * math.log10(rail_energy) if rail_energy > 0 else 0.0

    # Rail sources: distribute across 2 opposing sectors (rail line passes through)
    for l_db, _ in top_rails:
        if l_db > 0:
            _all_directional_sources.append((l_db - 3, 0.0, True))  # spread to 2 sectors, -3dB each
            _all_directional_sources.append((l_db - 3, math.pi, True))

    # --- Aircraft noise (VicPlan MAEO/AEO overlays) ---
    aircraft = aircraft_noise_penalty(lat, lng)
    aircraft_db = 0.0
    if aircraft["penalty_db"] > 0:
        # Aircraft penalty is an ambient dB addition from ANEF contours.
        # Convert to energy for summation with road/rail sources.
        aircraft_db = AMBIENT_DB + aircraft["penalty_db"]

    # --- L10 → Leq + Lden (time-of-day) ---
    road_leq = (road_db - L10_TO_LEQ_DB) if road_db > 0 else 0.0
    rail_leq = rail_db  # SEL formula already gives Leq
    aircraft_leq = aircraft_db

    leq_24h = max(_energy_sum(road_leq, rail_leq, aircraft_leq), AMBIENT_DB)

    # Period Leq — road: Austroads temporal profile; rail: day+evening only
    leq_day_val = max(_energy_sum(
        road_leq + _DAY_ADJ if road_leq > 0 else 0,
        rail_leq,
        aircraft_leq), AMBIENT_DB)
    leq_eve_val = max(_energy_sum(
        road_leq + _EVE_ADJ if road_leq > 0 else 0,
        max(rail_leq - 5, 0) if rail_leq > 0 else 0,
        aircraft_leq), AMBIENT_DB)
    leq_night_val = max(_energy_sum(
        road_leq + _NIGHT_ADJ if road_leq > 0 else 0,
        0,  # no passenger rail at night
        aircraft_leq), AMBIENT_DB)

    lden = _lden(leq_day_val, leq_eve_val, leq_night_val)

    # Score: 40 dB → 100, 75 dB → 0 (based on Lden)
    score = max(0, min(100, round((75 - lden) / 35 * 100)))

    if score >= 80:
        label = "Very Quiet"
    elif score >= 60:
        label = "Quiet"
    elif score >= 40:
        label = "Moderate"
    elif score >= 20:
        label = "Loud"
    else:
        label = "Very Loud"

    motor_roads = [r for r in roads if r[0] not in ("footway", "path", "steps", "cycleway", "pedestrian", "track")]

    result = {
        "score": score,
        "estimated_db": round(lden, 1),
        "leq_db": round(leq_24h, 1),
        "lden_db": round(lden, 1),
        "leq_day_db": round(leq_day_val, 1),
        "leq_night_db": round(leq_night_val, 1),
        "label": label,
        "state": state,
        "road_count": len(motor_roads),
        "aadt_segments": len(aadt_segments),
        "nfdh_stations": len(nfdh_stations),
        "roads_with_speed_limit": roads_with_speed,
        "road_db": round(road_db, 1),
    }

    if rail_db > 0:
        result["rail_db"] = round(rail_db, 1)
    if nearest_tram_m is not None:
        result["nearest_tram_m"] = round(nearest_tram_m, 0)
    if nearest_train_m is not None:
        result["nearest_train_m"] = round(nearest_train_m, 0)

    if gtfs_found:
        result["rail_source"] = "gtfs"
    if top_roads:
        result["dominant_road"] = top_roads[0][1]
    if top_rails:
        result["dominant_rail"] = top_rails[0][1]
    result["dominant_source"] = top_roads[0][1].get("road_name") or top_roads[0][1].get("class") if top_roads else None
    if building_screening_total > 0:
        result["max_building_screening_db"] = round(building_screening_total, 1)

    # Facade analysis: per-sector Lden
    facade = _facade_lden(_all_directional_sources, aircraft_db)
    if facade:
        result.update(facade)

    # Aircraft noise overlay
    if aircraft["zone_code"]:
        result["aircraft"] = {
            "zone_code": aircraft["zone_code"],
            "anef_min": aircraft["anef_min"],
            "anef_max": aircraft["anef_max"],
            "penalty_db": aircraft["penalty_db"],
            "impact": aircraft["impact"],
            "airport_type": aircraft["airport_type"],
            "lga": aircraft["lga"],
        }
        result["aircraft_db"] = round(aircraft_db, 1)

    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Compute multi-source noise score")
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lng", type=float, required=True)
    parser.add_argument("--radius", type=int, default=500)
    parser.add_argument("--source", type=str, default=None)
    args = parser.parse_args()

    result = noise_score(args.lat, args.lng, args.radius, source=args.source)
    print(f"Noise Score: {result['score']}/100 ({result['label']}) — {result.get('state', '?')}")
    print(f"Lden: {result['lden_db']} dB | Leq24h: {result['leq_db']} dB | Day: {result['leq_day_db']} | Night: {result['leq_night_db']}")
    print(f"Road: {result['road_db']} dB (L10)", end="")
    if result.get("rail_db"):
        print(f" | Rail: {result['rail_db']} dB", end="")
    if result.get("aircraft_db"):
        print(f" | Aircraft: {result['aircraft_db']} dB", end="")
    print(f"\nAADT: {result['aadt_segments']} VicRoads + {result['nfdh_stations']} NFDH | Overture roads: {result['road_count']}")
    if result.get("dominant_road"):
        d = result["dominant_road"]
        src = d.get("road_name", d.get("class", "?"))
        print(f"Dominant: {src} @ {d['distance_m']}m, AADT={d.get('aadt', d.get('aadt_est', '?'))}, {d['db']} dB")
    if result.get("aircraft"):
        a = result["aircraft"]
        print(f"Aircraft: {a['zone_code']} (ANEF {a['anef_min']}"
              + (f"-{a['anef_max']}" if a['anef_max'] else "+")
              + f") +{a['penalty_db']} dB — {a['impact']}")
