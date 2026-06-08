"""POC v2: EU->AU transfer with ROAD-CLASS + BUILDING features + stratified NL data.
Extends poc_eu_transfer.py (road-only). Tests whether richer features lift the
transfer from r~0.45 toward SOTA.
"""
import csv
import math
import os
import re
import sys
import time

import duckdb
import numpy as np
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error

CLASSES = ["motorway", "trunk", "primary", "secondary", "tertiary", "residential", "service", "unclassified"]
RINGS = [50, 100, 200, 400, 800]
AU_CITIES = ["melbourne", "sydney", "adelaide", "perth", "hobart", "canberra", "darwin"]


def lden(d, e, n):
    return 10 * math.log10((12 * 10 ** (d / 10) + 4 * 10 ** ((e + 5) / 10) + 8 * 10 ** ((n + 10) / 10)) / 24) if max(d, e, n) > 0 else 0.0


def fkeys():
    ks = []
    for c in CLASSES:
        ks += [f"{c}_invd", f"{c}_near"] + [f"{c}_n{r}" for r in RINGS]
    ks += ["nearest_major", "n_roads_200", "n_roads_500",
           "bldg_n100", "bldg_n200", "bldg_h_mean100", "bldg_h_max200", "canyon"]
    return sorted(ks)


def feats(con, roads_t, bldg_t, lat, lng):
    deg = 0.013
    mpd = 111_320 * math.cos(math.radians(lat))
    rows = con.execute(f"""
        SELECT class, ST_Distance(geometry, ST_Point({lng},{lat}))*{mpd} AS d FROM {roads_t}
        WHERE xmin BETWEEN {lng-deg} AND {lng+deg} AND ymin BETWEEN {lat-deg} AND {lat+deg}
          AND ST_Distance(geometry, ST_Point({lng},{lat})) < {1000/mpd}
    """).fetchall()
    f = {}
    for c in CLASSES:
        ds = [d for cls, d in rows if cls == c]
        f[f"{c}_invd"] = sum(1.0 / max(d, 10) for d in ds)
        f[f"{c}_near"] = min(ds) if ds else 1000.0
        for r in RINGS:
            f[f"{c}_n{r}"] = sum(1 for d in ds if d <= r)
    major = ("motorway", "trunk", "primary", "secondary", "tertiary")
    nm = min((d for cls, d in rows if cls in major), default=1000.0)
    f["nearest_major"] = nm
    f["n_roads_200"] = sum(1 for cls, d in rows if d <= 200)
    f["n_roads_500"] = sum(1 for cls, d in rows if d <= 500)
    # buildings (centroid arithmetic distance)
    b = con.execute(f"""
        SELECT h, SQRT(POW((clng-({lng}))*{mpd},2)+POW((clat-({lat}))*111320,2)) AS d FROM {bldg_t}
        WHERE clng BETWEEN {lng-0.004} AND {lng+0.004} AND clat BETWEEN {lat-0.003} AND {lat+0.003}
    """).fetchall()
    h100 = [h for h, d in b if d <= 100]
    f["bldg_n100"] = len(h100)
    f["bldg_n200"] = sum(1 for h, d in b if d <= 200)
    f["bldg_h_mean100"] = float(np.mean(h100)) if h100 else 0.0
    f["bldg_h_max200"] = max((h for h, d in b if d <= 200), default=0.0)
    f["canyon"] = (f["bldg_h_mean100"] / max(nm, 5)) if h100 else 0.0
    return f


