"""Pre-compute noise scores on a grid for fast API lookups.

Usage:
  python scripts/precompute_noise.py --region melbourne-inner  # ~30 min
  python scripts/precompute_noise.py --region melbourne        # ~5 hours
"""

import argparse
import sys
import time

import numpy as np
import pandas as pd

from property_scores.noise.score import noise_score
from property_scores.common.config import data_path

REGIONS = {
    "melbourne-inner": {
        "lat_min": -37.86, "lat_max": -37.76,
        "lng_min": 144.90, "lng_max": 145.02,
        "step": 0.002,  # ~200m
    },
    "melbourne": {
        "lat_min": -38.05, "lat_max": -37.55,
        "lng_min": 144.55, "lng_max": 145.45,
        "step": 0.005,  # ~500m
    },
    "sydney-inner": {
        "lat_min": -33.92, "lat_max": -33.82,
        "lng_min": 151.15, "lng_max": 151.28,
        "step": 0.002,
    },
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", required=True, choices=list(REGIONS.keys()))
    parser.add_argument("--radius", type=int, default=500)
    args = parser.parse_args()

    cfg = REGIONS[args.region]
    lats = np.arange(cfg["lat_min"], cfg["lat_max"], cfg["step"])
    lngs = np.arange(cfg["lng_min"], cfg["lng_max"], cfg["step"])
    total = len(lats) * len(lngs)
    print(f"Region: {args.region}")
    print(f"Grid: {len(lats)} x {len(lngs)} = {total} points (step={cfg['step']})")
    print(f"Estimated time: {total * 0.8 / 60:.0f} minutes")

    # Warm up
    noise_score(lats[0], lngs[0], args.radius)

    rows = []
    t0 = time.time()
    done = 0
    errors = 0

    for lat in lats:
        for lng in lngs:
            try:
                r = noise_score(float(lat), float(lng), args.radius)
                rows.append({
                    "lat": round(float(lat), 6),
                    "lng": round(float(lng), 6),
                    "score": r.get("score"),
                    "estimated_db": r.get("estimated_db"),
                    "road_db": r.get("road_db"),
                    "rail_db": r.get("rail_db"),
                    "label": r.get("label"),
                    "dominant_source": r.get("dominant_source"),
                })
            except Exception:
                errors += 1
                rows.append({
                    "lat": round(float(lat), 6),
                    "lng": round(float(lng), 6),
                    "score": None,
                    "estimated_db": None,
                })

            done += 1
            if done % 100 == 0:
                elapsed = time.time() - t0
                rate = done / elapsed
                eta = (total - done) / rate
                print(f"  {done}/{total} ({done*100//total}%) — "
                      f"{rate:.1f} pts/s — ETA {eta/60:.0f}m")

    df = pd.DataFrame(rows)
    out = data_path(f"noise_cache_{args.region.replace('-','_')}.parquet")
    df.to_parquet(out, index=False)

    elapsed = time.time() - t0
    valid = df["score"].notna().sum()
    print(f"\nDone: {valid}/{total} valid, {errors} errors")
    print(f"Time: {elapsed/60:.1f} minutes ({elapsed/total:.2f}s per point)")
    print(f"Saved: {out} ({out.stat().st_size/1024:.0f}KB)")


if __name__ == "__main__":
    main()
