"""Noise debug data — returns source coordinates for map visualization."""

import math

from property_scores.common.overture import (
    get_db, aadt_near, nfdh_near, gtfs_rail_near, rail_near,
)
from property_scores.common.config import data_path
from property_scores.common.overture import AU_RAIL_SHAPES_FILE, PTV_SHAPES_FILE
from property_scores.noise.score import (
    noise_score, _crtn_noise, _rail_noise_freq, _rail_noise_fallback,
    RAIL_EMISSION, CLASS_TO_AADT, DEFAULT_SPEED_KMH,
)
from property_scores.noise.buildings import buildings_in_radius, barrier_attenuation
from property_scores.noise.terrain import elevation_profile


def _rail_shapes_near(db, lat: float, lng: float, radius_m: int = 1000) -> list[dict]:
    """Get rail route shape geometries near a point for map drawing."""
    au_path = data_path(AU_RAIL_SHAPES_FILE)
    ptv_path = data_path(PTV_SHAPES_FILE)
    shapes_path = au_path if au_path.exists() else ptv_path
    if not shapes_path.exists():
        return []

    m_per_deg = 111_320 * math.cos(math.radians(lat))
    delta = radius_m / 111_000 * 2.0

    sql = f"""
        WITH nearby AS (
            SELECT shape_id, route_type,
                   MIN(SQRT(POW((lng - {lng}) * {m_per_deg}, 2) +
                            POW((lat - {lat}) * 111320, 2))) AS min_dist
            FROM read_parquet('{shapes_path}')
            WHERE lng BETWEEN {lng - delta} AND {lng + delta}
              AND lat BETWEEN {lat - delta} AND {lat + delta}
            GROUP BY shape_id, route_type
            HAVING min_dist < {radius_m}
        )
        SELECT s.shape_id, s.route_type, s.lat, s.lng, s.sequence
        FROM read_parquet('{shapes_path}') s
        JOIN nearby n ON s.shape_id = n.shape_id
        WHERE s.lng BETWEEN {lng - delta} AND {lng + delta}
          AND s.lat BETWEEN {lat - delta} AND {lat + delta}
        ORDER BY s.shape_id, s.sequence
    """
    rows = db.sql(sql).fetchall()

    routes = {}
    for shape_id, route_type, slat, slng, seq in rows:
        if shape_id not in routes:
            routes[shape_id] = {"type": route_type, "coords": []}
        routes[shape_id]["coords"].append([slat, slng])

    return [{"shape_id": k, "route_type": v["type"], "coords": v["coords"]}
            for k, v in routes.items()]


def noise_debug(lat: float, lng: float, radius_m: int = 500) -> dict:
    """Full noise score plus source coordinates for map visualization."""
    result = noise_score(lat, lng, radius_m)
    db = get_db()

    # Pre-fetch buildings once for screening calculations on each source
    nearby_buildings = buildings_in_radius(db, lat, lng, radius_m)

    def _screening(src_lng: float, src_lat: float, dist_m: float) -> float:
        return barrier_attenuation(nearby_buildings, src_lng, src_lat, lng, lat, dist_m)

    aadt_sources = []
    for aadt, hv_pct, road_name, dist_m, src_lng, src_lat in aadt_near(db, lat, lng, radius_m):
        hv_val = (hv_pct * 100) if hv_pct else 0.0
        l_db = _crtn_noise(int(aadt), dist_m, hv_pct=hv_val, speed_kmh=DEFAULT_SPEED_KMH)
        screening = _screening(src_lng, src_lat, dist_m)
        aadt_sources.append({
            "lat": src_lat, "lng": src_lng,
            "source": "vicroads",
            "road_name": road_name,
            "aadt": int(aadt),
            "hv_pct": round(hv_val),
            "distance_m": round(dist_m),
            "db_raw": round(l_db, 1),
            "db": round(max(l_db - screening, 0), 1),
            "screening_db": round(screening, 1),
        })

    nfdh_sources = []
    for aadt, hv_pct, road_name, dist_m, src_lng, src_lat in nfdh_near(db, lat, lng, radius_m):
        hv_val = max(hv_pct or 0, 0)
        l_db = _crtn_noise(int(aadt), dist_m, hv_pct=hv_val, speed_kmh=DEFAULT_SPEED_KMH)
        screening = _screening(src_lng, src_lat, dist_m)
        nfdh_sources.append({
            "lat": src_lat, "lng": src_lng,
            "source": "nfdh",
            "road_name": road_name,
            "aadt": int(aadt),
            "hv_pct": round(hv_val),
            "distance_m": round(dist_m),
            "db_raw": round(l_db, 1),
            "db": round(max(l_db - screening, 0), 1),
            "screening_db": round(screening, 1),
        })

    rail_sources = []
    gtfs_routes = gtfs_rail_near(db, lat, lng, radius_m)
    for route_type, route_name, dist_m, peak_svc, offpeak_svc, src_lng, src_lat in gtfs_routes:
        rail_type = "tram" if route_type == 0 else ("vline" if peak_svc < 4 else "train")
        svc_per_hr = peak_svc * 0.4 + offpeak_svc * 0.6
        l_db = _rail_noise_freq(rail_type, dist_m, svc_per_hr)
        screening = _screening(src_lng, src_lat, dist_m) * 0.6  # rail screening factor (score.py: rail_scr_factor)
        rail_sources.append({
            "lat": src_lat, "lng": src_lng,
            "source": "gtfs",
            "type": rail_type,
            "route": route_name,
            "distance_m": round(dist_m),
            "db_raw": round(l_db, 1),
            "db": round(max(l_db - screening, 0), 1),
            "screening_db": round(screening, 1),
            "peak_svc_hr": round(peak_svc, 1),
        })

    if not gtfs_routes:
        for rail_class, dist_m in rail_near(db, lat, lng, radius_m):
            l_db = _rail_noise_fallback(rail_class, dist_m)
            if l_db > 0:
                rail_sources.append({
                    "source": "overture",
                    "type": rail_class,
                    "route": rail_class,
                    "distance_m": round(dist_m),
                    "db": round(l_db, 1),
                    "db_raw": round(l_db, 1),
                    "screening_db": 0.0,
                })

    rail_shapes = _rail_shapes_near(db, lat, lng, radius_m)

    # Terrain elevation profile from receiver to dominant audible source
    terrain_profile = None
    all_sources = aadt_sources + nfdh_sources + [s for s in rail_sources if "lat" in s]
    if all_sources:
        # Pick dominant: highest db_raw (before screening)
        top = max(all_sources, key=lambda s: s.get("db_raw", 0))
        if top.get("distance_m", 0) >= 50:  # skip very close sources (profile too short)
            terrain_profile = elevation_profile(top["lat"], top["lng"], lat, lng)
            if terrain_profile:
                terrain_profile["source_name"] = top.get("road_name") or top.get("route")
                terrain_profile["source_lat"] = top["lat"]
                terrain_profile["source_lng"] = top["lng"]
                terrain_profile["source_db"] = top["db_raw"]

    return {
        "score": result,
        "query": {"lat": lat, "lng": lng, "radius_m": radius_m},
        "sources": {
            "aadt": aadt_sources,
            "nfdh": nfdh_sources,
            "rail": rail_sources,
            "rail_shapes": rail_shapes,
        },
        "terrain": terrain_profile,
    }
