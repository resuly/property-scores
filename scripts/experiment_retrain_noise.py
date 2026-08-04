"""Experiment: retrain the noise ML and compare approaches for fixing the
city-vs-country COMPRESSION (loud reads moderate, quiet reads loud).

Hypothesis: the deployed XGBoost regresses to the mean because its road
features come only from coarse Overture CLASS_TO_AADT — it never sees the real
traffic volume. Adding measured AADT (the restored multi-state aadt_*.parquet)
should give it the signal to separate loud from quiet.

Compares, on a held-out split + a labelled loud/quiet probe set:
  C  (ablation): la50 target, features WITHOUT measured AADT  (~ current design)
  A           : la50 target, features WITH measured AADT
  B           : laeq target, features WITH measured AADT  (energy-equiv metric)

Reports calibration (MAE to own target) AND differentiation (predicted spread +
loud-minus-quiet gap on the probe set — the thing the complaints are about).

Run:  .venv/bin/python scripts/experiment_retrain_noise.py [--max N]
"""

import argparse
import glob
import json
import math
import os
import sys
import time

import numpy as np
import xgboost as xgb
from sklearn.metrics import mean_absolute_error
from sklearn.model_selection import train_test_split

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.train_production_model import extract_features  # noqa: E402
from property_scores.common.overture import get_db, aadt_near  # noqa: E402
from property_scores.noise.score import _crtn_noise, DEFAULT_SPEED_KMH  # noqa: E402
from property_scores.noise.buildings import buildings_in_radius, barrier_attenuation  # noqa: E402

AADT_KEYS = ["meas_aadt_count", "meas_aadt_max", "meas_aadt_nearest_dist",
             "meas_road_energy_db", "meas_road_db_max"]


def measured_aadt_features(db, lat, lng, radius_m=500):
    """Summarise nearby measured AADT (mirrors score.py: dedup per road by
    nearest, CRTN + building screening, energy sum)."""
    rows = aadt_near(db, lat, lng, radius_m)
    seen = {}
    for r in rows:
        aadt_val, hv_pct, road_name, dist_m, near_lng, near_lat, _src = r
        key = ("name", road_name) if road_name else ("loc", round(near_lng, 3), round(near_lat, 3))
        cur = seen.get(key)
        if cur is None or dist_m < cur[3]:
            seen[key] = r
    segs = list(seen.values())
    if not segs:
        return {"meas_aadt_count": 0, "meas_aadt_max": 0, "meas_aadt_nearest_dist": radius_m,
                "meas_road_energy_db": 0.0, "meas_road_db_max": 0.0}
    bldgs = buildings_in_radius(db, lat, lng, radius_m)
    energies = []
    for aadt_val, hv_pct, road_name, dist_m, near_lng, near_lat, _src in segs:
        hv = (hv_pct * 100) if hv_pct else 0.0
        l = _crtn_noise(int(aadt_val), dist_m, hv_pct=hv, speed_kmh=DEFAULT_SPEED_KMH)
        if l <= 0:
            continue
        scr = barrier_attenuation(bldgs, near_lng, near_lat, lng, lat, dist_m) if dist_m > 20 else 0
        ls = max(l - scr, 0.0)
        if ls > 0:
            energies.append(ls)
    energy_db = 10 * math.log10(sum(10 ** (e / 10) for e in energies)) if energies else 0.0
    return {
        "meas_aadt_count": len(segs),
        "meas_aadt_max": max(int(s[0]) for s in segs),
        "meas_aadt_nearest_dist": min(s[3] for s in segs),
        "meas_road_energy_db": round(energy_db, 1),
        "meas_road_db_max": round(max(energies), 1) if energies else 0.0,
    }


def load_points(max_n=0, la50_min=40, la50_max=80, min_count=5):
    pts = []
    for f in glob.glob("data/noisecapture/*.areas.geojson"):
        parts = os.path.basename(f).replace(".areas.geojson", "").split("_")
        state = parts[1] if len(parts) > 1 else "?"
        try:
            data = json.load(open(f))
        except Exception:
            continue
        for feat in data.get("features", []):
            p = feat["properties"]
            g = feat["geometry"]
            if g["type"] != "Polygon":
                continue
            la50, laeq, cnt = p.get("la50"), p.get("laeq"), p.get("measure_count", 0)
            if not la50 or not laeq or cnt < min_count:
                continue
            la50 = float(la50)
            if la50 < la50_min or la50 > la50_max:
                continue
            coords = g["coordinates"][0]
            clng = sum(c[0] for c in coords) / len(coords)
            clat = sum(c[1] for c in coords) / len(coords)
            pts.append({"lat": clat, "lng": clng, "la50": la50, "laeq": float(laeq),
                        "count": cnt, "state": state})
    if max_n and len(pts) > max_n:
        pts.sort(key=lambda x: x["la50"])
        step = len(pts) / max_n
        pts = [pts[int(i * step)] for i in range(max_n)]
    return pts


