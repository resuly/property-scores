"""全 AU 校准评估: 对比 (A) LOCO 跨城仿射(现状) vs (B) 城内自校准(上线真实)。

现状 poc_eu_transfer5.py 用"留一城, 其余城学仿射"评估 -> Melbourne bias -5.3,
因为跨城仿射对不准 Melbourne 绝对水平。但上线时每个城/州都有自己的 SoundPLAN
样本可学仿射, 所以真实表现应看"城内 K-fold 自校准"。本脚本验证后者能否消 bias。

注: 当前用 transfer5_cache.npz (每城~350采样的特征) 做概念验证; 概念通过后再
重算全量 11k AU 立面特征提稳 (calib_eval.py --full, 待加)。
"""
import os
import sys
import numpy as np
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error
from sklearn.model_selection import KFold

CACHE = os.environ.get("CACHE", "data/eu/transfer5_cache.npz")


def stats(p, t):
    return (mean_absolute_error(t, p), float(np.mean(p - t)),
            (float(np.corrcoef(p, t)[0, 1]) if p.std() > 0 else 0.0))


def main():
    d = np.load(CACHE, allow_pickle=True)
    Xnl, ynl, Xau, yau = d["Xnl"], d["ynl"], d["Xau"], d["yau"]
    cau = np.array(d["cau"])
    # 过滤 SoundPLAN 占位符/哨兵点 (d=e=n=1 -> lden~7; Sydney 样本 16% 是此类垃圾值)
    min_lden = float(os.environ.get("MIN_LDEN", "0"))
    if min_lden > 0:
        keep = yau >= min_lden
        print(f"[filter] dropped {(~keep).sum()}/{len(yau)} AU points with lden<{min_lden} "
              f"(per-city: " + ", ".join(f"{c}:{int(((~keep) & (cau==c)).sum())}" for c in sorted(set(cau.tolist()))) + ")", flush=True)
        Xau, yau, cau = Xau[keep], yau[keep], cau[keep]
    cities = sorted(set(cau.tolist()))
    print(f"train(EU)={len(ynl)}  test(AU)={len(yau)}  cities={cities}", flush=True)

    rf = RandomForestRegressor(n_estimators=400, min_samples_leaf=3,
                               max_features="sqrt", n_jobs=-1, random_state=42)
    rf.fit(Xnl, ynl)
    raw = rf.predict(Xau)
    a = stats(raw, yau)
    print(f"\nRAW EU->AU (no cal): MAE={a[0]:.1f} bias={a[1]:+.1f} r={a[2]:.2f}", flush=True)

    print("\n=== (A) 现状 LOCO 跨城仿射 (上线时若无该城样本) ===", flush=True)
    loco = np.zeros(len(yau))
    for h in cities:
        te = cau == h; tr = cau != h
        if te.sum() < 5:
            continue
        lin = LinearRegression().fit(raw[tr].reshape(-1, 1), yau[tr])
        loco[te] = lin.predict(raw[te].reshape(-1, 1))
        m = stats(loco[te], yau[te])
        print(f"  {h:10s} n={te.sum():4d} MAE={m[0]:.1f} bias={m[1]:+.1f} r={m[2]:.2f}", flush=True)
    m = stats(loco, yau)
    print(f"  POOLED       MAE={m[0]:.1f} bias={m[1]:+.1f} r={m[2]:.2f}", flush=True)

    print("\n=== (B) 上线真实: 城内 5-fold 自校准 (该城有 SoundPLAN 样本) ===", flush=True)
    within = np.zeros(len(yau))
    for h in cities:
        idx = np.where(cau == h)[0]
        if len(idx) < 10:
            lin = LinearRegression().fit(raw.reshape(-1, 1), yau)
            within[idx] = lin.predict(raw[idx].reshape(-1, 1))
            print(f"  {h:10s} n={len(idx):4d} (too few -> global affine)", flush=True)
            continue
        kf = KFold(5, shuffle=True, random_state=1)
        for trf, tef in kf.split(idx):
            lin = LinearRegression().fit(raw[idx[trf]].reshape(-1, 1), yau[idx[trf]])
            within[idx[tef]] = lin.predict(raw[idx[tef]].reshape(-1, 1))
        m = stats(within[idx], yau[idx])
        print(f"  {h:10s} n={len(idx):4d} MAE={m[0]:.1f} bias={m[1]:+.1f} r={m[2]:.2f}", flush=True)
    m = stats(within, yau)
    print(f"  POOLED       MAE={m[0]:.1f} bias={m[1]:+.1f} r={m[2]:.2f}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
