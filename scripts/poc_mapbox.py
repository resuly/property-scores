"""POC: Mapbox 速度特征作为 AU 道路噪声迁移模型的"残差校正层"。

问题: EU(NL+UK)->AU 迁移 + 城内仿射校准已到 r~0.55 平台。几何/AADT 特征都从
路类派生(冗余),无法再涨。假设: Mapbox Directions 观测的实际车速/拥堵是唯一
非冗余的真实交通信号,作为残差校正层能破平台。

流程:
1. 每城 PER_CITY 点(antn_<city>_buildings_.csv 均匀采样, y=lden>0)
2. DuckDB: overture roads/buildings(限子集 bbox) + eu/poi
3. 每点: 75 几何特征(poc_eu_transfer5.feats) + 最近主干道 OD -> Mapbox 4 特征
4. RF(EU 训练) -> raw 预测
5. 基线: 城内 5-fold 仿射校准 raw->y
6. +Mapbox: 城内 5-fold GBR on [raw + 4 mapbox 列]
7. 诊断: 覆盖率 / congestion 有值率 / Mapbox 特征-残差相关
8. sydney 单独 + pooled 含/不含 sydney

不改 mapbox_feats.py / poc_eu_transfer5.py。约束: 免费额度内(~840 请求, 缓存防重)。
"""
import csv
import os
import re
import sys
import time

import duckdb
import numpy as np
from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error
from sklearn.model_selection import KFold

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from poc_eu_transfer5 import feats, fkeys, lden, AU_CITIES  # noqa: E402
from property_scores.noise import mapbox_feats as mf  # noqa: E402

PER_CITY = int(os.environ.get("PER_CITY", "120"))
NFOLD = 5
CLASS_MAXSPEED = {
    "motorway": 100, "trunk": 80, "primary": 60, "secondary": 50, "tertiary": 50,
    "residential": 40, "service": 30, "unclassified": 50, "living_street": 20,
}
MAJOR = ("motorway", "trunk", "primary", "secondary", "tertiary")


def load_au_points():
    """每城均匀采样 PER_CITY 点。geometry 是 'POINT (lat lng)' (非标准顺序!)。"""
    pts = []
    for c in AU_CITIES:
        fn = f"data/ambient_sample/antn_{c}_buildings_.csv"
        if not os.path.exists(fn):
            print(f"  [skip] {c}: no csv", flush=True)
            continue
        rows = list(csv.DictReader(open(fn)))
        kept = []
        for r in rows:
            m = re.search(r"POINT \(([-\d.]+) ([-\d.]+)\)", r["geometry"])
            if not m:
                continue
            la, lo = float(m.group(1)), float(m.group(2))  # POINT(lat lng)
            try:
                y = lden(float(r["sp_rd_max_d"]), float(r["sp_rd_max_e"]), float(r["sp_rd_max_n"]))
            except (ValueError, KeyError):
                continue
            if y > 0:
                kept.append((c, la, lo, y))
        # 均匀采样到 PER_CITY
        if len(kept) > PER_CITY:
            step = len(kept) / PER_CITY
            kept = [kept[int(i * step)] for i in range(PER_CITY)]
        pts.extend(kept)
        print(f"  {c}: {len(kept)} points", flush=True)
    return pts


def nearest_major_od(con, lat, lng):
    """最近主干道的一段 OD + class。起终点相同或无结果 -> (None, None)。"""
    r = con.execute(f"""
        SELECT class, ST_X(ST_StartPoint(geometry)), ST_Y(ST_StartPoint(geometry)),
               ST_X(ST_EndPoint(geometry)), ST_Y(ST_EndPoint(geometry))
        FROM rr
        WHERE xmin BETWEEN {lng-0.013} AND {lng+0.013}
          AND ymin BETWEEN {lat-0.013} AND {lat+0.013}
          AND class IN ('motorway','trunk','primary','secondary','tertiary')
        ORDER BY ST_Distance(geometry, ST_Point({lng},{lat})) LIMIT 1
    """).fetchone()
    if not r:
        return None, None
    cls, sx, sy, ex, ey = r
    if sx == ex and sy == ey:
        return None, cls
    return [(sx, sy), (ex, ey)], cls


