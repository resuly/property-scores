"""Pre-compute ERA5 P95/P99 daily rainfall grid for Australia.

Creates a ~1500-point grid at 1° resolution, queries Open-Meteo for 6 years
of daily precipitation, computes percentiles, and saves as parquet.

Output: data/era5_rainfall_p95.parquet (~50KB)
Usage: python scripts/precompute_era5_p95.py
"""

import time
import sys

import numpy as np
import pandas as pd
import requests

OPEN_METEO = "https://archive-api.open-meteo.com/v1/archive"
START_DATE = "2019-01-01"
END_DATE = "2024-12-31"
BATCH_SIZE = 4

AU_LAT_RANGE = np.arange(-44, -9, 1.0)
AU_LNG_RANGE = np.arange(112, 155, 1.0)


def fetch_batch(lats: list[float], lngs: list[float]) -> list[dict]:
    resp = requests.get(OPEN_METEO, params={
        "latitude": ",".join(f"{l:.1f}" for l in lats),
        "longitude": ",".join(f"{l:.1f}" for l in lngs),
        "start_date": START_DATE,
        "end_date": END_DATE,
        "daily": "precipitation_sum",
        "timezone": "auto",
    }, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        data = [data]
    return data


def compute_percentiles(precip: list) -> dict:
    valid = [p for p in precip if p is not None and p > 0]
    if len(valid) < 30:
        return {"p95_mm": None, "p99_mm": None, "max_mm": None, "rain_days_yr": 0}
    sorted_v = sorted(valid)
    n_years = 6
    return {
        "p95_mm": round(sorted_v[int(len(sorted_v) * 0.95)], 1),
        "p99_mm": round(sorted_v[int(len(sorted_v) * 0.99)], 1),
        "max_mm": round(max(valid), 1),
        "rain_days_yr": round(len(valid) / n_years, 0),
    }


def main():
    grid = [(lat, lng) for lat in AU_LAT_RANGE for lng in AU_LNG_RANGE]
    print(f"Grid: {len(grid)} points ({len(AU_LAT_RANGE)} lat x {len(AU_LNG_RANGE)} lng)")

    rows = []
    total = len(grid)
    t0 = time.time()

    for i in range(0, total, BATCH_SIZE):
        batch = grid[i:i + BATCH_SIZE]
        lats = [p[0] for p in batch]
        lngs = [p[1] for p in batch]

        for attempt in range(5):
            try:
                results = fetch_batch(lats, lngs)
                for j, res in enumerate(results):
                    precip = res.get("daily", {}).get("precipitation_sum", [])
                    stats = compute_percentiles(precip)
                    stats["lat"] = lats[j]
                    stats["lng"] = lngs[j]
                    rows.append(stats)
                time.sleep(0.5)  # rate limit protection
                break
            except Exception as e:
                wait = 2 ** attempt  # exponential backoff: 1, 2, 4, 8, 16s
                if attempt < 4:
                    time.sleep(wait)
                    continue
                print(f"  Batch {i} failed after retries: {e}", file=sys.stderr)
                for j in range(len(batch)):
                    rows.append({"lat": lats[j], "lng": lngs[j],
                                 "p95_mm": None, "p99_mm": None, "max_mm": None, "rain_days_yr": 0})

        done = min(i + BATCH_SIZE, total)
        elapsed = time.time() - t0
        eta = elapsed / done * (total - done) if done > 0 else 0
        if done % 80 == 0 or done == total:
            print(f"  {done}/{total} ({done*100//total}%) — {elapsed:.0f}s elapsed, ~{eta:.0f}s remaining")

    df = pd.DataFrame(rows)
    out_path = "data/era5_rainfall_p95.parquet"
    df.to_parquet(out_path, index=False)
    valid = df.dropna(subset=["p95_mm"])
    print(f"\nDone: {len(valid)} valid points, saved to {out_path}")
    print(f"P95 range: {valid['p95_mm'].min():.1f} — {valid['p95_mm'].max():.1f} mm")
    print(f"Total time: {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
