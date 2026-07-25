"""POC: distill the (A$100k) SoundPLAN professional road-noise model into a FREE
geodata-ML, following the SOTA (Staab/DLR land-use-regression + Shen/de Hoogh).

Idea: instead of training on crowdsourced NoiseCapture (which we proved does NOT
capture road noise), train a model on OPEN geodata features to predict the
SoundPLAN road Lden from our free AURIN 7-city sample. Validate honestly with
LEAVE-ONE-CITY-OUT (train 6 cities, predict the held-out one) = the real test of
"can a free model approximate the expensive product anywhere".

Features = our existing physics + geodata features (extract_features) + measured
AADT summary + a few SOTA road-class buffer encodings.
Target  = SoundPLAN road Lden (sp_rd_max facade, d/e/n -> Lden).

Run: .venv/bin/python scripts/poc_soundplan_distill.py [--n_per_city 400]
"""
import os
os.environ["NOISE_ML_CORRECTION"] = "1"
import argparse
import csv
import glob
import math
import re
import sys
import time
import logging
logging.basicConfig(level=logging.ERROR)

import numpy as np
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.train_production_model import extract_features  # noqa: E402
from scripts.experiment_retrain_noise import measured_aadt_features, AADT_KEYS  # noqa: E402
from property_scores.common.overture import get_db, roads_near  # noqa: E402
from property_scores.noise.ml_model import predict_correction  # noqa: E402

CITIES = ["melbourne", "sydney", "adelaide", "perth", "hobart", "canberra", "darwin"]
MAJOR = ("motorway", "trunk", "primary", "secondary", "tertiary")


def lden(d, e, n):
    return 10 * math.log10((12 * 10 ** (d / 10) + 4 * 10 ** ((e + 5) / 10) + 8 * 10 ** ((n + 10) / 10)) / 24) if max(d, e, n) > 0 else 0.0


def sota_road_features(db, lat, lng):
    """Staab/Shen-style: per-class road length (count proxy) + inverse-distance
    in multiple buffer rings."""
    roads = roads_near(db, lat, lng, 800)
    m_per_deg = 111_320 * math.cos(math.radians(lat))
    feats = {}
    rings = [50, 100, 200, 400, 800]
    for cls in MAJOR + ("residential", "service"):
        crl = [r for r in roads if r[0] == cls]
        # inverse-distance weighted "presence" of this class
        feats[f"sota_{cls}_invd"] = sum(1.0 / max(r[1], 10) for r in crl)
        for ring in rings:
            feats[f"sota_{cls}_n{ring}"] = sum(1 for r in crl if r[1] <= ring)
    # nearest major road distance + count of major roads in 200m
    feats["sota_nearest_major"] = min((r[1] for r in roads if r[0] in MAJOR), default=800)
    feats["sota_major_n200"] = sum(1 for r in roads if r[0] in MAJOR and r[1] <= 200)
    return feats


