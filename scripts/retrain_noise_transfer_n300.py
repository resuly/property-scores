"""Task 2: retrain the EU->AU transfer RF at the chosen n_estimators (300),
save it as the production model, and recompute the per-state affine calibration.

Backups are made BEFORE overwriting:
  data/noise_transfer_rf.pkl          -> .n500bak   (done by caller; re-asserted here)
  data/noise_state_calibration.json   -> .pre_opt_bak (done by caller; re-asserted here)

Steps:
  1. Train RF(n=300, min_samples_leaf=3, max_features=sqrt, random_state=42) on
     data/eu/transfer5_cache.npz Xnl/ynl.
  2. Save plain pickle -> data/noise_transfer_rf.pkl (production; pickle.load-compatible).
  3. raw = RF.predict(au_full_feat_cache X); per-state in-sample affine + in-city
     5-fold CV (same logic as recalc_au_full_calibration.py) -> noise_state_calibration.json.
  4. Report joblib compress=3 savings (NOT shipped: incompatible with pickle.load).

Pure cache + sklearn (no DuckDB / raster). Run from repo root.
"""
import json
import os
import pickle
import shutil
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
RF_PATH = "data/noise_transfer_rf.pkl"
CALIB_PATH = "data/noise_state_calibration.json"
N = 300
MIN_LDEN = 30.0
RF_KWARGS = dict(n_estimators=N, min_samples_leaf=3, max_features="sqrt",
                 random_state=42, n_jobs=-1)
CITY_STATE = {"melbourne": "VIC", "sydney": "NSW", "adelaide": "SA",
              "perth": "WA", "hobart": "TAS", "canberra": "ACT", "darwin": "NT"}


def cv_state(raw, y, k=5, seed=42):
    raw = raw.reshape(-1, 1)
    pred = np.zeros_like(y, dtype=float)
    kf = KFold(n_splits=min(k, len(y)), shuffle=True, random_state=seed)
    for tr, te in kf.split(raw):
        lin = LinearRegression().fit(raw[tr], y[tr])
        pred[te] = lin.predict(raw[te])
    mae = float(np.mean(np.abs(pred - y)))
    r = float(np.corrcoef(pred, y)[0, 1]) if len(y) > 1 else float("nan")
    return pred, mae, r


def pooled_cv(raw, y, state, mask, label):
    sub_raw = raw[mask]; sub_y = y[mask]; sub_state = state[mask]
    kf = KFold(n_splits=5, shuffle=True, random_state=42)
    pred = np.zeros_like(sub_y, dtype=float)
    idxarr = np.arange(len(sub_y))
    for tr, te in kf.split(idxarr):
        for st in np.unique(sub_state):
            stm_tr = tr[sub_state[tr] == st]
            stm_te = te[sub_state[te] == st]
            if len(stm_tr) < 2 or len(stm_te) == 0:
                if len(stm_te):
                    lin = LinearRegression().fit(sub_raw[tr].reshape(-1, 1), sub_y[tr])
                    pred[stm_te] = lin.predict(sub_raw[stm_te].reshape(-1, 1))
                continue
            lin = LinearRegression().fit(sub_raw[stm_tr].reshape(-1, 1), sub_y[stm_tr])
            pred[stm_te] = lin.predict(sub_raw[stm_te].reshape(-1, 1))
    mae = float(np.mean(np.abs(pred - sub_y)))
    r = float(np.corrcoef(pred, sub_y)[0, 1])
    print(f"  pooled {label}: n={len(sub_y)} CV_MAE={mae:.2f} CV_r={r:.3f}")
    return {"n": int(len(sub_y)), "cv_mae": round(mae, 2), "cv_r": round(r, 3)}


