"""POC: train a road-class geodata model on NETHERLANDS official noise (RIVM Lden,
public domain) and TRANSFER + CALIBRATE to Australia (SoundPLAN sample).

Tests the core hypothesis: foreign free dense noise data + globally-consistent
OSM/Overture road-class features, calibrated on our sparse AU sample, beats our
AU-only models (current r~0.2-0.5).

Unified feature set (identical NL & AU, from Overture road class only for this
first slice): per class inverse-distance + nearest-distance + counts in
50/100/200/400/800 m rings, nearest-major, road density.

Validation: train on NL (4000 pts, RIVM Lden) -> predict AU -> affine calibrate
on AU -> leave-one-AU-CITY-out CV vs SoundPLAN road Lden.
"""
import csv
import glob
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

CLASSES = ["motorway", "trunk", "primary", "secondary", "tertiary",
           "residential", "service", "unclassified"]
RINGS = [50, 100, 200, 400, 800]
AU_CITIES = ["melbourne", "sydney", "adelaide", "perth", "hobart", "canberra", "darwin"]


def lden(d, e, n):
    return 10 * math.log10((12 * 10 ** (d / 10) + 4 * 10 ** ((e + 5) / 10) + 8 * 10 ** ((n + 10) / 10)) / 24) if max(d, e, n) > 0 else 0.0


def feature_keys():
    ks = []
    for c in CLASSES:
        ks += [f"{c}_invd", f"{c}_near"] + [f"{c}_n{r}" for r in RINGS]
    ks += ["nearest_major", "n_roads_200", "n_roads_500"]
    return sorted(ks)


def feats_for(con, table, lat, lng):
    deg = 0.013
    m_per_deg = 111_320 * math.cos(math.radians(lat))
    rows = con.execute(f"""
        SELECT class, ST_Distance(geometry, ST_Point({lng},{lat})) * {m_per_deg} AS d
        FROM {table}
        WHERE xmin BETWEEN {lng-deg} AND {lng+deg} AND ymin BETWEEN {lat-deg} AND {lat+deg}
          AND ST_Distance(geometry, ST_Point({lng},{lat})) < {1000/m_per_deg}
    """).fetchall()
    f = {}
    for c in CLASSES:
        ds = [d for cls, d in rows if cls == c]
        f[f"{c}_invd"] = sum(1.0 / max(d, 10) for d in ds)
        f[f"{c}_near"] = min(ds) if ds else 1000.0
        for r in RINGS:
            f[f"{c}_n{r}"] = sum(1 for d in ds if d <= r)
    major = ("motorway", "trunk", "primary", "secondary", "tertiary")
    f["nearest_major"] = min((d for cls, d in rows if cls in major), default=1000.0)
    f["n_roads_200"] = sum(1 for cls, d in rows if d <= 200)
    f["n_roads_500"] = sum(1 for cls, d in rows if d <= 500)
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
            lat, lng = float(m.group(1)), float(m.group(2))
            t = lden(float(r["sp_rd_max_d"]), float(r["sp_rd_max_e"]), float(r["sp_rd_max_n"]))
            if t > 0:
                pts.append((c, lat, lng, t))
    return pts


