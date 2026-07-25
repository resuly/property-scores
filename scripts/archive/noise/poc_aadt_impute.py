"""Add an AADT-imputation feature (Shen-style) to the transfer model and test if
it breaks the r~0.55 plateau.

Train an imputer on AU MEASURED AADT (geodata features -> log AADT), then apply
it to the cached NL/UK/AU noise feature vectors as an extra 'imputed_aadt'
feature. This injects AU traffic-volume knowledge the noise RF (trained on NL/UK
noise) cannot see. Retrain + LOCO vs v5 baseline.
"""
import csv
import math
import os
import time

import duckdb
import numpy as np
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.poc_eu_transfer3 import feats, fkeys, AU_CITIES, load_au_points  # reuse

KEYS = fkeys()


def sample_au_aadt(con, bb, n=6000):
    """Sample measured AADT (VIC/QLD/SA) within the AU city bboxes."""
    rows = []
    # VIC + SA are linestrings (segment midpoint), QLD is points
    for f, is_line in [("aadt_vic", True), ("aadt_sa", True), ("aadt_nsw", False),
                       ("aadt_wa", False), ("aadt_qld", False)]:
        path = f"data/{f}.parquet"
        if not os.path.exists(path):
            continue
        geom = "ST_Centroid(geometry)" if is_line else "geometry"
        try:
            r = con.execute(f"""
                SELECT aadt, ST_Y({geom}) AS lat, ST_X({geom}) AS lng
                FROM read_parquet('{path}')
                WHERE aadt > 0 AND ({bb.replace('bbox.xmin', f'ST_X({geom})').replace('bbox.ymin', f'ST_Y({geom})')})
                USING SAMPLE {n // 5}
            """).fetchall()
            rows += [(la, lo, a) for a, la, lo in r if a and a > 0]
        except Exception as e:
            print(f"  {f}: {str(e)[:60]}")
    return rows


def main():
    con = duckdb.connect(); con.execute("INSTALL spatial; LOAD spatial; SET preserve_insertion_order=false;")
    t0 = time.time()
    au_pts = load_au_points()
    bb = " OR ".join(
        f"(bbox.xmin BETWEEN {min(lo)-0.05} AND {max(lo)+0.05} AND bbox.ymin BETWEEN {min(la)-0.05} AND {max(la)+0.05})"
        for c in AU_CITIES
        for la, lo in [([p[1] for p in au_pts if p[0] == c], [p[2] for p in au_pts if p[0] == c])] if la)

    con.execute("CREATE TABLE poi AS SELECT lng,lat FROM read_parquet('data/eu/poi.parquet') ORDER BY lng,lat")
    con.execute(f"CREATE TABLE aur AS SELECT class,geometry,bbox.xmin AS xmin,bbox.ymin AS ymin FROM read_parquet('data/overture_roads.parquet') WHERE class IN ('motorway','trunk','primary','secondary','tertiary','residential','service','unclassified','living_street') AND ({bb})")
    con.execute(f"CREATE TABLE aub AS SELECT COALESCE(height,6.0) AS h, ST_X(ST_Centroid(geometry)) AS clng, ST_Y(ST_Centroid(geometry)) AS clat FROM read_parquet('data/overture_buildings.parquet') WHERE ({bb})")
    print(f"loaded ({time.time()-t0:.0f}s)", flush=True)

    aadt_pts = sample_au_aadt(con, bb)
    print(f"AU AADT training points (in city regions): {len(aadt_pts)}", flush=True)
    Xa, ya = [], []
    for i, (la, lo, a) in enumerate(aadt_pts):
        try:
            f = feats(con, "aur", "aub", "poi", la, lo)
            Xa.append([f[k] for k in KEYS]); ya.append(math.log(a))
        except Exception:
            pass
        if (i + 1) % 1500 == 0: print(f"  aadt feat {i+1}/{len(aadt_pts)} ({time.time()-t0:.0f}s)", flush=True)
    Xa = np.array(Xa); ya = np.array(ya)
    imp = RandomForestRegressor(n_estimators=300, min_samples_leaf=5, max_features="sqrt", n_jobs=-1, random_state=42)
    imp.fit(Xa, ya)
    print(f"AADT imputer trained on {len(ya)} pts (in-sample R2={imp.score(Xa,ya):.2f})", flush=True)

    # load cached v5 noise features, append imputed_aadt
    d = np.load("data/eu/transfer5_cache.npz", allow_pickle=True)
    Xnl, ynl, Xau, yau, cau = d["Xnl"], d["ynl"], d["Xau"], d["yau"], np.array(d["cau"])
    imp_nl = imp.predict(Xnl).reshape(-1, 1)
    imp_au = imp.predict(Xau).reshape(-1, 1)
    Xnl2 = np.hstack([Xnl, imp_nl]); Xau2 = np.hstack([Xau, imp_au])

    def st(p, t): return mean_absolute_error(t, p), np.mean(p - t), (np.corrcoef(p, t)[0, 1] if p.std() > 0 else 0)

    def run(X_tr, X_te, label):
        rf = RandomForestRegressor(n_estimators=500, min_samples_leaf=3, max_features="sqrt", n_jobs=-1, random_state=42)
        rf.fit(X_tr, ynl); raw = rf.predict(X_te)
        cal = np.zeros(len(yau))
        for h in AU_CITIES:
            te = cau == h; tr = cau != h
            if te.sum() < 5: continue
            lin = LinearRegression().fit(raw[tr].reshape(-1, 1), yau[tr]); cal[te] = lin.predict(raw[te].reshape(-1, 1))
        per = {h: round(np.corrcoef(cal[cau == h], yau[cau == h])[0, 1], 2) for h in AU_CITIES if (cau == h).sum() > 5}
        a = st(cal, yau)
        print(f"\n{label}: pooled MAE={a[0]:.1f} bias={a[1]:+.1f} r={a[2]:.2f}", flush=True)
        print("  per-city r:", per, flush=True)
        return a

    print("\n" + "=" * 60, flush=True)
    run(Xnl, Xau, "v5 baseline (no AADT feature)")
    run(Xnl2, Xau2, "v6 + imputed_aadt feature")


if __name__ == "__main__":
    raise SystemExit(main())
