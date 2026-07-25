"""POC: 真实实测 AADT(年均日车流量)作为 AU 道路噪声迁移模型的"残差校正层"。

背景: EU(NL+UK)->AU 迁移 RF + 清洗哨兵(过滤 lden<30) + 按州自校准 已到
pooled(不含 sydney) r 0.63 / MAE 3.8 平台。Mapbox 速度/拥堵特征已判负
(拥堵幅度 != 车流量)。现在测真实 AADT —— 它是年均车数,是年均 Lden 的物理
驱动量,与目标对齐,理论上比 Mapbox 该有信号。

数据: data/aadt_{vic,nsw,qld,sa,wa}.parquet。统一 schema:
    aadt INTEGER, hv_pct DOUBLE(0..1), road_name VARCHAR, geometry GEOMETRY(WGS84),
    xmin DOUBLE, ymin DOUBLE
覆盖: VIC 逐段密 / QLD 点密 / SA 逐段 / NSW 站稀 / WA 站稀。
ACT(canberra) / TAS(hobart) / NT(darwin) 无 AADT。

城->州映射(仅有 AADT 的): melbourne=vic, sydney=nsw, adelaide=sa, perth=wa。
(QLD 无对应采样城,不用; canberra/hobart/darwin 无 AADT -> 缺失哨兵。)

流程:
1. 每城 120 点(过滤 lden<30 清洗哨兵), 算 75 几何特征 -> RF(EU 训练) -> raw 预测。
2. 城->州, 在该州 aadt parquet bbox(±0.05) 预过滤 + ST_Distance 找最近 AADT 记录,
   取 aadt / hv_pct / 距离(米)。无州数据或无命中 -> 缺失。
3. AADT 特征(4列): log1p(aadt), hv_pct(缺=-1), log1p(dist_m), aadt_ok(0/1)。
4. 基线: 城内 5-fold 仿射 raw->y。+AADT: 城内 5-fold GBR on [raw + 4 AADT列]。
5. 诊断: AADT 4 特征对残差(y-cal_raw)和对 y 的 Pearson; 覆盖率分档(<200/500/1km/>1km)。
6. 评估: per-city + pooled(含/不含 sydney)。重点看有 AADT 的 4 州。

不改 property-scores 现有代码。复用 poc_mapbox / poc_eu_transfer5 的 helper。
"""
import csv
import os
import re
import sys
import time

import duckdb
import numpy as np
from sklearn.ensemble import RandomForestRegressor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

# 复用 poc_mapbox 的 helper (metrics / incity_affine_cv / report_table)
from poc_mapbox import metrics, incity_affine_cv, report_table  # noqa: E402
# 复用 poc_eu_transfer5 的 几何特征 / fkeys / lden / 城市列表
from poc_eu_transfer5 import feats, fkeys, lden, AU_CITIES  # noqa: E402
# GBR 直接用 sklearn (poc_mapbox 的 incity_mapbox_cv 写死了 mapbox 语义, 这里自建对称版本)
from sklearn.ensemble import GradientBoostingRegressor  # noqa: E402
from sklearn.model_selection import KFold  # noqa: E402

PER_CITY = int(os.environ.get("PER_CITY", "120"))
NFOLD = 5

# 城 -> 州 (仅有 AADT 数据的): melbourne=vic, sydney=nsw, adelaide=sa, perth=wa
CITY_STATE = {
    "melbourne": "vic",
    "sydney": "nsw",
    "adelaide": "sa",
    "perth": "wa",
    # hobart(TAS) / canberra(ACT) / darwin(NT) 无 AADT -> 不在映射里
}
AADT_PARQUET = {st: f"data/aadt_{st}.parquet" for st in ("vic", "nsw", "qld", "sa", "wa")}

# AADT 特征列顺序 (4 列)
AADT_KEYS = ["log1p_aadt", "hv_pct", "log1p_dist_m", "aadt_ok"]
SENT_AADT = 0.0      # log1p(0) = 0
SENT_HV = -1.0       # 缺失 hv 哨兵
SENT_DIST = np.log1p(5000.0)  # 缺失距离哨兵: 设为远距离 ~log1p(5km)