def nearest_any_od(con, lat, lng):
    """无主干道时, 取最近任意路(含 residential 等)做兜底 OD + class。"""
    r = con.execute(f"""
        SELECT class, ST_X(ST_StartPoint(geometry)), ST_Y(ST_StartPoint(geometry)),
               ST_X(ST_EndPoint(geometry)), ST_Y(ST_EndPoint(geometry))
        FROM rr
        WHERE xmin BETWEEN {lng-0.013} AND {lng+0.013}
          AND ymin BETWEEN {lat-0.013} AND {lat+0.013}
        ORDER BY ST_Distance(geometry, ST_Point({lng},{lat})) LIMIT 1
    """).fetchone()
    if not r:
        return None, "residential"
    cls, sx, sy, ex, ey = r
    if sx == ex and sy == ey:
        return None, cls
    return [(sx, sy), (ex, ey)], cls


def metrics(p, t):
    p, t = np.asarray(p, float), np.asarray(t, float)
    mae = mean_absolute_error(t, p)
    bias = float(np.mean(p - t))
    r = float(np.corrcoef(p, t)[0, 1]) if p.std() > 0 and t.std() > 0 else 0.0
    return mae, bias, r


def incity_affine_cv(raw, y, cities):
    """基线: 城内 5-fold, LinearRegression raw->y。返回 out-of-fold 预测。"""
    pred = np.zeros(len(y))
    for c in np.unique(cities):
        idx = np.where(cities == c)[0]
        if len(idx) < NFOLD:
            # 太少做不了 CV -> 全城 fit 全城 predict (退化, 仅占位)
            lin = LinearRegression().fit(raw[idx].reshape(-1, 1), y[idx])
            pred[idx] = lin.predict(raw[idx].reshape(-1, 1))
            continue
        kf = KFold(n_splits=NFOLD, shuffle=True, random_state=42)
        for tr, te in kf.split(idx):
            tri, tei = idx[tr], idx[te]
            lin = LinearRegression().fit(raw[tri].reshape(-1, 1), y[tri])
            pred[tei] = lin.predict(raw[tei].reshape(-1, 1))
    return pred


def incity_mapbox_cv(feat_mat, y, cities):
    """+Mapbox: 城内 5-fold GBR on [raw + mapbox 4 列]。返回 out-of-fold 预测。"""
    pred = np.zeros(len(y))
    for c in np.unique(cities):
        idx = np.where(cities == c)[0]
        if len(idx) < NFOLD:
            gbr = GradientBoostingRegressor(random_state=42)
            gbr.fit(feat_mat[idx], y[idx])
            pred[idx] = gbr.predict(feat_mat[idx])
            continue
        kf = KFold(n_splits=NFOLD, shuffle=True, random_state=42)
        for tr, te in kf.split(idx):
            tri, tei = idx[tr], idx[te]
            gbr = GradientBoostingRegressor(
                n_estimators=300, max_depth=2, learning_rate=0.05,
                subsample=0.8, random_state=42)
            gbr.fit(feat_mat[tri], y[tri])
            pred[tei] = gbr.predict(feat_mat[tei])
    return pred


def report_table(title, pred, y, cities):
    print(f"\n{title}", flush=True)
    print(f"  {'city':<10} {'n':>4} | {'MAE':>5} {'bias':>6} {'r':>5}", flush=True)
    order = ["sydney"] + [c for c in AU_CITIES if c != "sydney"]
    for c in order:
        idx = np.where(cities == c)[0]
        if len(idx) == 0:
            continue
        m = metrics(pred[idx], y[idx])
        flag = "  <- sydney (离群)" if c == "sydney" else ""
        print(f"  {c:<10} {len(idx):>4} | {m[0]:>5.1f} {m[1]:>+6.1f} {m[2]:>5.2f}{flag}", flush=True)
    # pooled 含 sydney
    m = metrics(pred, y)
    print(f"  {'POOLED+syd':<10} {len(y):>4} | {m[0]:>5.1f} {m[1]:>+6.1f} {m[2]:>5.2f}", flush=True)
    # pooled 不含 sydney
    mask = cities != "sydney"
    m = metrics(pred[mask], y[mask])
    print(f"  {'POOLED-syd':<10} {mask.sum():>4} | {m[0]:>5.1f} {m[1]:>+6.1f} {m[2]:>5.2f}", flush=True)
    return metrics(pred, y), metrics(pred[mask], y[mask])


