"""POC: does adding dense US road-noise (NTAD) training data lift the AU transfer
plateau (NL+UK -> AU, r ~0.70 pooled-excl-sydney)?

Pipeline:
  1. Sample US training points from NTAD State_rasters (CA/NY/IL/TX) inside the
     4 city bboxes, stratified into 5dB bands, ~2000/city. LAeq24h -> Lden via +3dB.
  2. Compute the same 75 geometry features as poc_eu_transfer5.feats() using
     us_roads/us_buildings (schema == EU). POI absent for US -> the 3 POI features
     are left at 0 (POI is redundant for the model). DEM/LC from data/global vrts.
  3. Reuse the cached NL+UK features (Xnl/ynl) and AU eval set (Xau/yau/cau) from
     transfer5_cache.npz. Baseline: RF.fit(Xnl) ; +US: RF.fit(Xnl + US).
  4. Evaluate both on AU with the SAME protocol: clean sentinel yau>=30, in-city
     5-fold affine calibration, per-city + pooled (incl/excl sydney) MAE/r.

Does NOT modify existing code; imports feats()/fkeys() from poc_eu_transfer5.
US point features are cached to data/us/us_train_cache.npz so reruns are fast.
"""
import os
import sys
import time

import duckdb
import numpy as np
import rasterio
from rasterio.warp import transform as rio_transform
from rasterio.windows import from_bounds
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error
from sklearn.model_selection import KFold

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.poc_eu_transfer5 import feats, fkeys  # noqa: E402

# (name, state_raster_code, center_lat, center_lng)
US_CITIES = [
    ("los_angeles", "CA", 34.05, -118.25),
    ("new_york", "NY", 40.71, -74.01),
    ("chicago", "IL", 41.88, -87.63),
    ("houston", "TX", 29.76, -95.37),
]
BBOX_DEG = 0.2
BANDS = [(45, 50), (50, 55), (55, 60), (60, 65), (65, 70), (70, 75), (75, 80), (80, 85)]
PER_CITY = 2000
LDEN_OFFSET = 3.0  # LAeq24h -> Lden approximation (Lden ~ +2-4 dB)
TRANSFER_CACHE = "data/eu/transfer5_cache.npz"
US_CACHE = "data/us/us_train_cache.npz"
SENTINEL = 30.0  # clean AU sentinel: drop yau < 30
RNG = np.random.default_rng(42)


