"""Noise debug data — returns source coordinates for map visualization."""

import math

from property_scores.common.overture import (
    get_db, aadt_near, nfdh_near, gtfs_rail_near, rail_near, roads_near,
)
from property_scores.common.config import data_path
from property_scores.common.overture import AU_RAIL_SHAPES_FILE, PTV_SHAPES_FILE
from property_scores.noise.score import (
    noise_score, _crtn_noise, _rail_noise_freq, _rail_noise_fallback,
    _true_metres, _rail_screening_factor, RAIL_EMISSION, CLASS_TO_AADT,
    DEFAULT_SPEED_KMH,
)
from property_scores.noise.buildings import buildings_in_radius, barrier_attenuation


_STOPS_FILE = "au_rail_stops.parquet"

# State GTFS feeds carry replacement-bus and coach stops in the same stops file
# as the rail stops. Naming a rail source after "... Rail Replacement Bus Stop"
# reads as a data error to anyone looking at the output, so skip those rows and
# fall back to the next-nearest real stop.
_NON_RAIL_STOP_PATTERNS = ("rail replacement", "bus stop", "coach", "shuttle")


def _nearest_stop_name(db, lat: float, lng: float, max_dist_m: int = 500) -> str:
    stops_path = data_path(_STOPS_FILE)
    if not stops_path.exists():
        return ""
    try:
        m_per_deg = 111_320 * math.cos(math.radians(lat))
        delta = max_dist_m / 111_000 * 1.5
        rows = db.execute(f"""
            SELECT stop_name,
                   SQRT(POW((lng - {lng}) * {m_per_deg}, 2) + POW((lat - {lat}) * 111320, 2)) AS dist
            FROM read_parquet('{stops_path}')
            WHERE lng BETWEEN {lng - delta} AND {lng + delta}
              AND lat BETWEEN {lat - delta} AND {lat + delta}
            ORDER BY dist LIMIT 10
        """).fetchall()
        for name, _dist in rows:
            lowered = (name or "").lower()
            if any(p in lowered for p in _NON_RAIL_STOP_PATTERNS):
                continue
            return name
        return ""
    except Exception:
        return ""


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


