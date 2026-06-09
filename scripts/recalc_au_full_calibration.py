"""全量重算 AU 按州校准 (用全 11k SoundPLAN 立面替 v0 每城~350)。

RF 不重训 (复用 data/noise_transfer_rf.pkl)。只更新按州仿射校准 +
跑全量城内 5-fold CV 验证 (含 NSW 重点)。

步骤:
  1. 载入全 7 城 antn_<city>_buildings_.csv (全量, geometry POINT(lat lng)),
     y=lden(sp_rd_max_d,e,n), 清洗 y>=30。
  2. 每点算 transfer_feats(75 特征) -> 缓存 data/au_full_feat_cache.npz。
  3. RF.predict -> raw。
  4. 全量按州仿射 (raw->y) -> 更新 noise_state_calibration.json (备份 .v0bak)。
  5. 城内 5-fold CV (全量): per-state + pooled(含/不含 sydney)。
  6. NSW 重点验证: 预测 std vs 真值 std (排序是否压平)。

跑前 export PROJ_LIB; cd repo 根。
"""
import csv
import json
import math
import os
import pickle
import re
import shutil
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sklearn.ensemble import RandomForestRegressor  # noqa: E402
from sklearn.linear_model import LinearRegression  # noqa: E402
from sklearn.model_selection import KFold  # noqa: E402

from property_scores.common.overture import get_db  # noqa: E402
from property_scores.noise.transfer import transfer_feats  # noqa: E402

CITY_STATE = {"melbourne": "VIC", "sydney": "NSW", "adelaide": "SA",
              "perth": "WA", "hobart": "TAS", "canberra": "ACT", "darwin": "NT"}
MIN_LDEN = 30.0
RF_PATH = "data/noise_transfer_rf.pkl"
CALIB_PATH = "data/noise_state_calibration.json"
CACHE_PATH = "data/au_full_feat_cache.npz"
POINT_RE = re.compile(r"POINT\s*\(\s*([-\d.]+)\s+([-\d.]+)\s*\)")


def lden(d, e, n):
    return (10 * math.log10((12 * 10 ** (d / 10) + 4 * 10 ** ((e + 5) / 10)
            + 8 * 10 ** ((n + 10) / 10)) / 24) if max(d, e, n) > 0 else 0.0)


def load_points():
    """全量载入 7 城: 返回 list[(city, lat, lng, y)]。geometry 是 POINT(lat lng)。"""
    pts = []
    for city in CITY_STATE:
        path = f"data/ambient_sample/antn_{city}_buildings_.csv"
        if not os.path.exists(path):
            print(f"  ⚠️ 缺 {path}, 跳过")
            continue
        n_raw = n_clean = 0
        with open(path) as fh:
            for r in csv.DictReader(fh):
                n_raw += 1
                m = POINT_RE.search(r["geometry"])
                if not m:
                    continue
                # geometry = POINT(lat lng) -> 非标准顺序
                lat = float(m.group(1))
                lng = float(m.group(2))
                try:
                    y = lden(float(r["sp_rd_max_d"]), float(r["sp_rd_max_e"]),
                             float(r["sp_rd_max_n"]))
                except (KeyError, ValueError):
                    continue
                if y >= MIN_LDEN:
                    pts.append((city, lat, lng, y))
                    n_clean += 1
        print(f"  {city:10s} raw={n_raw:5d} -> clean(y>=30)={n_clean:5d}")
    return pts


