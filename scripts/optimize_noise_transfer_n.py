"""Task 1: n_estimators sweep for the EU->AU transfer RF, with in-city 5-fold CV.

Retrains the transfer RF on the cached NL+UK training set (data/eu/transfer5_cache.npz
Xnl/ynl) at n_estimators in {500, 400, 300} (all other params fixed to the shipped
model: min_samples_leaf=3, max_features=sqrt, random_state=42). For each model:
  * predict on the cached full AU SoundPLAN facades (data/au_full_feat_cache.npz X)
  * per-state affine + in-city 5-fold CV (same logic as recalc_au_full_calibration.py)
  * pooled CV with / without Sydney
  * single-point predict latency (averaged over many reps)
  * pkl disk size (plain pickle) + estimated RAM (tree node count proxy)

Pure cache + sklearn, no DuckDB / raster. Prints a comparison table + decision.

Usage (from repo root):
  .venv/bin/python scripts/optimize_noise_transfer_n.py
"""
import os
import pickle
import sys
import tempfile
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sklearn.ensemble import RandomForestRegressor  # noqa: E402
from sklearn.linear_model import LinearRegression  # noqa: E402
from sklearn.model_selection import KFold  # noqa: E402

EU_CACHE = "data/eu/transfer5_cache.npz"
AU_CACHE = "data/au_full_feat_cache.npz"
CURRENT_RF = "data/noise_transfer_rf.pkl"

CITY_STATE = {"melbourne": "VIC", "sydney": "NSW", "adelaide": "SA",
              "perth": "WA", "hobart": "TAS", "canberra": "ACT", "darwin": "NT"}

N_VALUES = [500, 400, 300]
RF_KWARGS = dict(min_samples_leaf=3, max_features="sqrt", random_state=42, n_jobs=-1)


def cv_state(raw, y, k=5, seed=42):
    """In-city k-fold CV: fit affine on train fold, eval on test fold."""
    raw = raw.reshape(-1, 1)
    pred = np.zeros_like(y, dtype=float)
    kf = KFold(n_splits=min(k, len(y)), shuffle=True, random_state=seed)
    for tr, te in kf.split(raw):
        lin = LinearRegression().fit(raw[tr], y[tr])
        pred[te] = lin.predict(raw[te])
    mae = float(np.mean(np.abs(pred - y)))
    r = float(np.corrcoef(pred, y)[0, 1]) if len(y) > 1 else float("nan")
    return pred, mae, r


def pooled_cv(raw, y, state, mask):
    """Per-state affine within each fold, then aggregate metrics over the mask."""
    sub_raw = raw[mask]
    sub_y = y[mask]
    sub_state = state[mask]
    kf = KFold(n_splits=5, shuffle=True, random_state=42)
    pred = np.zeros_like(sub_y, dtype=float)
    idxarr = np.arange(len(sub_y))
    for tr, te in kf.split(idxarr):
        for st in np.unique(sub_state):
            stm_tr = tr[sub_state[tr] == st]
            stm_te = te[sub_state[te] == st]
            if len(stm_tr) < 2 or len(stm_te) == 0:
                if len(stm_te):
                    lin = LinearRegression().fit(
                        sub_raw[tr].reshape(-1, 1), sub_y[tr])
                    pred[stm_te] = lin.predict(sub_raw[stm_te].reshape(-1, 1))
                continue
            lin = LinearRegression().fit(
                sub_raw[stm_tr].reshape(-1, 1), sub_y[stm_tr])
            pred[stm_te] = lin.predict(sub_raw[stm_te].reshape(-1, 1))
    mae = float(np.mean(np.abs(pred - sub_y)))
    r = float(np.corrcoef(pred, sub_y)[0, 1])
    return mae, r


def time_predict(rf, X, n_reps=300):
    """Single-point predict latency in ms, averaged. Warm up first."""
    x1 = X[:1]
    for _ in range(20):  # warmup
        rf.predict(x1)
    # cycle through different points to avoid any caching artifacts
    times = []
    n = len(X)
    for i in range(n_reps):
        xi = X[i % n:i % n + 1]
        t0 = time.perf_counter()
        rf.predict(xi)
        times.append((time.perf_counter() - t0) * 1000)
    times.sort()
    # trim 10% tails for a stable central estimate, also report median
    trimmed = times[int(0.05 * len(times)):int(0.95 * len(times))]
    return float(np.mean(trimmed)), float(np.median(times))


