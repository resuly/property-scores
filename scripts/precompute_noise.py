"""Pre-compute noise scores on a grid for fast API lookups.

Usage:
  python scripts/precompute_noise.py --region melbourne-inner  # ~30 min
  python scripts/precompute_noise.py --region melbourne        # ~5 hours

Run this with the SAME environment AND the same active model as the serving
process. Every row is stamped with NOISE_MODEL_VERSION, and cache.py refuses any
grid whose stamp does not match the reader's own. Since 2026-08-18 that stamp
encodes, besides the model date:

    NOISE_TRANSFER            on/off
    NOISE_ML_CORRECTION       on/off
    NOISE_QUIET_RECAL         on/off
    NOISE_RAIL_RECAL          on/off, plus NOISE_RAIL_RECAL_DB as a VALUE
    NOISE_AADT_ADJUST         on/off, plus NOISE_AADT_ADJUST_K as a VALUE
    the resolved transfer model id, from NOISE_MODEL_ID or, failing that, the
      `active` entry in data/models/noise/registry.json (so DATA_DIR has to
      point at the same registry the service reads, not just at a data dir)

A grid baked under a different one of these is not "slightly different" to the
serving API, it is invisible to it, and the region silently loses its precompute
speed-up. Production today runs
`DATA_DIR=... NOISE_TRANSFER=1 NOISE_QUIET_RECAL=1 NOISE_RAIL_RECAL=1`; check the
systemd unit rather than trusting this line, and confirm by comparing the printed
stamp below with the service's own.

Process rule this script is the other half of: any change that moves the numbers
must bump the date token in NOISE_MODEL_VERSION or be followed immediately by a
re-bake here. See the block at NOISE_MODEL_VERSION in property_scores/noise/
score.py for why (2026-08-04: six weeks of a stale melbourne-inner grid).
"""

import argparse
import sys
import time

import numpy as np
import pandas as pd

from property_scores.noise.score import cell_score, NOISE_MODEL_VERSION
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
    cell_m = cfg["step"] * 111_320  # grid step in metres → quincunx span
    total = len(lats) * len(lngs)
    # Printed so the operator can compare it with the running service's stamp
    # (GET /version) BEFORE spending hours on a grid the service will refuse.
    print(f"Model version stamped into every row: {NOISE_MODEL_VERSION}")
    print(f"Region: {args.region}")
    print(f"Grid: {len(lats)} x {len(lngs)} = {total} points (step={cfg['step']}, cell~{cell_m:.0f}m)")
    print(f"Estimated time: {total * 0.8 * 5 / 60:.0f} minutes (quincunx = 5x per cell)")

    # Optional CPU throttle: cap DuckDB worker threads so a background precompute
    # leaves cores free for interactive work (e.g. PRECOMPUTE_THREADS=8 on a
    # 10-core box ≈ 80%). Pair with `nice -n 19`.
    import os
    _thr = os.environ.get("PRECOMPUTE_THREADS")
    if _thr:
        from property_scores.common.overture import get_db
        get_db().execute(f"SET threads TO {int(_thr)}")
        print(f"DuckDB threads capped at {_thr}")

    # Warm up
    cell_score(lats[0], lngs[0], cell_m=cell_m)

    rows = []
    t0 = time.time()
    done = 0
    errors = 0

    for lat in lats:
        for lng in lngs:
            try:
                r = cell_score(float(lat), float(lng), cell_m=cell_m)
                rows.append({
                    "lat": round(float(lat), 6),
                    "lng": round(float(lng), 6),
                    "score": r.get("score"),
                    "estimated_db": r.get("estimated_db"),
                    "road_db": r.get("road_db"),
                    "rail_db": r.get("rail_db"),
                    "label": r.get("label"),
                    "dominant_source": r.get("dominant_source"),
                    "model_version": NOISE_MODEL_VERSION,
                })
            except Exception:
                errors += 1
                rows.append({
                    "lat": round(float(lat), 6),
                    "lng": round(float(lng), 6),
                    "score": None,
                    "estimated_db": None,
                    "model_version": NOISE_MODEL_VERSION,
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