def build_cache(feature_keys):
    """每点算 transfer_feats，缓存到 npz。若已存在则复用。"""
    if os.path.exists(CACHE_PATH):
        print(f"复用缓存 {CACHE_PATH}")
        d = np.load(CACHE_PATH, allow_pickle=True)
        return (d["lat"], d["lng"], d["city"], d["y"], d["raster_ok"],
                d["X"], list(d["feature_keys"]))

    pts = load_points()
    print(f"\n全量点数 (清洗后): {len(pts)}")
    db = get_db()
    lat_a, lng_a, city_a, y_a, ok_a, X_a = [], [], [], [], [], []
    t0 = time.time()
    for i, (city, lat, lng, y) in enumerate(pts):
        f, raster_ok = transfer_feats(db, lat, lng)
        X_a.append([f[k] for k in feature_keys])
        lat_a.append(lat); lng_a.append(lng); city_a.append(city)
        y_a.append(y); ok_a.append(raster_ok)
        if (i + 1) % 200 == 0:
            el = time.time() - t0
            eta = el / (i + 1) * (len(pts) - i - 1)
            print(f"  {i+1}/{len(pts)}  ({el:.0f}s, ETA {eta/60:.1f}min)")
    db.close()
    X = np.array(X_a, dtype=float)
    np.savez_compressed(
        CACHE_PATH,
        lat=np.array(lat_a), lng=np.array(lng_a), city=np.array(city_a),
        y=np.array(y_a), raster_ok=np.array(ok_a), X=X,
        feature_keys=np.array(feature_keys, dtype=object),
    )
    print(f"saved {CACHE_PATH} ({len(pts)} 点, {time.time()-t0:.0f}s)")
    return (np.array(lat_a), np.array(lng_a), np.array(city_a),
            np.array(y_a), np.array(ok_a), X, feature_keys)


def cv_state(raw, y, k=5, seed=42, g_slope=None):
    """城内 k-fold CV: 每折在 train 上 fit 仿射, 在 test 上评估。
    返回 pred(全量, out-of-fold), mae, r。

    g_slope 给定时 = UNIFIED constrained-slope (上线方案): 斜率固定为全局值,
    每折只在 train 上拟合 intercept = mean(y - g_slope*raw)。这才反映部署表现。
    g_slope=None 时退回旧的 per-state OLS (仅作对照)。"""
    pred = np.zeros_like(y, dtype=float)
    kf = KFold(n_splits=min(k, len(y)), shuffle=True, random_state=seed)
    for tr, te in kf.split(raw.reshape(-1, 1)):
        if g_slope is None:
            lin = LinearRegression().fit(raw[tr].reshape(-1, 1), y[tr])
            pred[te] = lin.predict(raw[te].reshape(-1, 1))
        else:
            intercept = float(np.mean(y[tr] - g_slope * raw[tr]))
            pred[te] = g_slope * raw[te] + intercept
    mae = float(np.mean(np.abs(pred - y)))
    r = float(np.corrcoef(pred, y)[0, 1]) if len(y) > 1 else float("nan")
    return pred, mae, r