def noise_debug(lat: float, lng: float, radius_m: int = 500,
                include_overture_roads: bool = True) -> dict:
    """Full noise score plus source coordinates for map visualization.

    ``include_overture_roads`` covers the modelled background segments used by
    the map's ripple animation. Callers that discard them (the licensed feed,
    where ODbL segments cannot ship) should pass False: screening those ~30
    segments is the most expensive part of this function.
    """
    result = noise_score(lat, lng, radius_m)
    db = get_db()

    # Pre-fetch buildings once for screening calculations on each source
    nearby_buildings = buildings_in_radius(db, lat, lng, radius_m)

    def _screening(src_lng: float, src_lat: float, dist_m: float) -> float:
        return barrier_attenuation(nearby_buildings, src_lng, src_lat, lng, lat, dist_m)

    aadt_sources = []
    for aadt, hv_pct, road_name, dist_m, src_lng, src_lat in aadt_near(
            db, lat, lng, radius_m, legacy_distance=True):
        hv_val = (hv_pct * 100) if hv_pct else 0.0
        l_db = _crtn_noise(int(aadt), dist_m, hv_pct=hv_val, speed_kmh=DEFAULT_SPEED_KMH)
        screening = _screening(src_lng, src_lat, dist_m)
        aadt_sources.append({
            "lat": src_lat, "lng": src_lng,
            "source": "vicroads",
            "road_name": road_name,
            "aadt": int(aadt),
            "hv_pct": round(hv_val),
            # Reported honestly; the dB used the legacy distance (see
            # score._true_metres). Keeps the map agreeing with the API.
            "distance_m": round(_true_metres(lat, lng, src_lat, src_lng)),
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
        # Must match score.py's rail_levels loop exactly, or the map/Inspector
        # sources disagree with the score that drove them (2026-08 fix; was a
        # hardcoded flat 0.6 here vs the ramp in score.py, up to ~7dB apart).
        screening = _screening(src_lng, src_lat, dist_m) * _rail_screening_factor(dist_m)
        stop_name = _nearest_stop_name(db, src_lat, src_lng)
        rail_sources.append({
            "lat": src_lat, "lng": src_lng,
            "source": "gtfs",
            "type": rail_type,
            "route": route_name,
            "stop_name": stop_name,
            "distance_m": round(dist_m),
            "db_raw": round(l_db, 1),
            "db": round(max(l_db - screening, 0), 1),
            "screening_db": round(screening, 1),
            "peak_svc_hr": round(peak_svc, 1),
        })

    if not gtfs_routes:
        for rail_class, dist_m in rail_near(db, lat, lng, radius_m,
                                            legacy_distance=True):
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

    # Top Overture road sources for the ripple animation.
    # Two-pass: cheap raw-dB filter to pick top 30 candidates, then run the
    # expensive barrier_attenuation only on those (avoids screening hundreds
    # of segments in dense CBD blocks).
    candidates = []
    if include_overture_roads:
        for road_class, dist_m, speed_kmh, src_lng, src_lat in roads_near(
                db, lat, lng, radius_m, legacy_distance=True):
            if road_class in ("footway", "path", "steps", "cycleway", "pedestrian", "track"):
                continue
            aadt_est = CLASS_TO_AADT.get(road_class, 400)
            l_db = _crtn_noise(aadt_est, dist_m)
            if l_db < 35:
                continue
            candidates.append((l_db, road_class, dist_m, src_lng, src_lat))
        # Tie-break fully: this list is truncated to 30 below, so an unstable
        # sort would change WHICH segments survive, not merely their order.
        candidates.sort(key=lambda x: (-x[0], x[2], str(x[1]), x[3], x[4]))

    overture_sources = []
    for l_db, road_class, dist_m, src_lng, src_lat in candidates[:30]:
        screening = _screening(src_lng, src_lat, dist_m)
        l_db_screened = max(l_db - screening, 0.0)
        if l_db_screened < 30:
            continue
        overture_sources.append({
            "lat": src_lat, "lng": src_lng,
            "class": road_class,
            # Reported honestly; the dB used the legacy distance.
            "distance_m": round(_true_metres(lat, lng, src_lat, src_lng)),
            "db": round(l_db_screened, 1),
            "screening_db": round(screening, 1),
        })

    # Deterministic order. The underlying SQL orders by distance but ties break
    # on DuckDB's parallel scan order, so two identical requests could return the
    # same rows in a different sequence -- measured: 13 of 69 rows reordered
    # between two consecutive runs of unchanged code. A customer diffing a saved
    # baseline (which is exactly what Foundit is doing) would see phantom churn.
    # Sort on the full row identity so ties can only resolve one way.
    def _stable(rows: list[dict]) -> list[dict]:
        return sorted(rows, key=lambda s: (
            s.get("distance_m") if s.get("distance_m") is not None else 1e9,
            str(s.get("road_name") or s.get("route") or s.get("class") or ""),
            str(s.get("type") or ""), str(s.get("source") or ""),
            -(s.get("db") or 0.0),
        ))

    aadt_sources = _stable(aadt_sources)
    nfdh_sources = _stable(nfdh_sources)
    rail_sources = _stable(rail_sources)
    overture_sources = _stable(overture_sources)

    # Identify dominant source for optional terrain profile (no API call here)
    terrain_source = None
    all_sources = aadt_sources + nfdh_sources + [s for s in rail_sources if "lat" in s]
    if all_sources:
        # max() returns the FIRST maximum, so a db_raw tie previously resolved on
        # scan order too. _stable above makes that deterministic.
        top = max(all_sources, key=lambda s: s.get("db_raw", 0))
        if top.get("distance_m", 0) >= 50:
            terrain_source = {
                "lat": top["lat"],
                "lng": top["lng"],
                "name": top.get("road_name") or top.get("route"),
                "db": top["db_raw"],
                "distance_m": top["distance_m"],
            }

    return {
        "score": result,
        "query": {"lat": lat, "lng": lng, "radius_m": radius_m},
        "sources": {
            "aadt": aadt_sources,
            "nfdh": nfdh_sources,
            "rail": rail_sources,
            "rail_shapes": rail_shapes,
            "overture_roads": overture_sources,
        },
        "terrain_source": terrain_source,
    }
