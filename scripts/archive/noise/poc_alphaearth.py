"""POC: AlphaEarth (Google Satellite Embedding V1) for AU road-noise lden.

Question: can the 64-dim annual satellite embedding break the r~0.70 plateau that
geometry-feature EU->AU transfer hits?

Baseline to beat (geometry features, EU->AU transfer + in-city affine cal):
    pooled (excl. sydney) r = 0.70 / MAE = 3.8

Pipeline:
  1. Reconstruct EU (NL+UK) train points + AU points (SAME order as transfer5_cache,
     so embedding rows align index-for-index with that cache's Xnl/Xau/yau/cau).
  2. Sample 2024 AlphaEarth embedding (64 bands) at every point via col.mosaic()
     .sampleRegions(scale=10), batched getInfo (~1000 pts/batch). Cache to npz.
  3. Train head (RandomForest + Ridge) on EU embeddings -> lden, predict AU,
     in-city 5-fold affine calibration, report per-city + pooled (with/without sydney).
  4. Optional concat: embedding + geometry features (transfer5_cache, aligned by index).

Run:  .venv/bin/python scripts/poc_alphaearth.py
"""
import csv
import math
import os
import re
import sys
import time
import warnings

import numpy as np

warnings.filterwarnings("ignore")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

AU_CITIES = ["melbourne", "sydney", "adelaide", "perth", "hobart", "canberra", "darwin"]
EMB_BANDS = [f"A{i:02d}" for i in range(64)]
CACHE = "data/alphaearth_emb_cache.npz"
GEOM_CACHE = "data/eu/transfer5_cache.npz"
CLEAN_SENTINEL = 30.0  # filter lden < 30 (silence sentinel) on AU


def lden(d, e, n):
    return 10 * math.log10((12 * 10 ** (d / 10) + 4 * 10 ** ((e + 5) / 10) + 8 * 10 ** ((n + 10) / 10)) / 24) if max(d, e, n) > 0 else 0.0


def load_eu_points():
    """NL + UK train points. Returns list of (country, lat, lng, lden)."""
    pts = []
    for r in csv.DictReader(open("data/eu/nl_train_points.csv")):
        pts.append(("nl", float(r["lat"]), float(r["lng"]), float(r["lden"])))
    for r in csv.DictReader(open("data/uk/uk_train_points.csv")):
        pts.append(("uk", float(r["lat"]), float(r["lng"]), float(r["lden"])))
    return pts


def load_au_points():
    """SAME logic/order as poc_eu_transfer5.load_au_points -> aligns with transfer5_cache.
    geometry is POINT(lat lng); group(1)=lat, group(2)=lng."""
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


def sample_embeddings(pts, batch=1000):
    """pts: list of (tag, lat, lng, y). Returns emb array (N,64) with np.nan rows
    for points GEE could not sample (ocean / missing data)."""
    import ee
    ee.Initialize(project="mapdev-319002")
    col = ee.ImageCollection("GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL").filterDate("2024-01-01", "2025-01-01")
    img = col.mosaic()

    N = len(pts)
    emb = np.full((N, 64), np.nan, dtype=np.float64)
    t0 = time.time()
    for b0 in range(0, N, batch):
        b1 = min(b0 + batch, N)
        feats = []
        for idx in range(b0, b1):
            _, la, lo, _ = pts[idx]
            feats.append(ee.Feature(ee.Geometry.Point([lo, la]), {"idx": idx}))
        fc = ee.FeatureCollection(feats)
        samp = img.sampleRegions(collection=fc, scale=10, geometries=False)
        res = samp.getInfo()
        got = 0
        for f in res["features"]:
            p = f["properties"]
            idx = int(p["idx"])
            if all(k in p for k in EMB_BANDS):
                emb[idx] = [p[k] for k in EMB_BANDS]
                got += 1
        print(f"    batch {b0}:{b1}  sampled {got}/{b1 - b0}  ({time.time() - t0:.0f}s)", flush=True)
    return emb


def get_data():
    eu_pts = load_eu_points()
    au_pts = load_au_points()
    all_pts = eu_pts + au_pts
    n_eu, n_au = len(eu_pts), len(au_pts)

    if os.path.exists(CACHE):
        d = np.load(CACHE, allow_pickle=True)
        emb = d["emb"]
        # sanity: cache must match current point count/order
        if emb.shape[0] != len(all_pts):
            print(f"  cache size {emb.shape[0]} != points {len(all_pts)}, re-sampling", flush=True)
        else:
            print(f"  loaded embedding cache ({emb.shape})", flush=True)
            return all_pts, n_eu, n_au, emb
    print(f"  sampling {len(all_pts)} points from GEE ({n_eu} EU + {n_au} AU)...", flush=True)
    emb = sample_embeddings(all_pts)
    lats = np.array([p[1] for p in all_pts])
    lngs = np.array([p[2] for p in all_pts])
    tags = np.array([p[0] for p in all_pts])
    ys = np.array([p[3] for p in all_pts])
    np.savez(CACHE, emb=emb, lat=lats, lng=lngs, tag=tags, y=ys)
    print(f"  cached to {CACHE}", flush=True)
    return all_pts, n_eu, n_au, emb