def load_au_points_clean():
    """每城均匀采样 PER_CITY 点。过滤 lden<30 清洗哨兵(sydney 16% 是 d=e=n=1 占位符)。

    geometry 字段是 'POINT (lat lng)' (非标准顺序, 第一个是纬度)。
    """
    pts = []
    for c in AU_CITIES:
        fn = f"data/ambient_sample/antn_{c}_buildings_.csv"
        if not os.path.exists(fn):
            print(f"  [skip] {c}: no csv", flush=True)
            continue
        rows = list(csv.DictReader(open(fn)))
        kept = []
        n_drop = 0
        for r in rows:
            m = re.search(r"POINT \(([-\d.]+) ([-\d.]+)\)", r["geometry"])
            if not m:
                continue
            la, lo = float(m.group(1)), float(m.group(2))  # POINT(lat lng)
            try:
                y = lden(float(r["sp_rd_max_d"]), float(r["sp_rd_max_e"]),
                         float(r["sp_rd_max_n"]))
            except (ValueError, KeyError):
                continue
            if y <= 0:
                continue
            if y < 30:  # 清洗哨兵: 过滤占位符 / 异常低值
                n_drop += 1
                continue
            kept.append((c, la, lo, y))
        if len(kept) > PER_CITY:
            step = len(kept) / PER_CITY
            kept = [kept[int(i * step)] for i in range(PER_CITY)]
        pts.extend(kept)
        print(f"  {c}: {len(kept)} points (dropped {n_drop} lden<30)", flush=True)
    return pts


def build_aadt_tables(con, au):
    """为每个用到的州建一个内存表 (限子集 bbox)。键 = 州名。

    只为有采样点的州建表。bbox = 该州所有采样城点 ±0.1 的 OR 并集。
    返回: 该州表是否建成功的 dict {state: bool}。
    """
    built = {}
    # 按州聚合点
    state_pts = {}
    for c, la, lo, y in au:
        st = CITY_STATE.get(c)
        if st is None:
            continue
        state_pts.setdefault(st, []).append((la, lo))
    for st, pp in state_pts.items():
        pq = AADT_PARQUET.get(st)
        if not pq or not os.path.exists(pq):
            print(f"  [aadt] {st}: parquet 缺失 -> 该州点全部缺失", flush=True)
            built[st] = False
            continue
        las = [p[0] for p in pp]
        los = [p[1] for p in pp]
        # 稍宽 bbox (±0.1) 以确保稀疏州也能命中最近记录
        bb = (f"xmin BETWEEN {min(los)-0.1} AND {max(los)+0.1} "
              f"AND ymin BETWEEN {min(las)-0.1} AND {max(las)+0.1}")
        tname = f"aadt_{st}"
        con.execute(f"DROP TABLE IF EXISTS {tname}")
        con.execute(f"""CREATE TABLE {tname} AS
            SELECT aadt, hv_pct, geometry, xmin, ymin
            FROM read_parquet('{pq}')
            WHERE {bb} AND aadt > 0""")
        n = con.execute(f"SELECT count(*) FROM {tname}").fetchone()[0]
        print(f"  [aadt] {st}: {n} records in subset bbox", flush=True)
        built[st] = n > 0
    return built


def nearest_aadt(con, state, lat, lng):
    """在该州 aadt 表里 bbox(±0.05) 预过滤 + ST_Distance 找最近记录。

    返回 (aadt, hv_pct, dist_m) 或 None(无命中)。dist_m 用真实米数
    (ST_Distance 给度, 乘 mpd)。
    """
    import math
    mpd = 111_320 * math.cos(math.radians(lat))
    tname = f"aadt_{state}"
    r = con.execute(f"""
        SELECT aadt, hv_pct,
               ST_Distance(geometry, ST_Point({lng},{lat})) * {mpd} AS dist_m
        FROM {tname}
        WHERE xmin BETWEEN {lng-0.05} AND {lng+0.05}
          AND ymin BETWEEN {lat-0.05} AND {lat+0.05}
        ORDER BY ST_Distance(geometry, ST_Point({lng},{lat}))
        LIMIT 1
    """).fetchone()
    if not r:
        return None
    aadt, hv_pct, dist_m = r
    return float(aadt), float(hv_pct), float(dist_m)


def incity_aadt_cv(feat_mat, y, cities):
    """+AADT: 城内 5-fold GBR on [raw + 4 AADT 列]。返回 out-of-fold 预测。

    与 poc_mapbox.incity_mapbox_cv 对称(同 GBR 超参), 只是输入列换成 AADT。
    """
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


