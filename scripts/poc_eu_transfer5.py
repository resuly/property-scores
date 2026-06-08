"""POC v3: EU->AU transfer with road + buildings + DEM + landcover + POI features.
Extends poc_eu_transfer2.py with the global Staab feature layers to push r up and
fight compression.
"""
import csv
import math
import os
import re
import time

import duckdb
import numpy as np
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from property_scores.noise import raster_sample as rs  # noqa: E402

CLASSES = ["motorway", "trunk", "primary", "secondary", "tertiary", "residential", "service", "unclassified"]
RINGS = [50, 100, 200, 400, 800]
AU_CITIES = ["melbourne", "sydney", "adelaide", "perth", "hobart", "canberra", "darwin"]
DEM = "data/global/dem.vrt"
LC = "data/global/lc.vrt"
LC_CLASSES = {10: "tree", 30: "grass", 40: "crop", 50: "built", 80: "water"}


def lden(d, e, n):
    return 10 * math.log10((12 * 10 ** (d / 10) + 4 * 10 ** ((e + 5) / 10) + 8 * 10 ** ((n + 10) / 10)) / 24) if max(d, e, n) > 0 else 0.0


def fkeys():
    ks = []
    for c in CLASSES:
        ks += [f"{c}_invd", f"{c}_near"] + [f"{c}_n{r}" for r in RINGS]
    ks += ["nearest_major", "n_roads_200", "n_roads_500",
           "bldg_n100", "bldg_n200", "bldg_h_mean100", "bldg_h_max200", "canyon",
           "elev", "elev_range300", "poi_n100", "poi_n300", "poi_n500"]
    ks += [f"lc_{n}_300" for n in LC_CLASSES.values()] + ["lc_built_100"]
    return sorted(ks)


