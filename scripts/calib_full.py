"""全量 11k 校准 (部署前置2-3): 用全 AU SoundPLAN 立面重算按州仿射 (替 v0 每城~350)。

复用 transfer.transfer_feats (本地 Copernicus DEM/LC, 与生产推理一致)。
更新 data/noise_state_calibration.json (备份 .v0bak)。城内 5-fold CV = 真实上线表现。
NSW 验证: sydney 预测 std vs 真值 std (排序是否被压平, v0 slope 0.65 担忧)。

跑: export PROJ_LIB=...; .venv/bin/python scripts/calib_full.py
"""
import csv
import json
import math
import os
import re
import shutil
import sys
import time

import numpy as np
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error
from sklearn.model_selection import KFold

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from property_scores.noise import transfer as T  # noqa: E402
from property_scores.common.overture import get_db  # noqa: E402

CITY_STATE = {"melbourne": "VIC", "sydney": "NSW", "adelaide": "SA", "perth": "WA",
              "hobart": "TAS", "canberra": "ACT", "darwin": "NT"}
MIN_LDEN = 30.0
CACHE = "data/au_full_feat_cache.npz"
CALIB = "data/noise_state_calibration.json"


def lden(d, e, n):
    return 10 * math.log10((12 * 10 ** (d / 10) + 4 * 10 ** ((e + 5) / 10) + 8 * 10 ** ((n + 10) / 10)) / 24) if max(d, e, n) > 0 else 0.0


def load_pts():
    pts = []
    for city in CITY_STATE:
        fn = f"data/ambient_sample/antn_{city}_buildings_.csv"
        if not os.path.exists(fn):
            continue
        for r in csv.DictReader(open(fn)):
            m = re.search(r"POINT \(([-\d.]+) ([-\d.]+)\)", r["geometry"])
            if not m:
                continue
            la, lo = float(m.group(1)), float(m.group(2))  # POINT(lat lng)
            try:
                y = lden(float(r["sp_rd_max_d"]), float(r["sp_rd_max_e"]), float(r["sp_rd_max_n"]))
            except (ValueError, KeyError):
                continue
            if y >= MIN_LDEN:
                pts.append((city, la, lo, y))
    return pts


def stats(p, t):
    return (mean_absolute_error(t, p), float(np.mean(p - t)),
            (float(np.corrcoef(p, t)[0, 1]) if p.std() > 0 and t.std() > 0 else 0.0), float(p.std()))


def main():
    t0 = time.time()
    if not T._load():
        raise SystemExit("transfer model load failed")
    keys = T._FEATURE_KEYS
    pts = load_pts()
    print(f"全量点(清洗 lden>={MIN_LDEN}): {len(pts)}  各州: " +
          ", ".join(f"{c}:{sum(1 for p in pts if p[0] == c)}" for c in CITY_STATE), flush=True)

    if os.path.exists(CACHE):
        d = np.load(CACHE, allow_pickle=True)
        X, y, city, rok = d["X"], d["y"], d["city"], d["rok"]
        print(f"loaded feat cache {X.shape}", flush=True)
    else:
        db = get_db()
        X, ys, citys, roks = [], [], [], []
        for i, (c, la, lo, yv) in enumerate(pts):
            try:
                f, r = T.transfer_feats(db, la, lo)
                X.append([f[k] for k in keys]); ys.append(yv); citys.append(c); roks.append(r)
            except Exception as e:  # noqa: BLE001
                print(f"  skip {c} {la:.4f},{lo:.4f}: {str(e)[:80]}", flush=True)
            if (i + 1) % 500 == 0:
                print(f"  {i+1}/{len(pts)} ({time.time()-t0:.0f}s)", flush=True)
        X = np.array(X, float); y = np.array(ys, float); city = np.array(citys); rok = np.array(roks)
        np.savez(CACHE, X=X, y=y, city=city, rok=rok)
        print(f"features done {X.shape} ({time.time()-t0:.0f}s)", flush=True)

    print(f"raster_ok 率: {100*rok.mean():.1f}% ({int(rok.sum())}/{len(rok)})", flush=True)
    raw = T._RF.predict(X)

    # 备份 + 更新 calibration (全量按州仿射)
    shutil.copy(CALIB, CALIB + ".v0bak")
    calib = json.load(open(CALIB))
    gl = LinearRegression().fit(raw.reshape(-1, 1), y)
    calib["global_affine"] = {"slope": float(gl.coef_[0]), "intercept": float(gl.intercept_), "n": int(len(y))}
    calib["_coeff_kind"] = "full ~11k in-sample fit per state (2026-06-08); true perf = in-city 5fold CV below"
    print("\n按州仿射 (全量 in-sample):", flush=True)
    for c, state in CITY_STATE.items():
        idx = city == c
        if idx.sum() < 10:
            continue
        lin = LinearRegression().fit(raw[idx].reshape(-1, 1), y[idx])
        calib["states"][state] = {"slope": float(lin.coef_[0]), "intercept": float(lin.intercept_),
                                  "n": int(idx.sum()), "city_sample": c}
        print(f"  {state}({c}): n={int(idx.sum())} slope={lin.coef_[0]:.2f} int={lin.intercept_:+.1f}", flush=True)
    calib["states"]["QLD"] = {**calib["global_affine"], "fallback": "global (no QLD SoundPLAN sample)"}
    json.dump(calib, open(CALIB, "w"), indent=2)
    print(f"updated {CALIB} (备份 .v0bak)", flush=True)

    # 城内 5-fold CV = 真实上线表现 + NSW 验证
    print("\n=== 城内 5-fold CV (全量, 真实上线表现) ===", flush=True)
    pred = np.zeros(len(y))
    for c in CITY_STATE:
        idx = np.where(city == c)[0]
        if len(idx) < 5:
            lin = LinearRegression().fit(raw[idx].reshape(-1, 1), y[idx]); pred[idx] = lin.predict(raw[idx].reshape(-1, 1)); continue
        kf = KFold(5, shuffle=True, random_state=42)
        for tr, te in kf.split(idx):
            lin = LinearRegression().fit(raw[idx[tr]].reshape(-1, 1), y[idx[tr]])
            pred[idx[te]] = lin.predict(raw[idx[te]].reshape(-1, 1))
        m = stats(pred[idx], y[idx])
        flag = "  <- NSW(排序未压平?)" if c == "sydney" else ""
        print(f"  {c:10s} n={len(idx):5d} MAE={m[0]:.2f} bias={m[1]:+.2f} r={m[2]:.2f} predstd={m[3]:.1f} ystd={y[idx].std():.1f}{flag}", flush=True)
    m = stats(pred, y); print(f"  POOLED+syd n={len(y):5d} MAE={m[0]:.2f} r={m[2]:.2f}", flush=True)
    mask = city != "sydney"; m = stats(pred[mask], y[mask]); print(f"  POOLED-syd n={int(mask.sum()):5d} MAE={m[0]:.2f} r={m[2]:.2f}", flush=True)
    print(f"\nruntime {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
