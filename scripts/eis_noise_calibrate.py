"""Calibrate the noise model against the MEASURED corpus (not SoundPLAN).

Step 1 of the measured re-anchor:
  1. compute per-point residuals (geocode + noise_score + measured Lden), cached
     to data/eis_noise/residuals.csv so re-runs are instant.
  2. diagnose residual structure: constant SHIFT vs level-dependent SLOPE
     (flattening = over-reads quiet more than loud).
  3. fit + 5-fold cross-validate candidate corrections and report MAE:
       none | global shift | global linear (a*model+b) | per-state shift.

The measured set is road-corridor-clustered (loggers sit near roads), so a
correction that GENERALISES (global, level-aware) is safer than per-state shifts
fit on small n. Decide the form from the CV numbers, do NOT blindly subtract the
mean (it bakes in the metric/facade confounds + the loud-cluster bias).
"""
import csv
import os
import sys
import math
import json

from eis_noise_compare import geocode, meas_lden  # reuse pipeline
from property_scores.noise.score import noise_score

RESID = "data/eis_noise/residuals.csv"


def compute_residuals(corpus, out=RESID):
    gc_cache = {}
    cache_path = "data/eis_noise/_geocode_cache.json"
    if os.path.exists(cache_path):
        gc_cache = json.load(open(cache_path))
    rows = []
    for r in csv.DictReader(open(corpus)):
        key = f"{r['state']}|{r['address']}"
        if key in gc_cache:
            lat, lng = gc_cache[key]
        else:
            lat, lng = geocode(r["address"], r["state"])
            gc_cache[key] = [lat, lng]
        if lat is None:
            continue
        ns = noise_score(lat, lng)
        mod = ns.get("lden_db")
        if mod is None:
            continue
        ml = meas_lden(float(r["meas_day"]), float(r["meas_night"]), r.get("metric", "LAeq"))
        rows.append({"state": r["state"], "address": r["address"],
                     "measured_lden": round(ml, 2), "model_lden": round(mod, 2),
                     "road_db": ns.get("road_db"), "metric": r.get("metric", "LAeq"),
                     "residual": round(mod - ml, 2)})
    json.dump(gc_cache, open(cache_path, "w"))
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return rows


def mae(rows, correct):
    return sum(abs(correct(r) - r["measured_lden"]) for r in rows) / len(rows)


def fit_linear(rows):
    """least-squares model->measured: measured ≈ a*model + b."""
    n = len(rows)
    sx = sum(r["model_lden"] for r in rows)
    sy = sum(r["measured_lden"] for r in rows)
    sxx = sum(r["model_lden"] ** 2 for r in rows)
    sxy = sum(r["model_lden"] * r["measured_lden"] for r in rows)
    a = (n * sxy - sx * sy) / (n * sxx - sx * sx)
    b = (sy - a * sx) / n
    return a, b


def kfold_mae(rows, fit_fn, apply_fn, k=5):
    import random
    rnd = random.Random(42)
    idx = list(range(len(rows)))
    rnd.shuffle(idx)
    folds = [idx[i::k] for i in range(k)]
    errs = []
    for f in folds:
        te = set(f)
        train = [rows[i] for i in idx if i not in te]
        test = [rows[i] for i in f]
        params = fit_fn(train)
        errs += [abs(apply_fn(r, params) - r["measured_lden"]) for r in test]
    return sum(errs) / len(errs)


def main():
    corpus = sys.argv[1] if len(sys.argv) > 1 else "data/eis_noise/measured_corpus_v2.csv"
    rows = compute_residuals(corpus)
    print(f"n={len(rows)}")

    # 1) residual structure: shift vs slope
    import statistics
    res = [r["residual"] for r in rows]
    lvl = [r["measured_lden"] for r in rows]
    mean_r = statistics.mean(res)
    # corr(residual, measured level)
    mr, ml_ = statistics.mean(res), statistics.mean(lvl)
    cov = sum((a - mr) * (b - ml_) for a, b in zip(res, lvl)) / len(res)
    sd = statistics.pstdev(res) * statistics.pstdev(lvl)
    corr = cov / sd if sd else 0
    print(f"\n[structure] mean residual {mean_r:+.2f}  corr(residual, measured_level) {corr:+.2f}")
    print("  residual by measured-Lden bin:")
    for lo, hi in [(40, 55), (55, 62), (62, 68), (68, 74), (74, 100)]:
        bin_r = [r["residual"] for r in rows if lo <= r["measured_lden"] < hi]
        if bin_r:
            print(f"    {lo}-{hi} dB  n={len(bin_r):>3}  mean residual {statistics.mean(bin_r):+.2f}")

    # 2) candidate corrections, 5-fold CV MAE
    print("\n[corrections] 5-fold CV MAE (lower = better):")
    print(f"  none           {mae(rows, lambda r: r['model_lden']):.2f}")
    print(f"  global shift   {kfold_mae(rows, lambda tr: statistics.mean(x['residual'] for x in tr), lambda r,b: r['model_lden']-b):.2f}")
    print(f"  global linear  {kfold_mae(rows, fit_linear, lambda r,p: p[0]*r['model_lden']+p[1]):.2f}")
    # per-state shift
    def fit_state(tr):
        from collections import defaultdict
        d = defaultdict(list)
        for x in tr:
            d[x["state"]].append(x["residual"])
        return {s: statistics.mean(v) for s, v in d.items()}, statistics.mean(x["residual"] for x in tr)
    def apply_state(r, p):
        shifts, glob = p
        return r["model_lden"] - shifts.get(r["state"], glob)
    print(f"  per-state shift{kfold_mae(rows, fit_state, apply_state):.2f}")

    # full-fit linear params (for reference)
    a, b = fit_linear(rows)
    print(f"\n[full-fit global linear] measured ≈ {a:.3f}*model + {b:.2f}  (a<1 ⇒ model over-spreads/flattening)")


if __name__ == "__main__":
    main()