def feats(con, roads_t, bldg_t, poi_t, lat, lng):
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
    b = con.execute(f"""
        SELECT h, SQRT(POW((clng-({lng}))*{mpd},2)+POW((clat-({lat}))*111320,2)) AS d FROM {bldg_t}
        WHERE clng BETWEEN {lng-0.004} AND {lng+0.004} AND clat BETWEEN {lat-0.003} AND {lat+0.003}
    """).fetchall()
    h100 = [h for h, d in b if d <= 100]
    f["bldg_n100"] = len(h100); f["bldg_n200"] = sum(1 for h, d in b if d <= 200)
    f["bldg_h_mean100"] = float(np.mean(h100)) if h100 else 0.0
    f["bldg_h_max200"] = max((h for h, d in b if d <= 200), default=0.0)
    f["canyon"] = (f["bldg_h_mean100"] / max(nm, 5)) if h100 else 0.0
    # POI density
    p = con.execute(f"""
        SELECT SQRT(POW((lng-({lng}))*{mpd},2)+POW((lat-({lat}))*111320,2)) AS d FROM {poi_t}
        WHERE lng BETWEEN {lng-0.006} AND {lng+0.006} AND lat BETWEEN {lat-0.0045} AND {lat+0.0045}
    """).fetchall()
    pd = [r[0] for r in p]
    f["poi_n100"] = sum(1 for d in pd if d <= 100); f["poi_n300"] = sum(1 for d in pd if d <= 300); f["poi_n500"] = sum(1 for d in pd if d <= 500)
    # DEM
    elev = rs.sample(DEM, lat, lng, default=0.0)
    f["elev"] = elev if not math.isnan(elev) else 0.0
    er = rs.window_stats(DEM, lat, lng, 300)
    f["elev_range300"] = (er.get("max", 0) - (er.get("mean", 0) - (er.get("max", 0) - er.get("mean", 0)))) if er else 0.0
    f["elev_range300"] = (er.get("max", 0) - er.get("mean", 0)) * 2 if er else 0.0
    # Land cover fractions
    lc = rs.window_stats(LC, lat, lng, 300, categorical=True, classes=list(LC_CLASSES.keys()))
    for code, name in LC_CLASSES.items():
        f[f"lc_{name}_300"] = lc.get(f"frac_{code}", 0.0)
    lc100 = rs.window_stats(LC, lat, lng, 100, categorical=True, classes=[50])
    f["lc_built_100"] = lc100.get("frac_50", 0.0)
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
    con.execute("CREATE TABLE poi AS SELECT lng,lat FROM read_parquet('data/eu/poi.parquet') ORDER BY lng,lat")  # small, keep loaded
    print(f"  POI loaded ({time.time()-t0:.0f}s)", flush=True)

    KEYS = fkeys()
    cache = "data/eu/transfer5_cache.npz"
    if os.path.exists(cache):
        d = np.load(cache, allow_pickle=True); Xnl, ynl, Xau, yau, cau = d["Xnl"], d["ynl"], d["Xau"], d["yau"], list(d["cau"])
    else:
        def compute(country_pts, roads_sql, bldg_sql):
            # MEMORY-SAFE: load this country's roads+buildings, compute, then DROP
            con.execute("DROP TABLE IF EXISTS rr"); con.execute("DROP TABLE IF EXISTS bb_t")
            con.execute(f"CREATE TABLE rr AS {roads_sql}")
            con.execute(f"CREATE TABLE bb_t AS {bldg_sql}")
            out=[]
            for j,(la,lo,tgt) in enumerate(country_pts):
                f=feats(con,"rr","bb_t","poi",la,lo); out.append(([f[k] for k in KEYS],tgt))
                if (j+1)%2000==0: print(f"    {j+1}/{len(country_pts)} ({time.time()-t0:.0f}s)",flush=True)
            con.execute("DROP TABLE rr"); con.execute("DROP TABLE bb_t")
            return out
        nl=[(float(r["lat"]),float(r["lng"]),float(r["lden"])) for r in csv.DictReader(open("data/eu/nl_train_points.csv"))]
        uk=[(float(r["lat"]),float(r["lng"]),float(r["lden"])) for r in csv.DictReader(open("data/uk/uk_train_points.csv"))]
        print("  NL...",flush=True); nlo=compute(nl,"SELECT class,geometry,xmin,ymin FROM read_parquet('data/eu/nl_roads.parquet')","SELECT h,clng,clat FROM read_parquet('data/eu/nl_buildings.parquet') ORDER BY clng,clat")
        print("  UK...",flush=True); uko=compute(uk,"SELECT class,geometry,xmin,ymin FROM read_parquet('data/uk/uk_roads.parquet')","SELECT h,clng,clat FROM read_parquet('data/uk/uk_buildings.parquet') ORDER BY clng,clat")
        Xnl=[x for x,_ in nlo+uko]; ynl=[t for _,t in nlo+uko]
        print("  AU...",flush=True)
        au3=[(la,lo,t) for (c,la,lo,t) in au_pts]
        auo=compute(au3,f"SELECT class,geometry,bbox.xmin AS xmin,bbox.ymin AS ymin FROM read_parquet('data/overture_roads.parquet') WHERE class IN ('motorway','trunk','primary','secondary','tertiary','residential','service','unclassified','living_street') AND ({bb})",f"SELECT COALESCE(height,6.0) AS h, ST_X(ST_Centroid(geometry)) AS clng, ST_Y(ST_Centroid(geometry)) AS clat FROM read_parquet('data/overture_buildings.parquet') WHERE ({bb})")
        Xau=[x for x,_ in auo]; yau=[t for _,t in auo]; cau=[c for (c,la,lo,t) in au_pts]
        Xnl, ynl, Xau, yau = map(lambda a: np.array(a, float), [Xnl, ynl, Xau, yau])
        np.savez(cache, Xnl=Xnl, ynl=ynl, Xau=Xau, yau=yau, cau=np.array(cau))
        print(f"features done ({time.time()-t0:.0f}s)", flush=True)
    cau = np.array(cau)

    def st(p, t): return mean_absolute_error(t, p), np.mean(p - t), (np.corrcoef(p, t)[0, 1] if p.std() > 0 else 0), p.std()
    rf = RandomForestRegressor(n_estimators=500, min_samples_leaf=3, max_features="sqrt", n_jobs=-1, random_state=42)
    rf.fit(Xnl, ynl)
    raw = rf.predict(Xau)
    a = st(raw, yau); print(f"\nRAW EU(NL+UK)->AU (road+bldg+DEM+LC+POI): MAE=%.1f bias=%+.1f r=%.2f std=%.1f (tgt %.1f)" % (a[0], a[1], a[2], a[3], yau.std()), flush=True)
    print("\nLEAVE-ONE-AU-CITY-OUT (+ affine cal):", flush=True)
    cal_all = np.zeros(len(yau))
    for h in AU_CITIES:
        te = cau == h; tr = cau != h
        if te.sum() < 5: continue
        lin = LinearRegression().fit(raw[tr].reshape(-1, 1), yau[tr]); cal_all[te] = lin.predict(raw[te].reshape(-1, 1))
        a = st(cal_all[te], yau[te]); print("  %-10s %4d | MAE=%.1f bias=%+.1f r=%.2f std=%.1f" % (h, te.sum(), a[0], a[1], a[2], a[3]), flush=True)
    a = st(cal_all, yau); print("  POOLED %4d | MAE=%.1f bias=%+.1f r=%.2f std=%.1f" % (len(yau), a[0], a[1], a[2], a[3]), flush=True)
    imp = sorted(zip(KEYS, rf.feature_importances_), key=lambda x: -x[1])[:14]
    print("\nTop features:", flush=True)
    for k, v in imp: print("  %-18s %.3f" % (k, v), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
