"""POC v2: Mapbox 时段拥堵幅度作为 AU 道路噪声残差校正层。

v1 (poc_mapbox.py) 判负, 但用错了字段: 取的是瞬时 speed annotation (实测对
depart_at 时段不敏感)。v2 改用 depart_at 取典型工作日高峰 vs 深夜的 duration,
派生"拥堵幅度"特征 (该路真实交通负荷的代理, 超出路类的非冗余信号):
  - mb_diurnal_slowdown = peak_dur / night_dur
  - mb_peak_congestion  = 高峰典型 congestion
  - mb_peak_speed_ratio = 高峰有效车速 / free-flow

每点 2 个 depart_at 请求 (高峰+深夜), 缓存防重。免费额度内。

跑法: export PROJ_LIB=...; PER_CITY=120 .venv/bin/python scripts/poc_mapbox_v2.py
"""
import os
import sys
import time
from datetime import datetime, timedelta

import duckdb
import numpy as np
from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from poc_eu_transfer5 import feats, fkeys, AU_CITIES  # noqa: E402
from property_scores.noise import mapbox_feats as mf  # noqa: E402
# 复用 v1 的 helper (load/od/metrics/校准/报表)
from poc_mapbox import (  # noqa: E402
    load_au_points, nearest_major_od, nearest_any_od, metrics,
    incity_affine_cv, report_table, CLASS_MAXSPEED, NFOLD,
)
from sklearn.model_selection import KFold  # noqa: E402

# 各城 UTC offset (2026-06 冬季, 无夏令时)
CITY_TZ = {"melbourne": 10, "sydney": 10, "canberra": 10, "hobart": 10,
           "brisbane": 10, "adelaide": 9.5, "perth": 8, "darwin": 9.5}
NEXT_TUE = datetime(2026, 6, 9)  # 固定一个典型工作日 (周二)


def utc_z(city, local_hour):
    off = CITY_TZ.get(city, 10)
    dt_utc = NEXT_TUE + timedelta(hours=local_hour) - timedelta(hours=off)
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def incity_gbr_cv(feat_mat, y, cities):
    """城内 5-fold GBR on [raw + diurnal 特征]。"""
    pred = np.zeros(len(y))
    for c in np.unique(cities):
        idx = np.where(cities == c)[0]
        if len(idx) < NFOLD:
            gbr = GradientBoostingRegressor(random_state=42).fit(feat_mat[idx], y[idx])
            pred[idx] = gbr.predict(feat_mat[idx])
            continue
        kf = KFold(n_splits=NFOLD, shuffle=True, random_state=42)
        for tr, te in kf.split(idx):
            gbr = GradientBoostingRegressor(n_estimators=300, max_depth=2,
                                            learning_rate=0.05, subsample=0.8, random_state=42)
            gbr.fit(feat_mat[idx[tr]], y[idx[tr]])
            pred[idx[te]] = gbr.predict(feat_mat[idx[te]])
    return pred