def main():
    t0 = time.time()
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial; SET preserve_insertion_order=false;")

    print(f"Loading AU points (PER_CITY={PER_CITY}, 过滤 lden<30)...", flush=True)
    au = load_au_points_clean()
    print(f"  total {len(au)} points ({time.time()-t0:.0f}s)", flush=True)

    # ---- 子集 bbox: overture roads/buildings + eu/poi (同 poc_mapbox) ----
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

    print("Building DuckDB tables (overture roads/buildings + poi)...", flush=True)
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

    # ---- AADT 州表 ----
    print("Building per-state AADT tables...", flush=True)
    aadt_built = build_aadt_tables(con, au)

    # ---- 逐点: 几何特征 + 最近 AADT ----
    KEYS = fkeys()
    Xau, A, cities, ys = [], [], [], []
    aadt_dist_raw = []   # 真实米距离 (诊断分档用), 缺失 = np.inf
    print("Computing geometry features + nearest AADT per point...", flush=True)
    for j, (c, la, lo, y) in enumerate(au):
        f = feats(con, "rr", "bb_t", "poi", la, lo)
        Xau.append([f[k] for k in KEYS])

        st = CITY_STATE.get(c)
        hit = None
        if st is not None and aadt_built.get(st):
            hit = nearest_aadt(con, st, la, lo)
        if hit is None:
            # 缺失哨兵
            A.append([SENT_AADT, SENT_HV, SENT_DIST, 0.0])
            aadt_dist_raw.append(np.inf)
        else:
            aadt, hv_pct, dist_m = hit
            A.append([np.log1p(aadt), hv_pct, np.log1p(dist_m), 1.0])
            aadt_dist_raw.append(dist_m)

        cities.append(c)
        ys.append(y)
        if (j + 1) % 120 == 0:
            print(f"    {j+1}/{len(au)} ({time.time()-t0:.0f}s)", flush=True)

    Xau = np.array(Xau, float)
    A = np.array(A, float)
    cities = np.array(cities)
    yau = np.array(ys, float)
    aadt_dist_raw = np.array(aadt_dist_raw, float)
    print(f"  features done ({time.time()-t0:.0f}s)", flush=True)

    # ---- RF 训练于 EU, 预测 AU -> raw ----
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

    # ---- 基线: 城内 5-fold 仿射 raw->y ----
    cal_raw = incity_affine_cv(raw, yau, cities)
    base_pool, base_nosyd = report_table(
        "=== BASELINE (in-city 5-fold affine raw->y) ===", cal_raw, yau, cities)

    # ---- +AADT: 城内 5-fold GBR on [raw + 4 AADT 列] ----
    feat_mat = np.column_stack([raw, A])
    aadt_pred = incity_aadt_cv(feat_mat, yau, cities)
    aadt_pool, aadt_nosyd = report_table(
        "=== +AADT (in-city 5-fold GBR on [raw + 4 AADT cols]) ===",
        aadt_pred, yau, cities)

    # ================= 诊断 =================
    print("\n=== DIAGNOSTICS ===", flush=True)
    okcol = A[:, AADT_KEYS.index("aadt_ok")]
    print(f"aadt_ok 覆盖率 (整体): {okcol.mean()*100:.0f}%  "
          f"({int(okcol.sum())}/{len(okcol)})", flush=True)
    print("aadt_ok 覆盖率 (按城; 仅 vic/nsw/sa/wa 有州数据):", flush=True)
    for c in (["sydney"] + [x for x in AU_CITIES if x != "sydney"]):
        idx = np.where(cities == c)[0]
        if len(idx):
            st = CITY_STATE.get(c, "—无AADT—")
            print(f"    {c:<10} (state={st:<8}) {okcol[idx].mean()*100:>3.0f}%",
                  flush=True)

    # 覆盖率分档 (按城): 最近 AADT < 200m / <500m / <1km / >1km
    print("\nAADT 最近距离分档 (按城, 各档占比):", flush=True)
    print(f"  {'city':<10} {'state':<6} {'<200m':>7} {'<500m':>7} "
          f"{'<1km':>7} {'>1km/无':>8}", flush=True)
    for c in (["sydney"] + [x for x in AU_CITIES if x != "sydney"]):
        idx = np.where(cities == c)[0]
        if len(idx) == 0:
            continue
        dd = aadt_dist_raw[idx]
        n = len(dd)
        p200 = np.mean(dd < 200) * 100
        p500 = np.mean((dd >= 200) & (dd < 500)) * 100
        p1k = np.mean((dd >= 500) & (dd < 1000)) * 100
        pfar = np.mean(dd >= 1000) * 100  # 含 inf(缺失)
        st = CITY_STATE.get(c, "none")
        print(f"  {c:<10} {st:<6} {p200:>6.0f}% {p500:>6.0f}% "
              f"{p1k:>6.0f}% {pfar:>7.0f}%", flush=True)

    # AADT 特征 与 (残差 y-cal_raw) / y 的 Pearson
    resid = yau - cal_raw
    print("\nAADT 4 特征 与 (残差 y-cal_raw) / y 的 Pearson 相关 (全样本):", flush=True)
    print(f"  {'feature':<16} {'r_residual':>11} {'r_y':>8}", flush=True)
    for i, k in enumerate(AADT_KEYS):
        col = A[:, i]
        if col.std() == 0:
            rr_res, rr_y = 0.0, 0.0
        else:
            rr_res = float(np.corrcoef(col, resid)[0, 1])
            rr_y = float(np.corrcoef(col, yau)[0, 1])
        print(f"  {k:<16} {rr_res:>+11.3f} {rr_y:>+8.3f}", flush=True)

    # 仅在 aadt_ok=1 子集上的 Pearson (更诚实: 不被缺失哨兵稀释)
    okmask = okcol == 1.0
    if okmask.sum() > 10:
        print(f"\nAADT 特征相关 (仅 aadt_ok=1 子集, n={int(okmask.sum())}):", flush=True)
        print(f"  {'feature':<16} {'r_residual':>11} {'r_y':>8}", flush=True)
        for i, k in enumerate(AADT_KEYS):
            if k == "aadt_ok":
                continue
            col = A[okmask, i]
            if col.std() == 0:
                rr_res, rr_y = 0.0, 0.0
            else:
                rr_res = float(np.corrcoef(col, resid[okmask])[0, 1])
                rr_y = float(np.corrcoef(col, yau[okmask])[0, 1])
            print(f"  {k:<16} {rr_res:>+11.3f} {rr_y:>+8.3f}", flush=True)

    # 密集州 vs 稀疏州 子集对比 (仅 aadt_ok=1)
    print("\n密集 vs 稀疏州: log1p(aadt) 与 y 的相关 (仅 aadt_ok=1, 按州):", flush=True)
    laadt = A[:, AADT_KEYS.index("log1p_aadt")]
    for st_grp, label in [(["melbourne"], "vic(密)"),
                          (["sydney"], "nsw(稀,弱样本)"),
                          (["adelaide"], "sa(逐段)"),
                          (["perth"], "wa(稀)")]:
        cmask = np.isin(cities, st_grp) & okmask
        if cmask.sum() > 5:
            col = laadt[cmask]
            if col.std() > 0:
                rr_y = float(np.corrcoef(col, yau[cmask])[0, 1])
                rr_res = float(np.corrcoef(col, resid[cmask])[0, 1])
            else:
                rr_y, rr_res = 0.0, 0.0
            print(f"    {label:<16} n_ok={int(cmask.sum()):>3} | "
                  f"r_y={rr_y:>+.3f}  r_residual={rr_res:>+.3f}", flush=True)
        else:
            print(f"    {label:<16} n_ok={int(cmask.sum()):>3} | (样本不足)",
                  flush=True)

    # ================= 结论数字 =================
    print("\n=== DELTA (基线 -> +AADT) ===", flush=True)
    print(f"  pooled+syd : MAE {base_pool[0]:.1f} -> {aadt_pool[0]:.1f} "
          f"({aadt_pool[0]-base_pool[0]:+.1f}) | r {base_pool[2]:.2f} -> "
          f"{aadt_pool[2]:.2f} ({aadt_pool[2]-base_pool[2]:+.2f})", flush=True)
    print(f"  pooled-syd : MAE {base_nosyd[0]:.1f} -> {aadt_nosyd[0]:.1f} "
          f"({aadt_nosyd[0]-base_nosyd[0]:+.1f}) | r {base_nosyd[2]:.2f} -> "
          f"{aadt_nosyd[2]:.2f} ({aadt_nosyd[2]-base_nosyd[2]:+.2f})", flush=True)

    # 重点 4 州 (有 AADT) 的合并对比
    aadt_states_mask = np.isin(cities, list(CITY_STATE.keys()))
    base4 = metrics(cal_raw[aadt_states_mask], yau[aadt_states_mask])
    aadt4 = metrics(aadt_pred[aadt_states_mask], yau[aadt_states_mask])
    print(f"  AADT-4州池: MAE {base4[0]:.1f} -> {aadt4[0]:.1f} "
          f"({aadt4[0]-base4[0]:+.1f}) | r {base4[2]:.2f} -> {aadt4[2]:.2f} "
          f"({aadt4[2]-base4[2]:+.2f})  (melb/syd/adel/perth)", flush=True)
    # 重点 4 州不含 sydney (干净对比 0.63 平台)
    m4ns = np.isin(cities, ["melbourne", "adelaide", "perth"])
    base4ns = metrics(cal_raw[m4ns], yau[m4ns])
    aadt4ns = metrics(aadt_pred[m4ns], yau[m4ns])
    print(f"  AADT-3州池(不含syd): MAE {base4ns[0]:.1f} -> {aadt4ns[0]:.1f} "
          f"({aadt4ns[0]-base4ns[0]:+.1f}) | r {base4ns[2]:.2f} -> "
          f"{aadt4ns[2]:.2f} ({aadt4ns[2]-base4ns[2]:+.2f})  (melb/adel/perth)",
          flush=True)

    print(f"\nTotal runtime: {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
