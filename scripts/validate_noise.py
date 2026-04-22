"""
Validate noise model against NoiseCapture crowdsourced measurements.

Uses Melbourne Inner areas (531 hexagons with LAeq measurements) to compare
our predicted noise levels against ground truth.

Usage:
    python scripts/validate_noise.py
    python scripts/validate_noise.py --city amsterdam
"""

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from property_scores.noise.score import noise_score


def load_areas(city: str = "melbourne") -> list[dict]:
    base = Path("D:/property-scores-data/noisecapture")
    if city == "melbourne":
        files = list((base / "melbourne").glob("*.areas.geojson"))
    elif city == "amsterdam":
        files = list((base / "amsterdam").glob("*.areas.geojson"))
    else:
        raise ValueError(f"Unknown city: {city}")

    areas = []
    for f in files:
        with open(f) as fh:
            data = json.load(fh)
        for feat in data.get("features", []):
            props = feat.get("properties", {})
            geom = feat.get("geometry", {})
            if geom.get("type") != "Polygon":
                continue
            coords = geom["coordinates"][0]
            centroid_lng = sum(c[0] for c in coords) / len(coords)
            centroid_lat = sum(c[1] for c in coords) / len(coords)
            laeq = props.get("laeq")
            la50 = props.get("la50")
            count = props.get("measure_count", 0)
            if laeq and count >= 3 and float(laeq) >= 50:
                areas.append({
                    "lat": centroid_lat,
                    "lng": centroid_lng,
                    "laeq": float(laeq),
                    "la50": float(la50) if la50 else None,
                    "measure_count": count,
                })
    return areas


def validate(city: str = "melbourne"):
    areas = load_areas(city)
    print(f"Loaded {len(areas)} NoiseCapture hexagons for {city}")
    if not areas:
        print("No areas found")
        return

    errors = []
    results = []

    for i, area in enumerate(areas):
        try:
            r = noise_score(area["lat"], area["lng"])
            predicted = r.get("leq_db", r["estimated_db"])
            measured = area["laeq"]
            error = predicted - measured
            errors.append(error)
            results.append({
                "lat": area["lat"],
                "lng": area["lng"],
                "predicted": predicted,
                "measured": measured,
                "error": error,
                "score": r["score"],
                "count": area["measure_count"],
            })
            if (i + 1) % 20 == 0:
                print(f"  {i + 1}/{len(areas)} processed...")
        except Exception as e:
            print(f"  Error at ({area['lat']:.4f}, {area['lng']:.4f}): {e}")

    if not errors:
        print("No valid comparisons")
        return

    n = len(errors)
    mean_error = sum(errors) / n
    rmse = math.sqrt(sum(e ** 2 for e in errors) / n)
    mae = sum(abs(e) for e in errors) / n
    within_5 = sum(1 for e in errors if abs(e) <= 5) / n * 100
    within_10 = sum(1 for e in errors if abs(e) <= 10) / n * 100

    print(f"\n{'='*60}")
    print(f"Validation Results: {city.title()} ({n} hexagons)")
    print(f"{'='*60}")
    print(f"Mean Error (bias): {mean_error:+.1f} dB")
    print(f"RMSE:              {rmse:.1f} dB")
    print(f"MAE:               {mae:.1f} dB")
    print(f"Within 5 dB:       {within_5:.0f}%")
    print(f"Within 10 dB:      {within_10:.0f}%")

    sorted_results = sorted(results, key=lambda x: abs(x["error"]), reverse=True)
    print(f"\nWorst 5 predictions:")
    for r in sorted_results[:5]:
        print(f"  ({r['lat']:.4f}, {r['lng']:.4f}): predicted={r['predicted']:.1f}, measured={r['measured']:.1f}, error={r['error']:+.1f} dB (n={r['count']})")

    print(f"\nBest 5 predictions:")
    for r in sorted_results[-5:]:
        print(f"  ({r['lat']:.4f}, {r['lng']:.4f}): predicted={r['predicted']:.1f}, measured={r['measured']:.1f}, error={r['error']:+.1f} dB (n={r['count']})")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", default="melbourne", choices=["melbourne", "amsterdam"])
    args = parser.parse_args()
    validate(args.city)