def main():
    print("=" * 72)
    print("Loading caches")
    print("=" * 72)
    eu = np.load(EU_CACHE, allow_pickle=True)
    Xnl, ynl = eu["Xnl"], eu["ynl"]
    print(f"  EU train: Xnl={Xnl.shape} ynl={ynl.shape}")

    au = np.load(AU_CACHE, allow_pickle=True)
    Xau, yau, city = au["X"], au["y"], au["city"]
    raster_ok = au["raster_ok"]
    state = np.array([CITY_STATE[c] for c in city])
    print(f"  AU eval:  X={Xau.shape} y={yau.shape}  "
          f"(raster_ok={int(raster_ok.sum())}/{len(raster_ok)})")
    # recalc uses all cached AU points (cache already filtered y>=30 at build).
    print(f"  states: {dict(zip(*np.unique(state, return_counts=True)))}")

    # Reference: predict latency / size of the SHIPPED model for sanity.
    results = []
    for n in N_VALUES:
        print("\n" + "=" * 72)
        print(f"n_estimators = {n}")
        print("=" * 72)
        t0 = time.time()
        rf = RandomForestRegressor(n_estimators=n, **RF_KWARGS)
        rf.fit(Xnl, ynl)
        train_s = time.time() - t0
        print(f"  train: {train_s:.1f}s")

        raw = rf.predict(Xau)
        print(f"  raw AU: mean={raw.mean():.1f} std={raw.std():.1f} "
              f"min={raw.min():.1f} max={raw.max():.1f}")

        # per-state in-city CV
        per_state = {}
        for st in ["VIC", "NSW", "SA", "WA", "TAS", "ACT", "NT"]:
            idx = state == st
            if idx.sum() < 10:
                continue
            _, mae, r = cv_state(raw[idx], yau[idx])
            per_state[st] = (int(idx.sum()), mae, r)

        mae_w, r_w = pooled_cv(raw, yau, state, np.ones(len(yau), bool))
        mae_n, r_n = pooled_cv(raw, yau, state, state != "NSW")

        # latency (n_jobs=1 for realistic single-request serving)
        rf_serve = rf
        rf_serve.n_jobs = 1
        mean_ms, med_ms = time_predict(rf_serve, Xau)

        # plain-pickle disk size + RAM proxy
        with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as tf:
            pickle.dump(rf, tf)
            tmp = tf.name
        disk_mb = os.path.getsize(tmp) / 1e6
        os.unlink(tmp)
        nodes = sum(est.tree_.node_count for est in rf.estimators_)
        # RAM scales ~linearly with node count; anchor to shipped 500=~450MB.
        ram_mb = nodes / 1e6 * (450.0 / 2.642846)  # 500-tree had 2.642846M nodes -> 450MB

        results.append({
            "n": n, "train_s": train_s, "raw_std": raw.std(),
            "per_state": per_state,
            "mae_w": mae_w, "r_w": r_w, "mae_n": mae_n, "r_n": r_n,
            "mean_ms": mean_ms, "med_ms": med_ms,
            "disk_mb": disk_mb, "ram_mb": ram_mb, "nodes": nodes,
        })
        print(f"  pooled WITH syd:  CV_MAE={mae_w:.3f}  CV_r={r_w:.4f}")
        print(f"  pooled NO   syd:  CV_MAE={mae_n:.3f}  CV_r={r_n:.4f}")
        print(f"  predict: trimmed-mean={mean_ms:.2f}ms  median={med_ms:.2f}ms")
        print(f"  disk(plain pkl)={disk_mb:.1f}MB  nodes={nodes/1e6:.3f}M  "
              f"est.RAM~{ram_mb:.0f}MB")

    # ---- Comparison table ----
    print("\n" + "=" * 72)
    print("COMPARISON TABLE")
    print("=" * 72)
    base = next(x for x in results if x["n"] == 500)
    hdr = (f"{'n':>4} | {'CV_r(+syd)':>11} {'dr':>7} | {'CV_r(-syd)':>11} {'dr':>7} | "
           f"{'MAE+syd':>8} {'MAE-syd':>8} | {'pred_ms':>8} | {'disk_MB':>8} | {'RAM_MB':>7}")
    print(hdr)
    print("-" * len(hdr))
    for x in results:
        dr_w = x["r_w"] - base["r_w"]
        dr_n = x["r_n"] - base["r_n"]
        print(f"{x['n']:>4} | {x['r_w']:>11.4f} {dr_w:>+7.4f} | "
              f"{x['r_n']:>11.4f} {dr_n:>+7.4f} | "
              f"{x['mae_w']:>8.3f} {x['mae_n']:>8.3f} | "
              f"{x['mean_ms']:>7.2f}m | {x['disk_mb']:>7.1f}M | {x['ram_mb']:>6.0f}M")

    print("\nPer-state CV_r:")
    states_order = ["VIC", "NSW", "SA", "WA", "TAS", "ACT", "NT"]
    print(f"{'n':>4} | " + " ".join(f"{s:>8}" for s in states_order))
    for x in results:
        cells = []
        for s in states_order:
            if s in x["per_state"]:
                cells.append(f"{x['per_state'][s][2]:>8.3f}")
            else:
                cells.append(f"{'-':>8}")
        print(f"{x['n']:>4} | " + " ".join(cells))

    # ---- Decision: smallest n where CV_r doesn't drop (>= base-0.01) AND
    #      MAE doesn't rise (<= base+0.01 tolerance for float noise). ----
    print("\n" + "=" * 72)
    print("DECISION")
    print("=" * 72)
    R_TOL = 0.01     # CV r must stay within 0.01 of 500 baseline
    MAE_TOL = 0.01   # MAE must not rise more than 0.01
    chosen = 500
    # check from smallest to largest; pick smallest passing both pooled metrics
    for x in sorted(results, key=lambda d: d["n"]):
        ok_r = (x["r_w"] >= base["r_w"] - R_TOL) and (x["r_n"] >= base["r_n"] - R_TOL)
        ok_mae = (x["mae_w"] <= base["mae_w"] + MAE_TOL) and (x["mae_n"] <= base["mae_n"] + MAE_TOL)
        verdict = "PASS" if (ok_r and ok_mae) else "FAIL"
        print(f"  n={x['n']}: r(+syd)Δ={x['r_w']-base['r_w']:+.4f} "
              f"r(-syd)Δ={x['r_n']-base['r_n']:+.4f} "
              f"MAE(+)Δ={x['mae_w']-base['mae_w']:+.3f} "
              f"MAE(-)Δ={x['mae_n']-base['mae_n']:+.3f} -> {verdict}")
        if ok_r and ok_mae and x["n"] < chosen:
            chosen = x["n"]
    print(f"\n>>> CHOSEN n_estimators = {chosen} "
          f"(accuracy-first: smallest n with CV_r within {R_TOL} and MAE within {MAE_TOL})")
    return chosen


if __name__ == "__main__":
    main()
