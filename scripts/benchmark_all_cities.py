"""Benchmark noise model against Ambient Maps data for all 7 cities."""

import csv
import math
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from property_scores.noise.score import noise_score


CITIES = ["melbourne", "sydney", "perth", "adelaide", "hobart", "darwin", "canberra"]
SAMPLE_DIR = "data/ambient_sample"


def load_city(city: str) -> list[dict]:
    path = os.path.join(SAMPLE_DIR, f"antn_{city}_buildings_.csv")
    buildings = []
    with open(path) as f:
        for row in csv.DictReader(f):
            geom = row["geometry"].replace("POINT (", "").replace(")", "").split()
            lat, lng = float(geom[0]), float(geom[1])
            sf = lambda v: float(v) if v else 0
            rd_d = sf(row["sp_rd_max_d"])
            rd_e = sf(row.get("sp_rd_max_e", ""))
            rd_n = sf(row.get("sp_rd_max_n", ""))
            if rd_d > 0:
                lden = 10 * math.log10(
                    (12 * 10 ** (rd_d / 10)
                     + 4 * 10 ** (((rd_e or rd_d - 5) + 5) / 10)
                     + 8 * 10 ** (((rd_n or rd_d - 10) + 10) / 10)) / 24
                )
            else:
                lden = 0
            if lden > 20:
                buildings.append({"lat": lat, "lng": lng, "amb_lden": lden, "rd_d": rd_d})
    return buildings


def benchmark_city(city: str, buildings: list[dict], max_n: int = 0) -> dict:
    if max_n > 0:
        buildings = buildings[:max_n]

    errors_omni = []
    errors_fmax = []
    t0 = time.time()
    failed = 0

    for i, b in enumerate(buildings):
        try:
            o = noise_score(b["lat"], b["lng"], 500)
            d_omni = o["lden_db"] - b["amb_lden"]
            our_max_f = o.get("lden_max_facade", o["lden_db"])
            d_fmax = our_max_f - b["amb_lden"]
            errors_omni.append(d_omni)
            errors_fmax.append(d_fmax)
        except Exception:
            failed += 1

        if (i + 1) % 100 == 0:
            elapsed = time.time() - t0
            print(f"    {city}: {i+1}/{len(buildings)} ({elapsed:.0f}s)")

    elapsed = time.time() - t0
    n = len(errors_omni)
    if n == 0:
        return {"city": city, "n": 0, "failed": failed}

    eo = np.array(errors_omni)
    ef = np.array(errors_fmax)

    return {
        "city": city,
        "n": n,
        "failed": failed,
        "time_s": elapsed,
        "omni_bias": float(np.mean(eo)),
        "omni_mae": float(np.mean(np.abs(eo))),
        "omni_w5": float(np.mean(np.abs(eo) <= 5) * 100),
        "fmax_bias": float(np.mean(ef)),
        "fmax_mae": float(np.mean(np.abs(ef))),
        "fmax_w5": float(np.mean(np.abs(ef) <= 5) * 100),
    }


def main():
    max_per_city = int(sys.argv[1]) if len(sys.argv) > 1 else 200

    print("=" * 70)
    print(f"Multi-City Benchmark (max {max_per_city} buildings/city)")
    print("=" * 70)

    all_results = []
    all_omni = []
    all_fmax = []

    for city in CITIES:
        buildings = load_city(city)
        print(f"\n{city.upper()}: {len(buildings)} buildings (sampling {min(len(buildings), max_per_city)})")

        # Sample evenly across noise range
        buildings.sort(key=lambda b: b["amb_lden"])
        if len(buildings) > max_per_city:
            step = len(buildings) / max_per_city
            buildings = [buildings[int(i * step)] for i in range(max_per_city)]

        result = benchmark_city(city, buildings, max_per_city)
        all_results.append(result)

        if result["n"] > 0:
            print(f"    Omni:  Bias={result['omni_bias']:+.1f}  MAE={result['omni_mae']:.1f}  W5={result['omni_w5']:.0f}%")
            print(f"    Facade: Bias={result['fmax_bias']:+.1f}  MAE={result['fmax_mae']:.1f}  W5={result['fmax_w5']:.0f}%")
            if result["failed"]:
                print(f"    Failed: {result['failed']}")

    # Summary table
    print(f"\n{'=' * 70}")
    print(f"{'City':12s} {'N':>5s} {'Omni Bias':>10s} {'Omni MAE':>9s} {'Omni W5':>8s} {'Fac Bias':>9s} {'Fac MAE':>8s} {'Fac W5':>7s}")
    print("-" * 70)
    total_n = 0
    for r in all_results:
        if r["n"] > 0:
            print(f"{r['city']:12s} {r['n']:5d} {r['omni_bias']:+10.1f} {r['omni_mae']:9.1f} {r['omni_w5']:7.0f}% {r['fmax_bias']:+9.1f} {r['fmax_mae']:8.1f} {r['fmax_w5']:6.0f}%")
            total_n += r["n"]

    # Aggregate
    all_o = []
    all_f = []
    for r in all_results:
        if r["n"] > 0:
            all_o.extend([r["omni_bias"]] * r["n"])  # weighted
            all_f.extend([r["fmax_bias"]] * r["n"])

    print("-" * 70)
    if total_n > 0:
        # Re-compute from per-city weighted
        wt_omni_bias = sum(r["omni_bias"] * r["n"] for r in all_results if r["n"] > 0) / total_n
        wt_omni_mae = sum(r["omni_mae"] * r["n"] for r in all_results if r["n"] > 0) / total_n
        wt_omni_w5 = sum(r["omni_w5"] * r["n"] for r in all_results if r["n"] > 0) / total_n
        wt_fmax_bias = sum(r["fmax_bias"] * r["n"] for r in all_results if r["n"] > 0) / total_n
        wt_fmax_mae = sum(r["fmax_mae"] * r["n"] for r in all_results if r["n"] > 0) / total_n
        wt_fmax_w5 = sum(r["fmax_w5"] * r["n"] for r in all_results if r["n"] > 0) / total_n
        print(f"{'WEIGHTED':12s} {total_n:5d} {wt_omni_bias:+10.1f} {wt_omni_mae:9.1f} {wt_omni_w5:7.0f}% {wt_fmax_bias:+9.1f} {wt_fmax_mae:8.1f} {wt_fmax_w5:6.0f}%")


if __name__ == "__main__":
    main()
