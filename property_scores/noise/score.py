"""
Multi-source noise score (v2) — AADT-calibrated.

Data hierarchy:
1. VicRoads AADT (ground truth, 14k+ monitored segments in VIC)
2. Overture speed_limit → calibrated AADT estimate
3. Overture road class → fallback AADT estimate

Sources: road traffic, rail/tram (from Overture rail subtype).
Propagation: CRTN L10 + duty-cycle correction + urban excess attenuation.

Score 0-100 where 100 = quietest.
"""

import math

from property_scores.common.overture import get_db, roads_near, rail_near, aadt_near
from property_scores.noise.buildings import barrier_attenuation

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

# Rail noise — time-averaged Leq (not peak pass-by)
RAIL_NOISE: dict[str, tuple[float, float]] = {
    "standard_gauge": (72.0, 25.0),
    "narrow_gauge":   (68.0, 25.0),
    "tram":           (65.0, 7.5),
}

GROUND_ABSORPTION_DB = 3.0
MIN_DISTANCE_M = 10.0
AMBIENT_DB = 35.0
EXCESS_ATTENUATION_DB_PER_M = 0.06
CONTINUOUS_FLOW_AADT = 15_000

TOP_N_ROAD_SOURCES = 3
TOP_N_RAIL_SOURCES = 2


def _estimate_aadt(road_class: str, speed_kmh: float | None) -> int:
    if speed_kmh is not None and speed_kmh > 0:
        best_key = min(SPEED_TO_AADT.keys(), key=lambda k: abs(k - speed_kmh))
        return SPEED_TO_AADT[best_key]
    return CLASS_TO_AADT.get(road_class, 400)


def _crtn_noise(aadt: int, distance_m: float) -> float:
    if aadt <= 0:
        return 0.0
    l10_ref = 42.2 + 10 * math.log10(aadt)
    if distance_m < MIN_DISTANCE_M:
        distance_m = MIN_DISTANCE_M
    geometric = 10 * math.log10(distance_m / 13.5)
    excess = max(0, (distance_m - 50)) * EXCESS_ATTENUATION_DB_PER_M
    duty_cycle = min(1.0, aadt / CONTINUOUS_FLOW_AADT)
    duty_correction = 10 * math.log10(duty_cycle) if duty_cycle > 0 else -30
    return max(l10_ref - geometric - GROUND_ABSORPTION_DB - excess + duty_correction, 0.0)


def _rail_noise(rail_class: str, distance_m: float) -> float:
    if rail_class not in RAIL_NOISE:
        return 0.0
    l_ref, ref_dist = RAIL_NOISE[rail_class]
    if distance_m < MIN_DISTANCE_M:
        distance_m = MIN_DISTANCE_M
    return max(l_ref - 10 * math.log10(distance_m / ref_dist) - GROUND_ABSORPTION_DB, 0.0)