def main():
    with open(CALIB_PATH) as f:
        old_calib = json.load(f)
    feature_keys = list(old_calib["_feature_keys"])
    assert len(feature_keys) == 75, f"特征键数 {len(feature_keys)} != 75"

    print("=" * 60)
    print("STEP 2-3: 载入 + 算特征 (75 维)")
    print("=" * 60)
    lat, lng, city, y, raster_ok, X, fk = build_cache(feature_keys)
    assert fk == feature_keys, "缓存特征键顺序不匹配!"

    # raster miss 报告
    n_miss = int((~raster_ok).sum())
    print(f"\nSTEP 8: raster miss 点数 = {n_miss} / {len(y)} "
          f"({100*n_miss/len(y):.2f}%)")
    if n_miss:
        miss_by_city = {}
        for c, ok in zip(city, raster_ok):
            if not ok:
                miss_by_city[c] = miss_by_city.get(c, 0) + 1
        print("  miss by city:", dict(sorted(miss_by_city.items())))

    print("\n" + "=" * 60)
    print("STEP 4: RF.predict -> raw (RF 不重训)")
    print("=" * 60)
    with open(RF_PATH, "rb") as f:
        rf = pickle.load(f)
    raw = rf.predict(X)
    print(f"raw: mean={raw.mean():.1f} std={raw.std():.1f} "
          f"min={raw.min():.1f} max={raw.max():.1f}")

    # 州标签
    state = np.array([CITY_STATE[c] for c in city])

    print("\n" + "=" * 60)
    print("STEP 5: 统一约束斜率校准 (constrained-slope, 上线系数)")
    print("=" * 60)
    # UNIFIED RECALIBRATION (2026-06-08): per-state OLS overfit small noisy
    # samples (NSW n338 -> slope 0.595 / intercept 33.64 = a NOISE FLOOR, 2.2x
    # every other state's intercept; NSW/VIC/WA/NT std-ratio collapsed <0.6).
    # Fix is NOT per-state band-aids but ONE constrained scheme: pin slope =
    # GLOBAL slope (fit on all ~11k) for EVERY state, fit only the intercept
    # per state (level shift). This removes the NSW intercept-floor by
    # construction (slope can't run away), keeps std-ratio in line with global,
    # and 5fold CV pooled r is unchanged (0.685 vs prior 0.686). See
    # scripts/unified_calib_analysis.py for the full strategy sweep that
    # selected this over global/shrinkage/TheilSen/Huber.
    gl = LinearRegression().fit(raw.reshape(-1, 1), y)
    g_slope = float(gl.coef_[0])
    g_int = float(gl.intercept_)
    new_calib = {
        "_feature_keys": feature_keys,
        "_min_lden": MIN_LDEN,
        "_note": old_calib.get("_note", ""),
        "_coeff_kind": "UNIFIED constrained-slope: global slope pinned for all "
                       "states (fit on full ~11k SoundPLAN facades), per-state "
                       "intercept only. Removes per-state OLS overfit "
                       "(esp. NSW intercept-floor). True perf = in-city 5fold "
                       "CV (see _cv block).",
        "global_affine": {"slope": g_slope, "intercept": g_int, "n": int(len(y))},
        "states": {},
    }
    for city_name, st in CITY_STATE.items():
        idx = state == st
        n = int(idx.sum())
        if n < 10:
            print(f"  {st} ({city_name}): n={n} 不足, 跳过 -> global")
            continue
        # constrained slope = global; intercept = mean(y - g_slope * raw)
        intercept = float(np.mean(y[idx] - g_slope * raw[idx]))
        pred_in = g_slope * raw[idx] + intercept
        mae = float(np.mean(np.abs(pred_in - y[idx])))
        r = float(np.corrcoef(pred_in, y[idx])[0, 1])
        new_calib["states"][st] = {
            "slope": g_slope, "intercept": intercept,
            "n": n, "insample_mae": round(mae, 2), "insample_r": round(r, 2),
            "city_sample": city_name,
            "calib_kind": "constrained-slope (global slope, per-state intercept)",
        }
        print(f"  {st} ({city_name}): n={n} slope={g_slope:.3f} "
              f"int={intercept:+.1f} (in-sample MAE={mae:.2f} r={r:.2f})")
    new_calib["states"]["QLD"] = {
        **new_calib["global_affine"],
        "fallback": "global (no QLD SoundPLAN sample)",
    }
    print(f"  QLD: global 兜底 slope={gl.coef_[0]:.3f} int={gl.intercept_:+.1f}")

    print("\n" + "=" * 60)
    print("STEP 6: 城内 5-fold CV (全量, 真实上线表现 = constrained-slope)")
    print("=" * 60)
    cv = {}
    print(f"{'state':6s} {'n':>5s} {'CV_MAE':>7s} {'CV_r':>6s}")
    for st in ["VIC", "NSW", "SA", "WA", "TAS", "ACT", "NT"]:
        idx = state == st
        if idx.sum() < 10:
            continue
        _, mae, r = cv_state(raw[idx], y[idx], g_slope=g_slope)
        cv[st] = {"n": int(idx.sum()), "cv_mae": round(mae, 2), "cv_r": round(r, 3)}
        print(f"{st:6s} {int(idx.sum()):5d} {mae:7.2f} {r:6.3f}")

    # pooled CV (per-state affine within fold, then aggregate metrics)
    def pooled_cv(mask, label):
        # UNIFIED constrained-slope per fold: global slope (= g_slope refit on
        # fold train across all states) pinned, per-state intercept only.
        sub_raw = raw[mask]; sub_y = y[mask]; sub_state = state[mask]
        kf = KFold(n_splits=5, shuffle=True, random_state=42)
        pred = np.zeros_like(sub_y, dtype=float)
        idxarr = np.arange(len(sub_y))
        for tr, te in kf.split(idxarr):
            fold_slope = float(LinearRegression()
                               .fit(sub_raw[tr].reshape(-1, 1), sub_y[tr]).coef_[0])
            fold_int = float(np.mean(sub_y[tr] - fold_slope * sub_raw[tr]))
            for st in np.unique(sub_state):
                stm_tr = tr[sub_state[tr] == st]
                stm_te = te[sub_state[te] == st]
                if len(stm_te) == 0:
                    continue
                if len(stm_tr) < 2:  # fall back to fold-global intercept
                    pred[stm_te] = fold_slope * sub_raw[stm_te] + fold_int
                    continue
                it = float(np.mean(sub_y[stm_tr] - fold_slope * sub_raw[stm_tr]))
                pred[stm_te] = fold_slope * sub_raw[stm_te] + it
        mae = float(np.mean(np.abs(pred - sub_y)))
        r = float(np.corrcoef(pred, sub_y)[0, 1])
        print(f"  pooled {label}: n={len(sub_y)} CV_MAE={mae:.2f} CV_r={r:.3f}")
        return {"n": int(len(sub_y)), "cv_mae": round(mae, 2), "cv_r": round(r, 3)}

    print("pooled (per-state affine per fold):")
    cv["pooled_with_sydney"] = pooled_cv(np.ones(len(y), bool), "含 sydney")
    cv["pooled_no_sydney"] = pooled_cv(state != "NSW", "不含 sydney")

    print("\n" + "=" * 60)
    print("STEP 7: NSW 验证 (排序是否被压平)")
    print("=" * 60)
    nsw = state == "NSW"
    nsw_pred, nsw_mae, nsw_r = cv_state(raw[nsw], y[nsw], g_slope=g_slope)
    true_std = float(np.std(y[nsw]))
    pred_std = float(np.std(nsw_pred))
    nsw_slope = float(new_calib["states"]["NSW"]["slope"])
    print(f"  NSW n={int(nsw.sum())}")
    print(f"  CV r={nsw_r:.3f}  CV MAE={nsw_mae:.2f}")
    print(f"  真值 std={true_std:.2f}  CV预测 std={pred_std:.2f}  "
          f"(比值 pred/true={pred_std/true_std:.2f})")
    print(f"  constrained slope={nsw_slope:.3f} (= global; 旧 per-state OLS=0.595 "
          f"+ intercept 33.6 是过拟合噪声地板, 已消除)")
    print("  对照其他城 (CV预测std/真值std 比值):")
    for st in ["VIC", "SA", "WA", "TAS", "ACT", "NT"]:
        idx = state == st
        if idx.sum() < 10:
            continue
        p, _, _ = cv_state(raw[idx], y[idx], g_slope=g_slope)
        ts = np.std(y[idx]); ps = np.std(p)
        sl = new_calib["states"][st]["slope"]
        print(f"    {st:4s} pred/true_std={ps/ts:.2f}  slope={sl:.3f}")

    new_calib["_cv"] = cv
    new_calib["_cv_nsw_check"] = {
        "n": int(nsw.sum()), "cv_r": round(nsw_r, 3), "cv_mae": round(nsw_mae, 2),
        "true_std": round(true_std, 2), "pred_std": round(pred_std, 2),
        "pred_over_true_std": round(pred_std / true_std, 3),
        "insample_slope": round(nsw_slope, 3),
    }

    # 备份 + 写入
    bak = CALIB_PATH + ".v0bak"
    if not os.path.exists(bak):
        shutil.copy(CALIB_PATH, bak)
        print(f"\n备份旧 json -> {bak}")
    else:
        print(f"\n备份已存在 {bak} (不覆盖)")
    with open(CALIB_PATH, "w") as f:
        json.dump(new_calib, f, indent=2)
    print(f"saved {CALIB_PATH} (全量校准)")
    print("\n⚠️ 本地完成, 未部署、未重算生产缓存。RF 未重训。")

    # v0 对照
    print("\n" + "=" * 60)
    print("v0 (每城~350) -> v1 (全量) 系数对照")
    print("=" * 60)
    print(f"{'state':6s} {'v0_n':>6s} {'v1_n':>6s} {'v0_slope':>9s} {'v1_slope':>9s} "
          f"{'v0_int':>8s} {'v1_int':>8s}")
    for st in ["VIC", "NSW", "SA", "WA", "TAS", "ACT", "NT"]:
        o = old_calib["states"].get(st, {})
        nw = new_calib["states"].get(st, {})
        if not nw:
            continue
        print(f"{st:6s} {o.get('n','-'):>6} {nw['n']:>6} "
              f"{o.get('slope',0):>9.3f} {nw['slope']:>9.3f} "
              f"{o.get('intercept',0):>+8.1f} {nw['intercept']:>+8.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