def stats(p, t):
    return (
        float(np.mean(np.abs(p - t))),
        float(np.mean(p - t)),
        float(np.corrcoef(p, t)[0, 1]) if p.std() > 0 and t.std() > 0 else 0.0,
        float(p.std()),
    )


def incity_5fold_affine(raw, y, cities, city_list):
    """In-city 5-fold affine calibration (same口径 as geometry baseline).
    For each city: 5-fold CV, fit affine raw->y on train folds, apply to test fold.
    Returns calibrated predictions array (same length)."""
    from sklearn.linear_model import LinearRegression
    from sklearn.model_selection import KFold

    cal = np.full(len(y), np.nan)
    for c in city_list:
        idx = np.where(cities == c)[0]
        if len(idx) < 10:
            # too few for 5-fold; single affine fit (still in-city)
            lin = LinearRegression().fit(raw[idx].reshape(-1, 1), y[idx])
            cal[idx] = lin.predict(raw[idx].reshape(-1, 1))
            continue
        kf = KFold(n_splits=5, shuffle=True, random_state=42)
        for tr, te in kf.split(idx):
            lin = LinearRegression().fit(raw[idx[tr]].reshape(-1, 1), y[idx[tr]])
            cal[idx[te]] = lin.predict(raw[idx[te]].reshape(-1, 1))
    return cal


def report(name, raw_au, y_au, cau):
    print(f"\n===== {name} =====", flush=True)
    a = stats(raw_au, y_au)
    print("  RAW (uncalibrated EU->AU): MAE=%.1f bias=%+.1f r=%.2f std=%.1f (tgt std %.1f)"
          % (a[0], a[1], a[2], a[3], y_au.std()), flush=True)
    cal = incity_5fold_affine(raw_au, y_au, cau, AU_CITIES)
    print("  In-city 5-fold affine calibration:", flush=True)
    rows = {}
    for c in AU_CITIES:
        m = cau == c
        if m.sum() < 5:
            continue
        a = stats(cal[m], y_au[m])
        rows[c] = (m.sum(), a)
        print("    %-10s %4d | MAE=%.1f bias=%+.1f r=%.2f" % (c, m.sum(), a[0], a[1], a[2]), flush=True)
    # pooled with sydney
    a_all = stats(cal, y_au)
    print("    POOLED(all) %4d | MAE=%.1f r=%.2f" % (len(y_au), a_all[0], a_all[2]), flush=True)
    # pooled without sydney
    ms = cau != "sydney"
    a_ns = stats(cal[ms], y_au[ms])
    print("    POOLED(excl sydney) %4d | MAE=%.1f r=%.2f" % (ms.sum(), a_ns[0], a_ns[2]), flush=True)
    return {"per_city": rows, "pooled_all": a_all, "pooled_no_syd": a_ns}


