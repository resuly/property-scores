"""固化噪声迁移模型 (上线产物, 不部署 — 待 Bo sign-off)。

架构: EU(NL+UK) 几何特征 RF -> 按州仿射校准(该州 SoundPLAN 样本 raw->Lden)。
两条加点状交通量的路(Mapbox 速度/拥堵, 实测 AADT)均判负 (几何特征已吃干净
交通量信息), 故上线模型 = 纯几何迁移 RF + 按州校准, 无需交通特征。

产物:
  data/noise_transfer_rf.pkl        — EU 迁移 RF
  data/noise_state_calibration.json — 按州仿射系数 + 特征键顺序 + QLD 全局兜底

注: 校准系数是 in-sample fit (全部该州样本) 用于上线; 真实表现看城内 5-fold CV
(calib_eval.py: pooled-syd r 0.70/MAE 3.8)。当前用 cache 每城~350 清洗样本 = v0,
全量 11k 重算特征后可提稳。

跑: .venv/bin/python scripts/build_noise_model.py
"""
import json
import os
import pickle
import sys

import numpy as np
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import LinearRegression

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
from poc_eu_transfer5 import fkeys  # noqa: E402

CITY_STATE = {"melbourne": "VIC", "sydney": "NSW", "adelaide": "SA",
              "perth": "WA", "hobart": "TAS", "canberra": "ACT", "darwin": "NT"}
MIN_LDEN = 30.0  # 清洗 SoundPLAN 哨兵占位符 (d=e=n=1 -> lden~7, Sydney 16%)


def main():
    d = np.load("data/eu/transfer5_cache.npz", allow_pickle=True)
    Xnl, ynl = d["Xnl"], d["ynl"]
    Xau, yau, cau = d["Xau"], d["yau"], np.array(d["cau"])
    keep = yau >= MIN_LDEN
    print(f"清洗哨兵: 去 {(~keep).sum()} 点 (lden<{MIN_LDEN}), 剩 {keep.sum()}")
    Xau, yau, cau = Xau[keep], yau[keep], cau[keep]

    rf = RandomForestRegressor(n_estimators=500, min_samples_leaf=3,
                               max_features="sqrt", n_jobs=-1, random_state=42)
    rf.fit(Xnl, ynl)
    with open("data/noise_transfer_rf.pkl", "wb") as f:
        pickle.dump(rf, f)
    print(f"saved data/noise_transfer_rf.pkl (trained on {len(ynl)} EU points, {Xnl.shape[1]} feats)")

    raw = rf.predict(Xau)
    gl = LinearRegression().fit(raw.reshape(-1, 1), yau)
    calib = {
        "_feature_keys": fkeys(),
        "_min_lden": MIN_LDEN,
        "_note": "EU(NL+UK) transfer RF + per-state affine. Mapbox/AADT both negative; geometry suffices.",
        "_coeff_kind": "in-sample fit per state; true perf = in-city 5fold CV pooled-syd r0.70/MAE3.8",
        "global_affine": {"slope": float(gl.coef_[0]), "intercept": float(gl.intercept_), "n": int(len(yau))},
        "states": {},
    }
    print("\n按州仿射 (in-sample fit; r/MAE 仅参考, 真实表现看 CV):")
    for city, state in CITY_STATE.items():
        idx = cau == city
        if idx.sum() < 10:
            print(f"  {state} ({city}): 样本不足 {idx.sum()}, 跳过 -> 用 global")
            continue
        lin = LinearRegression().fit(raw[idx].reshape(-1, 1), yau[idx])
        pred = lin.predict(raw[idx].reshape(-1, 1))
        mae = float(np.mean(np.abs(pred - yau[idx])))
        r = float(np.corrcoef(pred, yau[idx])[0, 1])
        calib["states"][state] = {
            "slope": float(lin.coef_[0]), "intercept": float(lin.intercept_),
            "n": int(idx.sum()), "insample_mae": round(mae, 2), "insample_r": round(r, 2),
            "city_sample": city,
        }
        print(f"  {state} ({city}): n={int(idx.sum())} slope={lin.coef_[0]:.2f} "
              f"int={lin.intercept_:+.1f} (in-sample MAE={mae:.1f} r={r:.2f})")
    # QLD 无 SoundPLAN 样本 -> 全局兜底
    calib["states"]["QLD"] = {**calib["global_affine"], "fallback": "global (no QLD SoundPLAN sample)"}
    print(f"  QLD: 无样本 -> 全局兜底 slope={gl.coef_[0]:.2f} int={gl.intercept_:+.1f}")

    with open("data/noise_state_calibration.json", "w") as f:
        json.dump(calib, f, indent=2)
    print("\nsaved data/noise_state_calibration.json")
    print("⚠️ v0 用 cache 每城~350 清洗样本; 全量 11k 重算特征后可提稳 + 需 Bo sign-off 才部署")


if __name__ == "__main__":
    sys.exit(main())