# Labelled probe set — known loud (near major road) vs quiet (leafy/residential)
PROBE_LOUD = [("Hoddle", -37.807, 144.991), ("SydCBD", -33.8700, 151.2070),
              ("ParramattaRd", -33.8870, 151.1480), ("BrisbaneCBD", -27.4670, 153.0270),
              ("MitchellFwy", -31.9300, 115.8350), ("AnzacHwySA", -34.9530, 138.5520)]
PROBE_QUIET = [("ParkOrchards", -37.7772, 145.2167), ("Warrandyte", -37.7430, 145.2150),
               ("Wahroonga", -33.7180, 151.1180), ("Pymble", -33.7430, 151.1410),
               ("Brookfield", -27.4960, 152.9000), ("StirlingHills", -35.0030, 138.7180)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=5000)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    pts = load_points(max_n=args.max)
    N = len(pts)
    print(f"points (la50 40-80, >=5 measures): {N}")

    cache = f"data/feature_cache_exp_{N}.npz"
    db = get_db()
    if os.path.exists(cache) and not args.force:
        d = np.load(cache, allow_pickle=True)
        feats = list(d["feats"]); la50 = d["la50"]; laeq = d["laeq"]; wt = d["wt"]
        print(f"loaded cached features {cache}")
    else:
        feats, la50, laeq, wt = [], [], [], []
        t0 = time.time()
        for i, p in enumerate(pts):
            try:
                f = extract_features(p["lat"], p["lng"])
                f.update(measured_aadt_features(db, p["lat"], p["lng"]))
                feats.append(f); la50.append(p["la50"]); laeq.append(p["laeq"])
                wt.append(min(p["count"] / 10, 5.0))
            except Exception:
                pass
            if (i + 1) % 200 == 0:
                el = time.time() - t0
                print(f"  {i+1}/{N} ({el:.0f}s, ETA {el/(i+1)*(N-i-1):.0f}s)", flush=True)
        la50, laeq, wt = np.array(la50), np.array(laeq), np.array(wt)
        np.savez(cache, feats=np.array(feats, dtype=object), la50=la50, laeq=laeq, wt=wt)
        print(f"extracted {len(feats)} in {time.time()-t0:.0f}s -> {cache}")

    all_keys = sorted(feats[0].keys())
    no_aadt_keys = [k for k in all_keys if k not in AADT_KEYS]

    def mat(keys):
        return np.array([[f.get(k, 0) for k in keys] for f in feats], dtype=float)

    phys = np.array([f["physics_lden"] for f in feats])
    idx = np.arange(len(feats))
    tr, te = train_test_split(idx, test_size=0.2, random_state=42)

    # probe features
    probe = {}
    for name, la, lo in PROBE_LOUD + PROBE_QUIET:
        f = extract_features(la, lo); f.update(measured_aadt_features(db, la, lo))
        probe[name] = f
    probe_phys = {n: probe[n]["physics_lden"] for n in probe}

    def train_eval(keys, target, label, params=None, blend=1.0):
        X = mat(keys)
        y = target
        resid = y - phys
        p = dict(n_estimators=300, max_depth=5, learning_rate=0.03, subsample=0.8,
                 colsample_bytree=0.8, reg_alpha=0.5, reg_lambda=2.0,
                 min_child_weight=5, random_state=42)
        if params:
            p.update(params)
        m = xgb.XGBRegressor(**p)
        m.fit(X[tr], resid[tr], sample_weight=wt[tr], verbose=False)
        # blend: final = blend*ml_pred + (1-blend)*physics  (blend=1 -> pure ML)
        def predict(Xrows, physrows):
            ml = physrows + m.predict(Xrows)
            return blend * ml + (1 - blend) * physrows
        pred = predict(X[te], phys[te])
        mae = mean_absolute_error(y[te], pred)
        pstd = np.std(pred)

        def pp(name):
            row = np.array([[probe[name].get(k, 0) for k in keys]], dtype=float)
            return predict(row, np.array([probe_phys[name]]))[0]
        loud = [pp(n) for n, _, _ in PROBE_LOUD]
        quiet = [pp(n) for n, _, _ in PROBE_QUIET]
        gap = np.mean(loud) - np.mean(quiet)
        tgt = "la50" if target is la50 else "laeq"
        print(f"\n=== {label} (target={tgt}, {len(keys)} feats) ===")
        print(f"  held-out MAE={mae:.2f}  pred_std={pstd:.1f}  (target_std={np.std(y[te]):.1f})")
        print(f"  PROBE loud mean={np.mean(loud):.1f}  quiet mean={np.mean(quiet):.1f}  GAP={gap:+.1f} dB")
        print("   loud :", " ".join(f"{n}={pp(n):.0f}" for n, _, _ in PROBE_LOUD))
        print("   quiet:", " ".join(f"{n}={pp(n):.0f}" for n, _, _ in PROBE_QUIET))
        return {"label": label, "mae": mae, "gap": gap, "pred_std": pstd}

    print("\n" + "=" * 70)
    pl = [probe_phys[n] for n, _, _ in PROBE_LOUD]; pq = [probe_phys[n] for n, _, _ in PROBE_QUIET]
    print(f"RAW PHYSICS probe: loud={np.mean(pl):.1f} quiet={np.mean(pq):.1f} GAP={np.mean(pl)-np.mean(pq):+.1f}")

    LESSREG = dict(max_depth=8, min_child_weight=2, reg_alpha=0.0, reg_lambda=0.3, n_estimators=500)
    results = [
        train_eval(no_aadt_keys, la50, "C ablation: la50, NO measured-AADT (~current)"),
        train_eval(all_keys, la50, "A: la50 + measured-AADT"),
        train_eval(all_keys, laeq, "B: laeq + measured-AADT"),
        train_eval(all_keys, la50, "D: la50 + AADT, LESS regularized", params=LESSREG),
        train_eval(all_keys, la50, "E: la50 + AADT, blend 50% physics", blend=0.5),
        train_eval(all_keys, la50, "F: la50 + AADT, lessreg + blend 0.6", params=LESSREG, blend=0.6),
    ]

    # --- Order-preserving (monotonic) calibration: preserves physics ranking,
    #     only rescales the absolute level. Pure physics_lden -> target. ---
    from sklearn.linear_model import LinearRegression
    from sklearn.isotonic import IsotonicRegression

    def linear_eval(label, target, feat_keys=None):
        if feat_keys is None:
            Xtr = phys[tr].reshape(-1, 1); Xte = phys[te].reshape(-1, 1)
            probe_X = {n: np.array([[probe_phys[n]]]) for n in probe}
        else:
            Xtr = mat(feat_keys)[tr]; Xte = mat(feat_keys)[te]
            probe_X = {n: np.array([[probe[n].get(k, 0) for k in feat_keys]]) for n in probe}
        lr = LinearRegression()
        lr.fit(Xtr, target[tr], sample_weight=wt[tr])
        pred = lr.predict(Xte)
        mae = mean_absolute_error(target[te], pred)
        def pp(n): return lr.predict(probe_X[n])[0]
        loud = [pp(n) for n, _, _ in PROBE_LOUD]; quiet = [pp(n) for n, _, _ in PROBE_QUIET]
        gap = np.mean(loud) - np.mean(quiet)
        print(f"\n=== {label} ===")
        print(f"  coef={lr.coef_[:3]} intercept={lr.intercept_:.1f}")
        print(f"  held-out MAE={mae:.2f}  pred_std={np.std(pred):.1f}")
        print(f"  PROBE loud={np.mean(loud):.1f} quiet={np.mean(quiet):.1f} GAP={gap:+.1f}")
        print("   loud :", " ".join(f"{n}={pp(n):.0f}" for n, _, _ in PROBE_LOUD))
        print("   quiet:", " ".join(f"{n}={pp(n):.0f}" for n, _, _ in PROBE_QUIET))
        return {"label": label, "mae": mae, "gap": gap, "pred_std": np.std(pred)}

    def isotonic_eval(label, target):
        iso = IsotonicRegression(out_of_bounds="clip")
        iso.fit(phys[tr], target[tr], sample_weight=wt[tr])
        pred = iso.predict(phys[te])
        mae = mean_absolute_error(target[te], pred)
        def pp(n): return iso.predict([probe_phys[n]])[0]
        loud = [pp(n) for n, _, _ in PROBE_LOUD]; quiet = [pp(n) for n, _, _ in PROBE_QUIET]
        gap = np.mean(loud) - np.mean(quiet)
        print(f"\n=== {label} ===")
        print(f"  held-out MAE={mae:.2f} pred_std={np.std(pred):.1f}  PROBE loud={np.mean(loud):.1f} quiet={np.mean(quiet):.1f} GAP={gap:+.1f}")
        print("   loud :", " ".join(f"{n}={pp(n):.0f}" for n, _, _ in PROBE_LOUD))
        print("   quiet:", " ".join(f"{n}={pp(n):.0f}" for n, _, _ in PROBE_QUIET))
        return {"label": label, "mae": mae, "gap": gap, "pred_std": np.std(pred)}

    KEY = ["physics_lden", "meas_road_energy_db", "physics_road_db", "sector_max_db", "road_count"]
    results += [
        linear_eval("G: LINEAR calibration physics_lden->la50 (order-preserving)", la50),
        isotonic_eval("H: ISOTONIC calibration physics_lden->la50", la50),
        linear_eval("I: LINEAR few-feature (phys+meas_road+...)->la50", la50, KEY),
    ]

    print("\n" + "=" * 70)
    print("SUMMARY (bigger GAP = better loud/quiet separation; lower MAE = better calibration)")
    for r in results:
        print(f"  {r['label'][:48]:48s} MAE={r['mae']:.2f} GAP={r['gap']:+.1f} std={r['pred_std']:.1f}")


if __name__ == "__main__":
    raise SystemExit(main())
