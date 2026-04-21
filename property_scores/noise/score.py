"""
Noise score using CRTN simplified formula.

Road class from Overture Maps is used as a proxy for traffic volume (AADT).
Each road segment contributes noise energy at the receiver point; contributions
are summed via energy addition (logarithmic) and converted to a 0-100 score
where 100 = quietest.
"""

import math

from property_scores.common.overture import get_db, roads_near

ROAD_NOISE_REF: dict[str, float] = {
    "motorway": 78,
    "trunk": 73,
    "primary": 70,
    "secondary": 67,
    "tertiary": 62,
    "residential": 55,
    "unclassified": 52,
}

GROUND_ABSORPTION_DB = 3.0
MIN_DISTANCE_M = 10.0
REFERENCE_DISTANCE_M = 13.5


def noise_score(lat: float, lng: float, radius_m: int = 1000,
                *, source: str | None = None) -> dict:
    """Compute noise score for a coordinate.

    Args:
        lat, lng: WGS84 coordinates.
        radius_m: Search radius for road segments.
        source: Optional local parquet path (skips S3 if provided).

    Returns:
        dict with score (0-100), estimated_db, label, road_count.
    """
    db = get_db()
    roads = roads_near(db, lat, lng, radius_m, source=source)

    total_energy = 0.0
    for road_class, dist_m in roads:
        if dist_m < MIN_DISTANCE_M:
            dist_m = MIN_DISTANCE_M
        l_ref = ROAD_NOISE_REF.get(road_class, 50)
        l_at_receiver = l_ref - 10 * math.log10(dist_m / REFERENCE_DISTANCE_M) - GROUND_ABSORPTION_DB
        total_energy += 10 ** (l_at_receiver / 10)

    if total_energy > 0:
        l_total = 10 * math.log10(total_energy)
    else:
        l_total = 30.0

    score = max(0, min(100, round(100 - (l_total - 35) * 1.5)))

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

    return {
        "score": score,
        "estimated_db": round(l_total, 1),
        "label": label,
        "road_count": len(roads),
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Compute noise score for a location")
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lng", type=float, required=True)
    parser.add_argument("--radius", type=int, default=1000)
    parser.add_argument("--source", type=str, default=None, help="Local parquet file")
    args = parser.parse_args()

    result = noise_score(args.lat, args.lng, args.radius, source=args.source)
    print(f"Noise Score: {result['score']}/100 ({result['label']})")
    print(f"Estimated: {result['estimated_db']} dB | Roads: {result['road_count']}")