def main():
    t0 = time.time()
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial; SET preserve_insertion_order=false;")

    print(f"Loading AU points (PER_CITY={PER_CITY})...", flush=True)
    au = load_au_points()
    print(f"  total {len(au)} points ({time.time()-t0:.0f}s)", flush=True)

    # 子集 bbox: 每城 ±0.05 的 OR 条件
    bb_parts = []
    for c in AU_CITIES:
        las = [p[1] for p in au if p[0] == c]
        los = [p[2] for p in au if p[0] == c]
        if not las:
            continue
        bb_parts.append(
            f"(bbox.xmin BETWEEN {min(los)-0.05} AND {max(los)+0.05} "
            f"AND bbox.ymin BETWEEN {min(las)-0.05} AND {max(las)+0.05})")
    bb = " OR ".join(bb_parts)

    print("Building DuckDB tables...", flush=True)
    con.execute("CREATE TABLE poi AS SELECT lng,lat FROM read_parquet('data/eu/poi.parquet')")
    con.execute(f"""CREATE TABLE rr AS
        SELECT class, geometry, bbox.xmin AS xmin, bbox.ymin AS ymin
        FROM read_parquet('data/overture_roads.parquet')
        WHERE class IN ('motorway','trunk','primary','secondary','tertiary',
                        'residential','service','unclassified','living_street')
          AND ({bb})""")
    con.execute(f"""CREATE TABLE bb_t AS
        SELECT COALESCE(height,6.0) AS h,
               ST_X(ST_Centroid(geometry)) AS clng,
               ST_Y(ST_Centroid(geometry)) AS clat
        FROM read_parquet('data/overture_buildings.parquet')
        WHERE ({bb})""")
    print(f"  rr={con.execute('SELECT count(*) FROM rr').fetchone()[0]} "
          f"bb_t={con.execute('SELECT count(*) FROM bb_t').fetchone()[0]} "
          f"poi={con.execute('SELECT count(*) FROM poi').fetchone()[0]} "
          f"({time.time()-t0:.0f}s)", flush=True)

    KEYS = fkeys()
    Xau, M, cities, ys = [], [], [], []
    n_no_od = 0
    print("Computing features + Mapbox per point...", flush=True)
    for j, (c, la, lo, y) in enumerate(au):
        # a. 几何特征
        f = feats(con, "rr", "bb_t", "poi", la, lo)
        Xau.append([f[k] for k in KEYS])
        # b. 最近主干道 OD
        od, cls = nearest_major_od(con, la, lo)
        if od is None:
            # 兜底: 最近任意路
            od, cls = nearest_any_od(con, la, lo)
            if od is None:
                n_no_od += 1
        fb_ms = CLASS_MAXSPEED.get(cls, 50)
        # c. Mapbox 特征 (od=None 时 point_features 兜底, mapbox_ok=0)
        feat = mf.point_features(od, cls or "residential", fb_ms)
        M.append([feat[k] for k in mf.FEATURE_KEYS])
        cities.append(c)
        ys.append(y)
        if (j + 1) % 60 == 0:
            print(f"    {j+1}/{len(au)} ({time.time()-t0:.0f}s)", flush=True)

    Xau = np.array(Xau, float)
    M = np.array(M, float)
    cities = np.array(cities)
    yau = np.array(ys, float)
    print(f"  features done. {n_no_od} points had no OD at all ({time.time()-t0:.0f}s)", flush=True)

    # 4. RF 训练于 EU, 预测 AU -> raw
    d = np.load("data/eu/transfer5_cache.npz", allow_pickle=True)
    Xnl, ynl = d["Xnl"], d["ynl"]
    print(f"\nEU train set: Xnl={Xnl.shape} ynl mean={ynl.mean():.1f}", flush=True)
    rf = RandomForestRegressor(
        n_estimators=400, min_samples_leaf=3, max_features="sqrt",
        n_jobs=-1, random_state=42)
    rf.fit(Xnl, ynl)
    raw = rf.predict(Xau)
    m = metrics(raw, yau)
    print(f"RAW EU->AU (no cal): MAE={m[0]:.1f} bias={m[1]:+.1f} r={m[2]:.2f} "
          f"(y std={yau.std():.1f})", flush=True)

    # 5. 基线: 城内仿射校准
    cal_raw = incity_affine_cv(raw, yau, cities)
    base_pool, base_nosyd = report_table(
        "=== BASELINE (in-city 5-fold affine raw->y) ===", cal_raw, yau, cities)

    # 6. +Mapbox: 城内 GBR on [raw + mapbox 4]
    feat_mat = np.column_stack([raw, M])
    mb_pred = incity_mapbox_cv(feat_mat, yau, cities)
    mb_pool, mb_nosyd = report_table(
        "=== +MAPBOX (in-city 5-fold GBR on [raw + mapbox]) ===", mb_pred, yau, cities)

    # 7. 诊断
    print("\n=== DIAGNOSTICS ===", flush=True)
    ok = M[:, mf.FEATURE_KEYS.index("mapbox_ok")]
    cong = M[:, mf.FEATURE_KEYS.index("mapbox_congestion")]
    print(f"mapbox_ok 覆盖率 (整体): {ok.mean()*100:.0f}%  ({int(ok.sum())}/{len(ok)})", flush=True)
    print("mapbox_ok 覆盖率 (按城):", flush=True)
    for c in (["sydney"] + [x for x in AU_CITIES if x != "sydney"]):
        idx = np.where(cities == c)[0]
        if len(idx):
            print(f"    {c:<10} {ok[idx].mean()*100:>3.0f}%", flush=True)
    cong_has = (cong != -1.0)
    print(f"congestion 有值率 (!=-1): {cong_has.mean()*100:.0f}%  "
          f"({int(cong_has.sum())}/{len(cong)})", flush=True)

    # 残差(基线) 与 y 相关
    resid = yau - cal_raw
    print("\nMapbox 特征 与 (残差 y-cal_raw) / y 的 Pearson 相关:", flush=True)
    print(f"  {'feature':<22} {'r_residual':>10} {'r_y':>8}", flush=True)
    for i, k in enumerate(mf.FEATURE_KEYS):
        col = M[:, i]
        if col.std() == 0:
            rr_res, rr_y = 0.0, 0.0
        else:
            rr_res = float(np.corrcoef(col, resid)[0, 1])
            rr_y = float(np.corrcoef(col, yau)[0, 1])
        print(f"  {k:<22} {rr_res:>+10.3f} {rr_y:>+8.3f}", flush=True)
    # congestion 仅在有值子集上的相关 (更诚实)
    if cong_has.sum() > 10:
        rc_res = float(np.corrcoef(cong[cong_has], resid[cong_has])[0, 1])
        rc_y = float(np.corrcoef(cong[cong_has], yau[cong_has])[0, 1])
        print(f"  {'congestion(有值子集)':<22} {rc_res:>+10.3f} {rc_y:>+8.3f} "
              f"(n={int(cong_has.sum())})", flush=True)

    # 结论数字
    print("\n=== DELTA (基线 -> +Mapbox) ===", flush=True)
    print(f"  pooled+syd : MAE {base_pool[0]:.1f} -> {mb_pool[0]:.1f} "
          f"({mb_pool[0]-base_pool[0]:+.1f}) | r {base_pool[2]:.2f} -> {mb_pool[2]:.2f} "
          f"({mb_pool[2]-base_pool[2]:+.2f})", flush=True)
    print(f"  pooled-syd : MAE {base_nosyd[0]:.1f} -> {mb_nosyd[0]:.1f} "
          f"({mb_nosyd[0]-base_nosyd[0]:+.1f}) | r {base_nosyd[2]:.2f} -> {mb_nosyd[2]:.2f} "
          f"({mb_nosyd[2]-base_nosyd[2]:+.2f})", flush=True)
    print(f"\nTotal runtime: {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