def load_au_points():
    pts = []
    for c in AU_CITIES:
        fn = f"data/ambient_sample/antn_{c}_buildings_.csv"
        if not os.path.exists(fn):
            continue
        rows = list(csv.DictReader(open(fn)))
        if len(rows) > 350:
            rows = rows[::len(rows) // 350][:350]
        for r in rows:
            m = re.search(r"POINT \(([-\d.]+) ([-\d.]+)\)", r["geometry"])
            if not m:
                continue
            la, lo = float(m.group(1)), float(m.group(2))
            t = lden(float(r["sp_rd_max_d"]), float(r["sp_rd_max_e"]), float(r["sp_rd_max_n"]))
            if t > 0:
                pts.append((c, la, lo, t))
    return pts


def main():
    con = duckdb.connect(); con.execute("INSTALL spatial; LOAD spatial; SET preserve_insertion_order=false;")
    t0 = time.time()
    au_pts = load_au_points()
    bb = " OR ".join(
        f"(bbox.xmin BETWEEN {min(lo)-0.05} AND {max(lo)+0.05} AND bbox.ymin BETWEEN {min(la)-0.05} AND {max(la)+0.05})"
        for c in AU_CITIES
        for la, lo in [([p[1] for p in au_pts if p[0] == c], [p[2] for p in au_pts if p[0] == c])] if la)
    bb2 = bb.replace("bbox.xmin", "cx").replace("bbox.ymin", "cy")
    print("loading roads+buildings (NL local, AU from cloud-local)...", flush=True)
    con.execute("CREATE TABLE nlr AS SELECT class,geometry,xmin,ymin FROM read_parquet('data/eu/nl_roads.parquet')")
    con.execute("CREATE TABLE nlb AS SELECT h,clng,clat FROM read_parquet('data/eu/nl_buildings.parquet') ORDER BY clng, clat")
    con.execute(f"CREATE TABLE aur AS SELECT class,geometry,bbox.xmin AS xmin,bbox.ymin AS ymin FROM read_parquet('data/overture_roads.parquet') WHERE class IN ('motorway','trunk','primary','secondary','tertiary','residential','service','unclassified','living_street') AND ({bb})")
    con.execute(f"CREATE TABLE aub AS SELECT COALESCE(height,6.0) AS h, ST_X(ST_Centroid(geometry)) AS clng, ST_Y(ST_Centroid(geometry)) AS clat FROM read_parquet('data/overture_buildings.parquet') WHERE ({bb.replace('bbox.','bbox.')})")
    print(f"  loaded ({time.time()-t0:.0f}s)", flush=True)

    KEYS = fkeys()
    cache = "data/eu/transfer2_cache.npz"
    if os.path.exists(cache):
        d = np.load(cache, allow_pickle=True); Xnl, ynl, Xau, yau, cau = d["Xnl"], d["ynl"], d["Xau"], d["yau"], list(d["cau"])
    else:
        nlp = list(csv.DictReader(open("data/eu/nl_train_points.csv")))
        Xnl, ynl = [], []
        for i, r in enumerate(nlp):
            f = feats(con, "nlr", "nlb", float(r["lat"]), float(r["lng"]))
            Xnl.append([f[k] for k in KEYS]); ynl.append(float(r["lden"]))
            if (i + 1) % 1000 == 0: print(f"  NL {i+1}/{len(nlp)} ({time.time()-t0:.0f}s)", flush=True)
        Xau, yau, cau = [], [], []
        for i, (c, la, lo, t) in enumerate(au_pts):
            f = feats(con, "aur", "aub", la, lo)
            Xau.append([f[k] for k in KEYS]); yau.append(t); cau.append(c)
            if (i + 1) % 500 == 0: print(f"  AU {i+1}/{len(au_pts)} ({time.time()-t0:.0f}s)", flush=True)
        Xnl, ynl, Xau, yau = map(lambda a: np.array(a, float), [Xnl, ynl, Xau, yau])
        np.savez(cache, Xnl=Xnl, ynl=ynl, Xau=Xau, yau=yau, cau=np.array(cau))
        print(f"features done ({time.time()-t0:.0f}s)", flush=True)
    cau = np.array(cau)

    def st(p, t): return mean_absolute_error(t, p), np.mean(p - t), (np.corrcoef(p, t)[0, 1] if p.std() > 0 else 0), p.std()
    rf = RandomForestRegressor(n_estimators=500, min_samples_leaf=3, max_features="sqrt", n_jobs=-1, random_state=42)
    rf.fit(Xnl, ynl)
    raw = rf.predict(Xau)
    a = st(raw, yau); print(f"\nRAW NL->AU transfer (road+bldg): MAE=%.1f bias=%+.1f r=%.2f std=%.1f (target std=%.1f)" % (a[0], a[1], a[2], a[3], yau.std()), flush=True)
    print("\nLEAVE-ONE-AU-CITY-OUT (NL-RF + affine cal):", flush=True)
    print("%-10s %4s | cal(MAE/bias/r/std)" % ("held-out", "n"), flush=True)
    cal_all = np.zeros(len(yau))
    for h in AU_CITIES:
        te = cau == h; tr = cau != h
        if te.sum() < 5: continue
        lin = LinearRegression().fit(raw[tr].reshape(-1, 1), yau[tr])
        cal_all[te] = lin.predict(raw[te].reshape(-1, 1))
        a = st(cal_all[te], yau[te]); print("%-10s %4d | %4.1f/%+5.1f/%5.2f/%4.1f" % (h, te.sum(), a[0], a[1], a[2], a[3]), flush=True)
    a = st(cal_all, yau); print("-" * 50 + f"\nPOOLED %4d | MAE=%.1f bias=%+.1f r=%.2f std=%.1f" % (len(yau), a[0], a[1], a[2], a[3]), flush=True)
    imp = sorted(zip(KEYS, rf.feature_importances_), key=lambda x: -x[1])[:12]
    print("\nTop features:", flush=True)
    for k, v in imp: print("  %-18s %.3f" % (k, v), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