def main():
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial; SET preserve_insertion_order=false;")
    t0 = time.time()
    print("loading NL roads into memory...", flush=True)
    con.execute("CREATE TABLE nl AS SELECT class, geometry, xmin, ymin FROM read_parquet('data/eu/nl_roads.parquet')")

    # AU city bboxes from the sample points
    au_pts = load_au_points()
    la = [p[1] for p in au_pts]; lo = [p[2] for p in au_pts]
    bb = " OR ".join(
        f"(bbox.xmin BETWEEN {min(lo2)-0.05} AND {max(lo2)+0.05} AND bbox.ymin BETWEEN {min(la2)-0.05} AND {max(la2)+0.05})"
        for c in AU_CITIES
        for la2, lo2 in [([p[1] for p in au_pts if p[0] == c], [p[2] for p in au_pts if p[0] == c])]
        if la2
    )
    print("loading AU city roads into memory...", flush=True)
    con.execute(f"""CREATE TABLE au AS SELECT class, geometry, bbox.xmin AS xmin, bbox.ymin AS ymin
                    FROM read_parquet('data/overture_roads.parquet')
                    WHERE class IN ('motorway','trunk','primary','secondary','tertiary','residential','service','unclassified','living_street')
                      AND ({bb})""")
    print(f"  NL={con.execute('SELECT count(*) FROM nl').fetchone()[0]} AU={con.execute('SELECT count(*) FROM au').fetchone()[0]} ({time.time()-t0:.0f}s)", flush=True)

    KEYS = feature_keys()
    cache = "data/eu/transfer_cache.npz"
    if os.path.exists(cache):
        d = np.load(cache, allow_pickle=True)
        Xnl, ynl = d["Xnl"], d["ynl"]
        Xau, yau, cau = d["Xau"], d["yau"], list(d["cau"])
        print("loaded cache", flush=True)
    else:
        # NL training
        nlp = list(csv.DictReader(open("data/eu/nl_train_points.csv")))
        Xnl, ynl = [], []
        for i, r in enumerate(nlp):
            f = feats_for(con, "nl", float(r["lat"]), float(r["lng"]))
            Xnl.append([f[k] for k in KEYS]); ynl.append(float(r["lden"]))
            if (i + 1) % 500 == 0:
                print(f"  NL {i+1}/{len(nlp)} ({time.time()-t0:.0f}s)", flush=True)
        # AU
        Xau, yau, cau = [], [], []
        for i, (c, lat, lng, t) in enumerate(au_pts):
            f = feats_for(con, "au", lat, lng)
            Xau.append([f[k] for k in KEYS]); yau.append(t); cau.append(c)
            if (i + 1) % 500 == 0:
                print(f"  AU {i+1}/{len(au_pts)} ({time.time()-t0:.0f}s)", flush=True)
        Xnl, ynl, Xau, yau = map(lambda a: np.array(a, float), [Xnl, ynl, Xau, yau])
        np.savez(cache, Xnl=Xnl, ynl=ynl, Xau=Xau, yau=yau, cau=np.array(cau))
        print(f"features done ({time.time()-t0:.0f}s)", flush=True)

    cau = np.array(cau)

    def st(p, t):
        return mean_absolute_error(t, p), np.mean(p - t), (np.corrcoef(p, t)[0, 1] if p.std() > 0 else 0), p.std()

    # Train RF on ALL of NL
    rf = RandomForestRegressor(n_estimators=400, min_samples_leaf=3, max_features="sqrt", n_jobs=-1, random_state=42)
    rf.fit(Xnl, ynl)
    au_raw = rf.predict(Xau)
    print(f"\nNL-trained RF applied to AU (RAW transfer, no calibration):", flush=True)
    a = st(au_raw, yau); print("  MAE=%.1f bias=%+.1f r=%.2f std=%.1f (target std=%.1f)" % (a[0], a[1], a[2], a[3], yau.std()), flush=True)

    # Leave-one-AU-city-out: calibrate affine on the OTHER AU cities, apply to held-out
    print("\nLEAVE-ONE-AU-CITY-OUT: NL-RF + affine calibration on other AU cities", flush=True)
    print("%-10s %4s | EU-transfer+cal(MAE/bias/r) | raw-EU(MAE/r)" % ("held-out", "n"), flush=True)
    cal_all = np.zeros(len(yau))
    for h in AU_CITIES:
        te = cau == h; tr = cau != h
        if te.sum() < 5 or tr.sum() < 30:
            continue
        lin = LinearRegression().fit(au_raw[tr].reshape(-1, 1), yau[tr])
        cal = lin.predict(au_raw[te].reshape(-1, 1)); cal_all[te] = cal
        a = st(cal, yau[te]); b = st(au_raw[te], yau[te])
        print("%-10s %4d | %4.1f/%+5.1f/%5.2f | %4.1f/%5.2f" % (h, te.sum(), a[0], a[1], a[2], b[0], b[2]), flush=True)
    print("-" * 60, flush=True)
    a = st(cal_all, yau); print("%-10s %4d | %4.1f/%+5.1f/%5.2f | pooled" % ("POOLED", len(yau), a[0], a[1], a[2]), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