def load_points(n_per_city):
    pts = []
    for c in CITIES:
        f = f"data/ambient_sample/antn_{c}_buildings_.csv"
        if not os.path.exists(f):
            continue
        rows = list(csv.DictReader(open(f)))
        if n_per_city and len(rows) > n_per_city:
            rows = rows[::len(rows) // n_per_city][:n_per_city]
        for r in rows:
            m = re.search(r'POINT \(([-\d.]+) ([-\d.]+)\)', r["geometry"])
            if not m:
                continue
            lat, lng = float(m.group(1)), float(m.group(2))
            tgt = lden(float(r["sp_rd_max_d"]), float(r["sp_rd_max_e"]), float(r["sp_rd_max_n"]))
            if tgt <= 0:
                continue
            pts.append((c, lat, lng, tgt))
    return pts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n_per_city", type=int, default=400)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    pts = load_points(args.n_per_city)
    print(f"SoundPLAN points: {len(pts)} across {len(set(p[0] for p in pts))} cities", flush=True)

    cache = f"data/feature_cache_soundplan_{len(pts)}.npz"
    db = get_db()
    if os.path.exists(cache) and not args.force:
        d = np.load(cache, allow_pickle=True)
        feats = list(d["feats"]); tgt = d["tgt"]; city = list(d["city"])
        phys = d["phys"]; ml = d["ml"]
        print(f"loaded {cache}", flush=True)
    else:
        feats, tgt, city, phys, ml = [], [], [], [], []
        t0 = time.time()
        for i, (c, lat, lng, t) in enumerate(pts):
            try:
                f = extract_features(lat, lng)
                f.update(measured_aadt_features(db, lat, lng))
                f.update(sota_road_features(db, lat, lng))
                p = f["physics_lden"]
                corr = predict_correction(f)
                feats.append(f); tgt.append(t); city.append(c)
                phys.append(p); ml.append(p + corr if corr is not None else p)
            except Exception:
                pass
            if (i + 1) % 200 == 0:
                el = time.time() - t0
                print(f"  {i+1}/{len(pts)} ({el:.0f}s ETA {el/(i+1)*(len(pts)-i-1):.0f}s)", flush=True)
        tgt = np.array(tgt); phys = np.array(phys); ml = np.array(ml)
        np.savez(cache, feats=np.array(feats, dtype=object), tgt=tgt,
                 city=np.array(city), phys=phys, ml=ml)
        print(f"extracted {len(feats)} in {time.time()-t0:.0f}s -> {cache}", flush=True)

    city = np.array(city)
    keys = sorted([k for k in feats[0].keys()])
    X = np.array([[f.get(k, 0) for k in keys] for f in feats], dtype=float)
    tgt = np.array(tgt)

    def stats(p, t):
        return mean_absolute_error(t, p), np.mean(p - t), (np.corrcoef(p, t)[0, 1] if p.std() > 0 else float("nan"))

    print("\n=== LEAVE-ONE-CITY-OUT: distilled RF vs physics vs current ML ===", flush=True)
    print("%-10s %4s | %-16s | %-16s | %-16s" % ("held-out", "n", "RF-distill(MAE/bias/r)", "physics(MAE/bias/r)", "ML(MAE/bias/r)"))
    rf_all = np.zeros(len(tgt))
    for held in CITIES:
        te = city == held
        tr = ~te
        if te.sum() < 5 or tr.sum() < 50:
            continue
        rf = RandomForestRegressor(n_estimators=400, max_depth=None, min_samples_leaf=3,
                                   max_features="sqrt", n_jobs=-1, random_state=42)
        rf.fit(X[tr], tgt[tr])
        pred = rf.predict(X[te])
        rf_all[te] = pred
        a = stats(pred, tgt[te]); b = stats(phys[te], tgt[te]); c2 = stats(ml[te], tgt[te])
        print("%-10s %4d | %4.1f/%+5.1f/%5.2f | %4.1f/%+5.1f/%5.2f | %4.1f/%+5.1f/%5.2f" %
              (held, te.sum(), a[0], a[1], a[2], b[0], b[1], b[2], c2[0], c2[1], c2[2]), flush=True)
    print("-" * 78, flush=True)
    a = stats(rf_all, tgt); b = stats(phys, tgt); c2 = stats(ml, tgt)
    print("%-10s %4d | %4.1f/%+5.1f/%5.2f | %4.1f/%+5.1f/%5.2f | %4.1f/%+5.1f/%5.2f" %
          ("POOLED-LOCO", len(tgt), a[0], a[1], a[2], b[0], b[1], b[2], c2[0], c2[1], c2[2]), flush=True)

    # feature importance (train on all)
    rf = RandomForestRegressor(n_estimators=400, min_samples_leaf=3, max_features="sqrt", n_jobs=-1, random_state=42)
    rf.fit(X, tgt)
    imp = sorted(zip(keys, rf.feature_importances_), key=lambda x: -x[1])[:15]
    print("\nTop features:", flush=True)
    for k, v in imp:
        print("  %-26s %.3f" % (k, v), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