def main():
    t0 = time.time()
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial; SET preserve_insertion_order=false;")

    au = load_au_points()
    print(f"  total {len(au)} points ({time.time()-t0:.0f}s)", flush=True)

    bb_parts = []
    for c in AU_CITIES:
        las = [p[1] for p in au if p[0] == c]
        los = [p[2] for p in au if p[0] == c]
        if las:
            bb_parts.append(f"(bbox.xmin BETWEEN {min(los)-0.05} AND {max(los)+0.05} "
                            f"AND bbox.ymin BETWEEN {min(las)-0.05} AND {max(las)+0.05})")
    bb = " OR ".join(bb_parts)

    print("Building DuckDB tables...", flush=True)
    con.execute("CREATE TABLE poi AS SELECT lng,lat FROM read_parquet('data/eu/poi.parquet')")
    con.execute(f"""CREATE TABLE rr AS SELECT class, geometry, bbox.xmin AS xmin, bbox.ymin AS ymin
        FROM read_parquet('data/overture_roads.parquet')
        WHERE class IN ('motorway','trunk','primary','secondary','tertiary',
                        'residential','service','unclassified','living_street') AND ({bb})""")
    con.execute(f"""CREATE TABLE bb_t AS SELECT COALESCE(height,6.0) AS h,
        ST_X(ST_Centroid(geometry)) AS clng, ST_Y(ST_Centroid(geometry)) AS clat
        FROM read_parquet('data/overture_buildings.parquet') WHERE ({bb})""")
    print(f"  tables ready ({time.time()-t0:.0f}s)", flush=True)

    KEYS = fkeys()
    Xau, M, cities, ys = [], [], [], []
    print("Computing geo feats + diurnal Mapbox per point...", flush=True)
    for j, (c, la, lo, y) in enumerate(au):
        f = feats(con, "rr", "bb_t", "poi", la, lo)
        Xau.append([f[k] for k in KEYS])
        od, cls = nearest_major_od(con, la, lo)
        if od is None:
            od, cls = nearest_any_od(con, la, lo)
        fb_ms = CLASS_MAXSPEED.get(cls, 50)
        feat = mf.diurnal_features(od, cls or "residential", fb_ms,
                                   utc_z(c, 8.0), utc_z(c, 3.0))
        M.append([feat[k] for k in mf.DIURNAL_KEYS])
        cities.append(c)
        ys.append(y)
        if (j + 1) % 60 == 0:
            print(f"    {j+1}/{len(au)} ({time.time()-t0:.0f}s)", flush=True)

    Xau = np.array(Xau, float); M = np.array(M, float)
    cities = np.array(cities); yau = np.array(ys, float)
    print(f"  done ({time.time()-t0:.0f}s)", flush=True)

    d = np.load("data/eu/transfer5_cache.npz", allow_pickle=True)
    rf = RandomForestRegressor(n_estimators=400, min_samples_leaf=3, max_features="sqrt",
                               n_jobs=-1, random_state=42).fit(d["Xnl"], d["ynl"])
    raw = rf.predict(Xau)

    cal_raw = incity_affine_cv(raw, yau, cities)
    base_pool, base_nosyd = report_table("=== BASELINE (in-city affine raw->y) ===", cal_raw, yau, cities)

    feat_mat = np.column_stack([raw, M])
    mb_pred = incity_gbr_cv(feat_mat, yau, cities)
    mb_pool, mb_nosyd = report_table("=== +DIURNAL MAPBOX (in-city GBR [raw + 时段特征]) ===", mb_pred, yau, cities)

    print("\n=== DIAGNOSTICS ===", flush=True)
    ok = M[:, mf.DIURNAL_KEYS.index("mb_diurnal_ok")]
    print(f"diurnal_ok 覆盖率: {ok.mean()*100:.0f}% ({int(ok.sum())}/{len(ok)})", flush=True)
    resid = yau - cal_raw
    print("\n时段特征 与 (残差 y-cal_raw) / y 的 Pearson:", flush=True)
    print(f"  {'feature':<22} {'r_resid':>9} {'r_y':>8}", flush=True)
    for i, k in enumerate(mf.DIURNAL_KEYS):
        col = M[:, i]
        rr_res = float(np.corrcoef(col, resid)[0, 1]) if col.std() > 0 else 0.0
        rr_y = float(np.corrcoef(col, yau)[0, 1]) if col.std() > 0 else 0.0
        print(f"  {k:<22} {rr_res:>+9.3f} {rr_y:>+8.3f}", flush=True)
    # slowdown 仅在 ok 子集
    sd = M[:, mf.DIURNAL_KEYS.index("mb_diurnal_slowdown")]
    okm = ok > 0.5
    if okm.sum() > 10:
        print(f"\n  diurnal_slowdown 分布(ok子集): min={sd[okm].min():.2f} "
              f"med={np.median(sd[okm]):.2f} p90={np.percentile(sd[okm],90):.2f} max={sd[okm].max():.2f}", flush=True)
        rr = float(np.corrcoef(sd[okm], resid[okm])[0, 1])
        ry = float(np.corrcoef(sd[okm], yau[okm])[0, 1])
        print(f"  slowdown(ok子集) r_resid={rr:+.3f} r_y={ry:+.3f} (n={int(okm.sum())})", flush=True)

    print("\n=== DELTA (基线 -> +时段Mapbox) ===", flush=True)
    print(f"  pooled-syd : MAE {base_nosyd[0]:.1f}->{mb_nosyd[0]:.1f} ({mb_nosyd[0]-base_nosyd[0]:+.1f}) | "
          f"r {base_nosyd[2]:.2f}->{mb_nosyd[2]:.2f} ({mb_nosyd[2]-base_nosyd[2]:+.2f})", flush=True)
    print(f"  pooled+syd : MAE {base_pool[0]:.1f}->{mb_pool[0]:.1f} ({mb_pool[0]-base_pool[0]:+.1f}) | "
          f"r {base_pool[2]:.2f}->{mb_pool[2]:.2f} ({mb_pool[2]-base_pool[2]:+.2f})", flush=True)
    print(f"\nruntime {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