def noise_score(lat: float, lng: float, radius_m: int = 500,
                *, source: str | None = None) -> dict:
    db = get_db()

    # --- VicRoads AADT (ground truth for major roads) ---
    aadt_segments = aadt_near(db, lat, lng, radius_m)
    aadt_levels: list[tuple[float, dict]] = []
    building_screening_total = 0.0
    for aadt, hv_pct, road_name, dist_m, src_lng, src_lat in aadt_segments:
        l_db = _crtn_noise(int(aadt), dist_m)
        if l_db > 0:
            screening = barrier_attenuation(db, src_lng, src_lat, lng, lat, dist_m)
            l_db_screened = max(l_db - screening, 0.0)
            if screening > building_screening_total:
                building_screening_total = screening
            aadt_levels.append((l_db_screened, {
                "source": "vicroads",
                "road_name": road_name,
                "aadt": int(aadt),
                "hv_pct": round(hv_pct * 100) if hv_pct else 0,
                "distance_m": round(dist_m, 0),
                "db": round(l_db_screened, 1),
                "screening_db": round(screening, 1),
            }))

    # --- Overture roads (fill gaps: residential streets not in VicRoads) ---
    # Use CLASS_TO_AADT only (conservative). If a road were busy enough to
    # matter, VicRoads would monitor it and it would appear in aadt_segments.
    roads = roads_near(db, lat, lng, radius_m, source=source)
    overture_levels: list[tuple[float, dict]] = []
    roads_with_speed = 0

    for road_class, dist_m, speed_kmh in roads:
        if road_class in ("footway", "path", "steps", "cycleway", "pedestrian", "track"):
            continue
        if speed_kmh:
            roads_with_speed += 1
        aadt_est = CLASS_TO_AADT.get(road_class, 400)
        l_db = _crtn_noise(aadt_est, dist_m)
        if l_db > 0:
            overture_levels.append((l_db, {
                "source": "overture",
                "class": road_class,
                "speed_kmh": speed_kmh,
                "aadt_est": aadt_est,
                "distance_m": round(dist_m, 0),
                "db": round(l_db, 1),
            }))

    # Merge: prefer VicRoads for loud sources, add Overture for minor roads
    all_road_levels = sorted(aadt_levels + overture_levels, key=lambda x: x[0], reverse=True)
    top_roads = all_road_levels[:TOP_N_ROAD_SOURCES]
    road_energy = sum(10 ** (l / 10) for l, _ in top_roads)
    road_db = 10 * math.log10(road_energy) if road_energy > 0 else 0.0

    # --- Rail/tram ---
    rails = rail_near(db, lat, lng, radius_m, source=source)
    rail_levels: list[tuple[float, dict]] = []
    nearest_tram_m = None
    nearest_train_m = None

    for rail_class, dist_m in rails:
        l_db = _rail_noise(rail_class, dist_m)
        if l_db > 0:
            rail_levels.append((l_db, {"class": rail_class, "distance_m": round(dist_m, 0), "db": round(l_db, 1)}))
        if rail_class == "tram" and (nearest_tram_m is None or dist_m < nearest_tram_m):
            nearest_tram_m = dist_m
        if rail_class in ("standard_gauge", "narrow_gauge") and (nearest_train_m is None or dist_m < nearest_train_m):
            nearest_train_m = dist_m

    top_rails = sorted(rail_levels, key=lambda x: x[0], reverse=True)[:TOP_N_RAIL_SOURCES]
    rail_energy = sum(10 ** (l / 10) for l, _ in top_rails)
    rail_db = 10 * math.log10(rail_energy) if rail_energy > 0 else 0.0

    # --- Energy summation ---
    total_energy = road_energy + rail_energy
    l_total = 10 * math.log10(total_energy) if total_energy > 0 else AMBIENT_DB
    l_total = max(l_total, AMBIENT_DB)

    # Score: 40 dB → 100, 75 dB → 0
    score = max(0, min(100, round((75 - l_total) / 35 * 100)))

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
        "estimated_db": round(l_total, 1),
        "label": label,
        "road_count": len(motor_roads),
        "aadt_segments": len(aadt_segments),
        "roads_with_speed_limit": roads_with_speed,
        "road_db": round(road_db, 1),
    }

    if rail_db > 0:
        result["rail_db"] = round(rail_db, 1)
    if nearest_tram_m is not None:
        result["nearest_tram_m"] = round(nearest_tram_m, 0)
    if nearest_train_m is not None:
        result["nearest_train_m"] = round(nearest_train_m, 0)

    if top_roads:
        result["dominant_road"] = top_roads[0][1]
    if top_rails:
        result["dominant_rail"] = top_rails[0][1]
    result["dominant_source"] = top_roads[0][1].get("road_name") or top_roads[0][1].get("class") if top_roads else None
    if building_screening_total > 0:
        result["max_building_screening_db"] = round(building_screening_total, 1)

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
    print(f"Noise Score: {result['score']}/100 ({result['label']})")
    print(f"Total: {result['estimated_db']} dB | Road: {result['road_db']} dB", end="")
    if result.get("rail_db"):
        print(f" | Rail: {result['rail_db']} dB", end="")
    print(f"\nAADT segments: {result['aadt_segments']} | Overture roads: {result['road_count']}")
    if result.get("dominant_road"):
        d = result["dominant_road"]
        src = d.get("road_name", d.get("class", "?"))
        print(f"Dominant: {src} @ {d['distance_m']}m, AADT={d.get('aadt', d.get('aadt_est', '?'))}, {d['db']} dB")
