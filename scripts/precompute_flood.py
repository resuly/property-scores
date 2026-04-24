"""Pre-compute flood scores on a grid for fast API lookups.

Usage:
  python scripts/precompute_flood.py --region melbourne-inner  # ~1-2 hours (remote COG)
"""

import argparse
import sys
import time

import numpy as np
import pandas as pd

from property_scores.flood.score import flood_score
from property_scores.common.config import data_path

REGIONS = {
    "melbourne-inner": {
        "lat_min": -37.86, "lat_max": -37.76,
        "lng_min": 144.90, "lng_max": 145.02,
        "step": 0.005,  # ~500m (coarser since each query is slow)
    },
    "melbourne": {
        "lat_min": -38.05, "lat_max": -37.55,
        "lng_min": 144.55, "lng_max": 145.45,
        "step": 0.01,  # ~1km
    },
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", required=True, choices=list(REGIONS.keys()))
    args = parser.parse_args()

    cfg = REGIONS[args.region]
    lats = np.arange(cfg["lat_min"], cfg["lat_max"], cfg["step"])
    lngs = np.arange(cfg["lng_min"], cfg["lng_max"], cfg["step"])
    total = len(lats) * len(lngs)
    print(f"Region: {args.region}")
    print(f"Grid: {len(lats)} x {len(lngs)} = {total} points")

    rows = []
    t0 = time.time()
    done = 0

    for lat in lats:
        for lng in lngs:
            try:
                r = flood_score(float(lat), float(lng))
                jrc = r.get("jrc", {})
                hand = r.get("hand", {})
                rows.append({
                    "lat": round(float(lat), 6),
                    "lng": round(float(lng), 6),
                    "score": r.get("score"),
                    "label": r.get("label"),
                    "flood_zones": ",".join(r.get("flood_zones", [])),
                    "jrc_flood_cells": jrc.get("flood_cells"),
                    "jrc_max_occ": jrc.get("max_occurrence_pct"),
                    "hand_m": hand.get("hand_m"),
                })
            except Exception as e:
                rows.append({"lat": round(float(lat), 6), "lng": round(float(lng), 6), "score": None})

            done += 1
            if done % 20 == 0:
                elapsed = time.time() - t0
                rate = done / elapsed
                eta = (total - done) / rate if rate > 0 else 0
                print(f"  {done}/{total} — {rate:.2f} pts/s — ETA {eta/60:.0f}m")

    df = pd.DataFrame(rows)
    out = data_path(f"flood_cache_{args.region.replace('-','_')}.parquet")
    df.to_parquet(out, index=False)
    valid = df["score"].notna().sum()
    print(f"\nDone: {valid}/{total} valid, {time.time()-t0:.0f}s")
    print(f"Saved: {out}")


if __name__ == "__main__":
    main()