def sample_us_points():
    """Stratified sample of (city, lat, lng, lden) over the 4 US city bboxes."""
    pts = []
    band_report = {}
    for name, st, clat, clng in US_CITIES:
        p = f"data/us/CONUS_road_noise_2020/State_rasters/{st}_road_noise_2020.tif"
        with rasterio.open(p) as ds:
            la0, la1 = clat - BBOX_DEG, clat + BBOX_DEG
            lo0, lo1 = clng - BBOX_DEG, clng + BBOX_DEG
            corner_lats = [la0, la0, la1, la1]
            corner_lngs = [lo0, lo1, lo0, lo1]
            xs, ys = rio_transform("EPSG:4326", ds.crs, corner_lngs, corner_lats)
            win = from_bounds(min(xs), min(ys), max(xs), max(ys), ds.transform)
            arr = ds.read(1, window=win)
            wt = rasterio.windows.transform(win, ds.transform)
            valid = (arr >= 45) & (arr <= 85)
            per_band = max(1, PER_CITY // len(BANDS))
            city_counts = {}
            for lo_db, hi_db in BANDS:
                mask = valid & (arr >= lo_db) & (arr < hi_db) if hi_db < 85 else valid & (arr >= lo_db) & (arr <= hi_db)
                rr, cc = np.where(mask)
                if rr.size == 0:
                    city_counts[f"{lo_db}-{hi_db}"] = 0
                    continue
                take = min(per_band, rr.size)
                idx = RNG.choice(rr.size, size=take, replace=False)
                rr, cc = rr[idx], cc[idx]
                # pixel center -> albers xy (window-relative row/col)
                axs, ays = rasterio.transform.xy(wt, rr, cc)
                axs = np.asarray(axs)
                ays = np.asarray(ays)
                # albers -> lat/lng (rasterio.warp.transform, no pyproj)
                lngs, lats = rio_transform(ds.crs, "EPSG:4326", axs.tolist(), ays.tolist())
                vals = arr[rr, cc].astype(float)
                for la, lo, v in zip(lats, lngs, vals):
                    pts.append((name, float(la), float(lo), float(v) + LDEN_OFFSET))
                city_counts[f"{lo_db}-{hi_db}"] = take
            band_report[name] = city_counts
    return pts, band_report


def compute_us_features(us_pts):
    """75-dim geometry features for US points using us_roads/us_buildings, POI=0."""
    keys = fkeys()
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial; SET preserve_insertion_order=false;")
    # bbox union for road/building prefilter (US Albers parquet already in CRS84)
    clauses = []
    for name, st, clat, clng in US_CITIES:
        clauses.append(
            f"(xmin BETWEEN {clng-BBOX_DEG-0.02} AND {clng+BBOX_DEG+0.02} "
            f"AND ymin BETWEEN {clat-BBOX_DEG-0.02} AND {clat+BBOX_DEG+0.02})"
        )
    bb = " OR ".join(clauses)
    bb_b = " OR ".join(
        f"(clng BETWEEN {clng-BBOX_DEG-0.02} AND {clng+BBOX_DEG+0.02} "
        f"AND clat BETWEEN {clat-BBOX_DEG-0.02} AND {clat+BBOX_DEG+0.02})"
        for _, _, clat, clng in US_CITIES
    )
    con.execute(
        f"CREATE TABLE rr AS SELECT class,geometry,xmin,ymin FROM "
        f"read_parquet('data/us/us_roads.parquet') WHERE {bb}"
    )
    con.execute(
        f"CREATE TABLE bb_t AS SELECT h,clng,clat FROM "
        f"read_parquet('data/us/us_buildings.parquet') WHERE {bb_b} ORDER BY clng,clat"
    )
    # empty POI table (US not in poi.parquet); feats() returns 0 for poi_* features
    con.execute("CREATE TABLE poi(lng DOUBLE, lat DOUBLE)")
    n_roads = con.execute("SELECT count(*) FROM rr").fetchone()[0]
    n_bldg = con.execute("SELECT count(*) FROM bb_t").fetchone()[0]
    print(f"  US roads in bbox: {n_roads}  buildings: {n_bldg}", flush=True)

    X, y, cities = [], [], []
    t0 = time.time()
    for j, (name, la, lo, tgt) in enumerate(us_pts):
        f = feats(con, "rr", "bb_t", "poi", la, lo)
        X.append([f[k] for k in keys])
        y.append(tgt)
        cities.append(name)
        if (j + 1) % 1000 == 0:
            print(f"    {j+1}/{len(us_pts)} ({time.time()-t0:.0f}s)", flush=True)
    return np.array(X, float), np.array(y, float), np.array(cities)


def stats(pred, true):
    return (
        mean_absolute_error(true, pred),
        float(np.mean(pred - true)),
        float(np.corrcoef(pred, true)[0, 1]) if pred.std() > 0 else 0.0,
        float(pred.std()),
    )


def evaluate(rf, Xau, yau, cau, label):
    """Clean sentinel, in-city 5-fold affine calibration, per-city + pooled."""
    keep = yau >= SENTINEL
    Xe, ye, ce = Xau[keep], yau[keep], cau[keep]
    raw = rf.predict(Xe)
    cal = np.zeros(len(ye))
    print(f"\n=== {label} ===", flush=True)
    print(f"  (kept {keep.sum()}/{len(yau)} AU points after sentinel yau>={SENTINEL:.0f})", flush=True)
    rows = {}
    for city in sorted(set(ce.tolist())):
        m = ce == city
        n = int(m.sum())
        if n < 5:
            cal[m] = raw[m]
            continue
        rc, yc = raw[m], ye[m]
        cc = np.zeros(n)
        if n >= 5:
            kf = KFold(n_splits=5, shuffle=True, random_state=42)
            for tr, te in kf.split(rc):
                lin = LinearRegression().fit(rc[tr].reshape(-1, 1), yc[tr])
                cc[te] = lin.predict(rc[te].reshape(-1, 1))
        cal[m] = cc
        s = stats(cc, yc)
        rows[city] = (n, s)
        print("  %-12s %4d | MAE=%.2f bias=%+.2f r=%.2f std=%.1f" % (city, n, s[0], s[1], s[2], s[3]), flush=True)
    s_all = stats(cal, ye)
    excl = ce != "sydney"
    s_excl = stats(cal[excl], ye[excl]) if excl.sum() > 1 else (0, 0, 0, 0)
    print("  %-12s %4d | MAE=%.2f bias=%+.2f r=%.2f std=%.1f" % ("POOLED(all)", len(ye), s_all[0], s_all[1], s_all[2], s_all[3]), flush=True)
    print("  %-12s %4d | MAE=%.2f bias=%+.2f r=%.2f std=%.1f" % ("POOLED(-syd)", int(excl.sum()), s_excl[0], s_excl[1], s_excl[2], s_excl[3]), flush=True)
    return {"per_city": rows, "pooled_all": s_all, "pooled_excl": s_excl, "n_all": len(ye), "n_excl": int(excl.sum())}


def main():
    t0 = time.time()
    d = np.load(TRANSFER_CACHE, allow_pickle=True)
    Xnl, ynl = d["Xnl"], d["ynl"]
    Xau, yau, cau = d["Xau"], d["yau"], np.array(d["cau"])
    print(f"Loaded transfer cache: Xnl={Xnl.shape} Xau={Xau.shape} cities={sorted(set(cau.tolist()))}", flush=True)

    # --- US sampling ---
    if os.path.exists(US_CACHE):
        u = np.load(US_CACHE, allow_pickle=True)
        Xus, yus, cus = u["Xus"], u["yus"], np.array(u["cus"])
        band_report = u["band_report"].item() if "band_report" in u else {}
        print(f"Loaded US cache: Xus={Xus.shape}", flush=True)
    else:
        us_pts, band_report = sample_us_points()
        print(f"\nSampled {len(us_pts)} US points across {len(US_CITIES)} cities:", flush=True)
        for name in band_report:
            print(f"  {name}: {band_report[name]}", flush=True)
        Xus, yus, cus = compute_us_features(us_pts)
        np.savez(US_CACHE, Xus=Xus, yus=yus, cus=cus, band_report=np.array(band_report, dtype=object))
        print(f"US features done ({time.time()-t0:.0f}s) -> {US_CACHE}", flush=True)

    # DEM/LC coverage on US points: elev feature (key 'elev') nonzero fraction
    keys = fkeys()
    ei = keys.index("elev")
    lci = keys.index("lc_built_300")
    print("\n--- US DEM/LC coverage ---", flush=True)
    for name in sorted(set(cus.tolist())):
        m = cus == name
        dem_cov = float(np.mean(Xus[m, ei] != 0.0))
        # lc fractions sum > 0 => landcover sampled
        lc_keys = [k for k in keys if k.startswith("lc_")]
        lc_idx = [keys.index(k) for k in lc_keys]
        lc_cov = float(np.mean(Xus[m][:, lc_idx].sum(axis=1) > 0))
        print(f"  {name:12s} n={int(m.sum()):4d}  DEM!=0: {dem_cov*100:.1f}%  LC>0: {lc_cov*100:.1f}%", flush=True)
    print(f"  yus range: {yus.min():.1f}-{yus.max():.1f} mean {yus.mean():.1f}", flush=True)

    # --- Train + evaluate ---
    def make_rf():
        return RandomForestRegressor(
            n_estimators=400, min_samples_leaf=3, max_features="sqrt",
            n_jobs=-1, random_state=42,
        )

    rf_base = make_rf()
    rf_base.fit(Xnl, ynl)
    base = evaluate(rf_base, Xau, yau, cau, "BASELINE: RF.fit(NL+UK) -> AU")

    Xcomb = np.vstack([Xnl, Xus])
    ycomb = np.concatenate([ynl, yus])
    rf_us = make_rf()
    rf_us.fit(Xcomb, ycomb)
    plus = evaluate(rf_us, Xau, yau, cau, "+US: RF.fit(NL+UK+US) -> AU")

    # --- Side-by-side ---
    print("\n\n================= SUMMARY: BASELINE vs +US =================", flush=True)
    print(f"{'city':12s} | {'baseline MAE/r':>16s} | {'+US MAE/r':>16s} | {'dMAE':>6s} {'dr':>6s}", flush=True)
    cities = sorted(set(cau[yau >= SENTINEL].tolist()))
    for city in cities:
        if city in base["per_city"] and city in plus["per_city"]:
            bn, bs = base["per_city"][city]
            pn, ps = plus["per_city"][city]
            print("%-12s | MAE=%.2f r=%.2f%s | MAE=%.2f r=%.2f%s | %+.2f %+.2f" % (
                city, bs[0], bs[2], " " * 5, ps[0], ps[2], " " * 5,
                ps[0] - bs[0], ps[2] - bs[2]), flush=True)
    for label, key in [("POOLED(all)", "pooled_all"), ("POOLED(-syd)", "pooled_excl")]:
        bs, ps = base[key], plus[key]
        print("%-12s | MAE=%.2f r=%.2f%s | MAE=%.2f r=%.2f%s | %+.2f %+.2f" % (
            label, bs[0], bs[2], " " * 5, ps[0], ps[2], " " * 5,
            ps[0] - bs[0], ps[2] - bs[2]), flush=True)
    print("===========================================================", flush=True)
    print(f"\nTotal runtime: {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