def main():
    t0 = time.time()
    all_pts, n_eu, n_au, emb = get_data()

    tags = np.array([p[0] for p in all_pts])
    ys = np.array([p[3] for p in all_pts])

    eu_emb = emb[:n_eu]
    eu_y = ys[:n_eu]
    au_emb = emb[n_eu:]
    au_y = ys[n_eu:]
    au_city = tags[n_eu:]

    # coverage
    eu_ok = ~np.isnan(eu_emb).any(axis=1)
    au_ok = ~np.isnan(au_emb).any(axis=1)
    print("\n--- COVERAGE ---", flush=True)
    print("  EU embedding: %d/%d (%.1f%%)" % (eu_ok.sum(), n_eu, 100 * eu_ok.mean()), flush=True)
    print("  AU embedding: %d/%d (%.1f%%)" % (au_ok.sum(), n_au, 100 * au_ok.mean()), flush=True)
    for c in AU_CITIES:
        m = au_city == c
        if m.sum():
            print("    %-10s %4d/%4d (%.0f%%)" % (c, au_ok[m].sum(), m.sum(), 100 * au_ok[m].mean()), flush=True)

    # apply clean sentinel on AU (lden < 30) + require valid embedding
    au_keep = au_ok & (au_y >= CLEAN_SENTINEL)
    eu_keep = eu_ok  # EU truth comes from official rasters, keep all valid embeddings
    print("\n  after sentinel(lden>=%g)+coverage: EU train %d, AU eval %d"
          % (CLEAN_SENTINEL, eu_keep.sum(), au_keep.sum()), flush=True)

    Xtr = eu_emb[eu_keep]
    ytr = eu_y[eu_keep]
    Xau = au_emb[au_keep]
    yau = au_y[au_keep]
    cau = au_city[au_keep]

    from sklearn.ensemble import RandomForestRegressor
    from sklearn.linear_model import Ridge
    from sklearn.preprocessing import StandardScaler

    # (a) embedding-only RandomForest
    rf = RandomForestRegressor(n_estimators=500, min_samples_leaf=3, max_features="sqrt",
                               n_jobs=-1, random_state=42)
    rf.fit(Xtr, ytr)
    raw_rf = rf.predict(Xau)
    res_rf = report("EMBEDDING-ONLY  RandomForest (EU->AU)", raw_rf, yau, cau)

    # (a) embedding-only Ridge (scaled)
    sc = StandardScaler().fit(Xtr)
    rg = Ridge(alpha=10.0)
    rg.fit(sc.transform(Xtr), ytr)
    raw_rg = rg.predict(sc.transform(Xau))
    res_rg = report("EMBEDDING-ONLY  Ridge (EU->AU)", raw_rg, yau, cau)

    # (b) concat embedding + geometry features, aligned by index via transfer5_cache
    res_cat = None
    if os.path.exists(GEOM_CACHE):
        g = np.load(GEOM_CACHE, allow_pickle=True)
        Xnl_g, ynl_g = g["Xnl"], g["ynl"]
        Xau_g, yau_g, cau_g = g["Xau"], g["yau"], g["cau"]
        # AU geometry cache aligns index-for-index with our AU points (verified offline):
        # same load_au_points order, same yau. Confirm before concat.
        au_y_full = au_y  # full AU (n_au) before keep-mask
        aligned = (len(yau_g) == n_au) and np.allclose(yau_g, au_y_full, atol=1e-6)
        if aligned:
            # EU geometry cache order = NL then UK (transfer5 builds nlo+uko), matches our load_eu_points
            eu_aligned = (len(ynl_g) == n_eu) and np.allclose(ynl_g, eu_y, atol=1e-6)
            if eu_aligned:
                Xtr_cat = np.hstack([eu_emb[eu_keep], Xnl_g[eu_keep]])
                Xau_cat = np.hstack([au_emb[au_keep], Xau_g[au_keep]])
                rf2 = RandomForestRegressor(n_estimators=500, min_samples_leaf=3,
                                            max_features="sqrt", n_jobs=-1, random_state=42)
                rf2.fit(Xtr_cat, ytr)
                raw_cat = rf2.predict(Xau_cat)
                res_cat = report("CONCAT  embedding + geometry  RandomForest (EU->AU)",
                                 raw_cat, yau, cau)
            else:
                print("\n  [concat] EU geometry cache does not align with EU points -> skip", flush=True)
        else:
            print("\n  [concat] AU geometry cache does not align (len %d vs %d) -> skip"
                  % (len(yau_g), n_au), flush=True)
    else:
        print("\n  [concat] %s missing -> skip" % GEOM_CACHE, flush=True)

    # ---- verdict ----
    print("\n\n========== VERDICT vs geometry baseline (pooled excl sydney r0.70 / MAE3.8) ==========", flush=True)
    best_emb = max(res_rf["pooled_no_syd"][2], res_rg["pooled_no_syd"][2])
    best_name = "RF" if res_rf["pooled_no_syd"][2] >= res_rg["pooled_no_syd"][2] else "Ridge"
    print("  embedding-only best (%s) pooled-excl-sydney: r=%.2f MAE=%.1f"
          % (best_name, best_emb, (res_rf if best_name == "RF" else res_rg)["pooled_no_syd"][0]), flush=True)
    delta = best_emb - 0.70
    if delta > 0.03:
        print("  --> embedding SIGNIFICANTLY beats plateau (+%.2f r)" % delta, flush=True)
    elif delta > 0:
        print("  --> embedding marginally above plateau (+%.2f r)" % delta, flush=True)
    else:
        print("  --> embedding does NOT beat plateau (%.2f r)" % delta, flush=True)
    if res_cat:
        print("  concat pooled-excl-sydney: r=%.2f MAE=%.1f"
              % (res_cat["pooled_no_syd"][2], res_cat["pooled_no_syd"][0]), flush=True)

    print("\nelapsed %.0fs" % (time.time() - t0), flush=True)


if __name__ == "__main__":
    sys.exit(main())