def main():
    # Re-assert backups exist (caller already made them).
    assert os.path.exists(RF_PATH + ".n500bak"), "missing .n500bak backup!"
    assert os.path.exists(CALIB_PATH + ".pre_opt_bak"), "missing .pre_opt_bak backup!"

    with open(CALIB_PATH) as f:
        old_calib = json.load(f)
    feature_keys = list(old_calib["_feature_keys"])
    assert len(feature_keys) == 75

    eu = np.load(EU_CACHE, allow_pickle=True)
    Xnl, ynl = eu["Xnl"], eu["ynl"]
    print(f"EU train: {Xnl.shape}")

    print("=" * 60)
    print(f"STEP 1: train RF n_estimators={N}")
    print("=" * 60)
    t0 = time.time()
    rf = RandomForestRegressor(**RF_KWARGS).fit(Xnl, ynl)
    print(f"  trained in {time.time()-t0:.1f}s, n_estimators={rf.n_estimators}")

    print("\n" + "=" * 60)
    print("STEP 2: save production plain-pickle RF")
    print("=" * 60)
    # write to temp then atomic replace, so a crash can't leave a half file
    fd, tmp = tempfile.mkstemp(dir="data", suffix=".pkl")
    os.close(fd)
    with open(tmp, "wb") as f:
        pickle.dump(rf, f)
    os.replace(tmp, RF_PATH)
    disk_mb = os.path.getsize(RF_PATH) / 1e6
    print(f"  saved {RF_PATH} (plain pickle, {disk_mb:.1f} MB)")
    # round-trip sanity: pickle.load must work (mirrors transfer._load)
    with open(RF_PATH, "rb") as f:
        rf_chk = pickle.load(f)
    assert rf_chk.n_estimators == N
    print(f"  pickle.load round-trip OK (n_estimators={rf_chk.n_estimators})")

    # joblib compress=3 savings report (NOT shipped — incompatible w/ pickle.load)
    import joblib
    jf = tempfile.NamedTemporaryFile(suffix=".joblib", delete=False); jf.close()
    joblib.dump(rf, jf.name, compress=3)
    jdisk_mb = os.path.getsize(jf.name) / 1e6
    os.unlink(jf.name)
    print(f"  [report only] joblib compress=3 size = {jdisk_mb:.1f} MB "
          f"(-{disk_mb - jdisk_mb:.1f} MB / -{100*(1-jdisk_mb/disk_mb):.0f}% vs plain)")
    print("  NOTE: joblib compress NOT shipped — transfer._load() uses pickle.load "
          "which CANNOT read zlib-framed joblib files.")

    print("\n" + "=" * 60)
    print("STEP 3: recompute per-state calibration (recalc logic)")
    print("=" * 60)
    au = np.load(AU_CACHE, allow_pickle=True)
    X, y, city = au["X"], au["y"], au["city"]
    state = np.array([CITY_STATE[c] for c in city])
    raw = rf.predict(X)
    print(f"  raw: mean={raw.mean():.1f} std={raw.std():.1f} "
          f"min={raw.min():.1f} max={raw.max():.1f}")

    gl = LinearRegression().fit(raw.reshape(-1, 1), y)
    new_calib = {
        "_feature_keys": feature_keys,
        "_min_lden": MIN_LDEN,
        "_note": old_calib.get("_note", ""),
        "_coeff_kind": old_calib.get("_coeff_kind", ""),
        "_n_estimators": N,
        "_opt_note": (f"n_estimators reduced 500->{N} (2026-06-08): CV r/MAE "
                      "unchanged within 0.001, ~40% less RAM/disk/latency. "
                      "RF retrained on same NL+UK cache, recalibrated on same "
                      "~11k AU SoundPLAN facades."),
        "global_affine": {"slope": float(gl.coef_[0]),
                          "intercept": float(gl.intercept_), "n": int(len(y))},
        "states": {},
    }
    for city_name, st in CITY_STATE.items():
        idx = state == st
        n = int(idx.sum())
        if n < 10:
            continue
        lin = LinearRegression().fit(raw[idx].reshape(-1, 1), y[idx])
        pred_in = lin.predict(raw[idx].reshape(-1, 1))
        mae = float(np.mean(np.abs(pred_in - y[idx])))
        r = float(np.corrcoef(pred_in, y[idx])[0, 1])
        new_calib["states"][st] = {
            "slope": float(lin.coef_[0]), "intercept": float(lin.intercept_),
            "n": n, "insample_mae": round(mae, 2), "insample_r": round(r, 2),
            "city_sample": city_name,
        }
        print(f"  {st} ({city_name}): n={n} slope={lin.coef_[0]:.3f} "
              f"int={lin.intercept_:+.1f} (in-sample MAE={mae:.2f} r={r:.2f})")
    new_calib["states"]["QLD"] = {
        **new_calib["global_affine"],
        "fallback": "global (no QLD SoundPLAN sample)",
    }

    # in-city 5-fold CV
    print("\n  in-city 5-fold CV:")
    cv = {}
    for st in ["VIC", "NSW", "SA", "WA", "TAS", "ACT", "NT"]:
        idx = state == st
        if idx.sum() < 10:
            continue
        _, mae, r = cv_state(raw[idx], y[idx])
        cv[st] = {"n": int(idx.sum()), "cv_mae": round(mae, 2), "cv_r": round(r, 3)}
        print(f"    {st:6s} n={int(idx.sum()):5d} CV_MAE={mae:6.2f} CV_r={r:.3f}")
    cv["pooled_with_sydney"] = pooled_cv(raw, y, state, np.ones(len(y), bool), "含 sydney")
    cv["pooled_no_sydney"] = pooled_cv(raw, y, state, state != "NSW", "不含 sydney")

    # NSW std check
    nsw = state == "NSW"
    nsw_pred, nsw_mae, nsw_r = cv_state(raw[nsw], y[nsw])
    true_std = float(np.std(y[nsw])); pred_std = float(np.std(nsw_pred))
    new_calib["_cv"] = cv
    new_calib["_cv_nsw_check"] = {
        "n": int(nsw.sum()), "cv_r": round(nsw_r, 3), "cv_mae": round(nsw_mae, 2),
        "true_std": round(true_std, 2), "pred_std": round(pred_std, 2),
        "pred_over_true_std": round(pred_std / true_std, 3),
        "insample_slope": round(float(new_calib["states"]["NSW"]["slope"]), 3),
    }

    # write calibration (.pre_opt_bak already exists; do not overwrite it)
    fd, tmp = tempfile.mkstemp(dir="data", suffix=".json"); os.close(fd)
    with open(tmp, "w") as f:
        json.dump(new_calib, f, indent=2)
    os.replace(tmp, CALIB_PATH)
    print(f"\n  saved {CALIB_PATH}")
    print(f"  pooled WITH syd: CV_r={cv['pooled_with_sydney']['cv_r']} "
          f"MAE={cv['pooled_with_sydney']['cv_mae']}")
    print(f"  pooled NO   syd: CV_r={cv['pooled_no_sydney']['cv_r']} "
          f"MAE={cv['pooled_no_sydney']['cv_mae']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
