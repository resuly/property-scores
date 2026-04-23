"""Train production noise model: Physics + XGBoost residual → LA50 background noise.

Single model pipeline:
  1. Physics v8 computes features (road/rail/building/POI context)
  2. XGBoost predicts residual correction (LA50_true - physics_lden)
  3. Final output: physics_lden + residual = predicted LA50

Data: NoiseCapture Australia (ODbL, real measurements, zero legal risk)
Target: LA50 (50th percentile) = typical background noise for property assessment

Usage:
    python scripts/train_production_model.py              # full 10K, use cache
    python scripts/train_production_model.py --force      # re-extract features
    python scripts/train_production_model.py --max 2000   # limit sample size
"""

import csv
import json
import glob
import math
import os
import pickle
import sys
import time

import numpy as np
from sklearn.model_selection import KFold
from sklearn.metrics import mean_absolute_error
import xgboost as xgb

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from property_scores.noise.score import noise_score, _crtn_noise, _rail_noise_freq
from property_scores.noise.score import CLASS_TO_AADT, DEFAULT_SPEED_KMH, _bearing, NUM_FACADE_SECTORS
from property_scores.noise.buildings import buildings_in_radius, barrier_attenuation
from property_scores.common.overture import get_db, roads_near, aadt_near, nfdh_near, gtfs_rail_near

# Features that proved useless (importance = 0 across all models)
DROP_FEATURES = {"aadt_count", "aadt_max", "aadt_nearest_dist",
                 "rail_screening_max", "tram_max_peak_svc"}


_shared_db = None

def _get_shared_db():
    global _shared_db
    if _shared_db is None:
        _shared_db = get_db()
    return _shared_db


def extract_features(lat: float, lng: float, radius_m: int = 500) -> dict:
    db = _get_shared_db()
    feats = {}

    # Buildings
    bldgs = buildings_in_radius(db, lat, lng, radius_m)
    feats["building_count"] = len(bldgs)
    heights = [h for h, _, _ in bldgs] if bldgs else [0]
    feats["building_height_mean"] = np.mean(heights)
    feats["building_height_max"] = max(heights)
    feats["building_height_p75"] = np.percentile(heights, 75) if bldgs else 0

    # Inner zone density
    m_per_deg = 111_320 * math.cos(math.radians(lat))
    inner = [h for h, clng, clat in bldgs
             if math.sqrt(((clng - lng) * m_per_deg) ** 2 + ((clat - lat) * 111_320) ** 2) < 100]
    feats["building_count_100m"] = len(inner)
    feats["building_height_100m_mean"] = np.mean(inner) if inner else 0
    feats["density_ratio"] = len(inner) / max(len(bldgs), 1)

    # Roads
    roads = roads_near(db, lat, lng, radius_m)
    motor = [r for r in roads if r[0] not in ("footway", "path", "steps", "cycleway", "pedestrian", "track")]
    feats["road_count"] = len(motor)
    for cls in ["motorway", "trunk", "primary", "secondary", "tertiary", "residential", "service"]:
        cls_roads = [r for r in motor if r[0] == cls]
        feats[f"road_{cls}_count"] = len(cls_roads)
        feats[f"road_{cls}_min_dist"] = min((r[1] for r in cls_roads), default=radius_m)
    feats["roads_with_speed_pct"] = len([r for r in motor if r[2]]) / max(len(motor), 1)

    # Canyon ratio
    major = ("motorway", "trunk", "primary", "secondary", "tertiary")
    nearest_major = min((r[1] for r in motor if r[0] in major), default=radius_m)
    feats["canyon_ratio"] = (feats["building_height_100m_mean"] or feats["building_height_mean"]) / max(nearest_major, 5)
    feats["nearest_major_dist"] = nearest_major

    # Road energy (raw + screened)
    road_energies, screened_energies = [], []
    max_screening = 0
    for cls, dist_m, speed, slng, slat in motor:
        aadt = CLASS_TO_AADT.get(cls, 400)
        db_val = _crtn_noise(aadt, dist_m)
        if db_val > 0:
            road_energies.append(db_val)
            scr = barrier_attenuation(bldgs, slng, slat, lng, lat, dist_m)
            screened = max(db_val - scr, 0)
            if screened > 0:
                screened_energies.append(screened)
            max_screening = max(max_screening, scr)

    feats["road_db_max"] = max(road_energies) if road_energies else 0
    feats["road_db_mean"] = np.mean(road_energies) if road_energies else 0
    feats["road_db_sum_energy"] = 10 * math.log10(sum(10 ** (e / 10) for e in road_energies)) if road_energies else 0
    feats["road_screened_max"] = max(screened_energies) if screened_energies else 0
    feats["road_screened_sum"] = 10 * math.log10(sum(10 ** (e / 10) for e in sorted(screened_energies, reverse=True)[:8])) if screened_energies else 0
    feats["max_screening_db"] = max_screening

    # NFDH
    nfdh = nfdh_near(db, lat, lng, radius_m)
    feats["nfdh_count"] = len(nfdh)
    feats["nfdh_max_aadt"] = max((n[0] for n in nfdh), default=0)
    feats["nfdh_nearest_dist"] = min((n[3] for n in nfdh), default=radius_m)

    # Rail
    gtfs = gtfs_rail_near(db, lat, lng, radius_m)
    feats["rail_route_count"] = len(gtfs)
    trains = [g for g in gtfs if g[0] != 0]
    trams = [g for g in gtfs if g[0] == 0]
    feats["train_count"] = len(trains)
    feats["tram_count"] = len(trams)
    feats["train_min_dist"] = min((t[2] for t in trains), default=radius_m)
    feats["tram_min_dist"] = min((t[2] for t in trams), default=radius_m)
    feats["train_max_peak_svc"] = max((t[3] for t in trains), default=0)

    # Rail screening
    rail_raw, rail_scr = 0, 0
    for rt, rn, dist_m, pk, opk, slng, slat in gtfs:
        rtype = "tram" if rt == 0 else ("vline" if pk < 4 else "train")
        l = _rail_noise_freq(rtype, dist_m, pk * 0.4 + opk * 0.6)
        if l > 0:
            rail_raw = max(rail_raw, l)
            scr = barrier_attenuation(bldgs, slng, slat, lng, lat, dist_m) if dist_m > 20 else 0
            rail_scr = max(rail_scr, max(l - scr, 0))
    feats["rail_raw_db_max"] = rail_raw
    feats["rail_screened_db_max"] = rail_scr
    feats["rail_screening_delta"] = rail_raw - rail_scr

    # POI
    try:
        from property_scores.common.overture import pois_near
        pois = pois_near(db, lat, lng, 500)
        noise_cats = {"bar", "nightclub", "pub", "restaurant", "cafe", "construction", "factory", "industrial"}
        noise_pois = [p for p in pois if p[0] and any(c in p[0].lower() for c in noise_cats)]
        feats["poi_noise_count"] = len(noise_pois)
        feats["poi_noise_min_dist"] = min((p[1] for p in noise_pois), default=500)
        feats["poi_total_count"] = len(pois)
    except Exception:
        feats["poi_noise_count"] = 0
        feats["poi_noise_min_dist"] = 500
        feats["poi_total_count"] = 0

    # Directional
    sector_energy = [0.0] * NUM_FACADE_SECTORS
    sector_width = 2 * math.pi / NUM_FACADE_SECTORS
    for cls, dist_m, speed, slng, slat in motor:
        aadt = CLASS_TO_AADT.get(cls, 400)
        db_val = _crtn_noise(aadt, dist_m)
        if db_val > 0:
            bearing = _bearing(lat, lng, slat, slng)
            sector_energy[int(bearing / sector_width) % NUM_FACADE_SECTORS] += 10 ** (db_val / 10)
    sector_db = [10 * math.log10(e) if e > 0 else 0 for e in sector_energy]
    active = [s for s in sector_db if s > 0]
    feats["sector_max_db"] = max(sector_db) if sector_db else 0
    feats["sector_min_db"] = min(active) if active else 0
    feats["sector_range_db"] = (max(sector_db) - min(active)) if active else 0
    feats["sector_std_db"] = np.std(active) if active else 0
    feats["sectors_active"] = len(active)

    # Physics model output (computed from already-fetched data, no duplicate queries)
    from property_scores.noise.score import (
        _energy_sum, _lden, _adaptive_select,
        L10_TO_LEQ_DB, AMBIENT_DB, _DAY_ADJ, _EVE_ADJ, _NIGHT_ADJ,
        MAX_ROAD_SOURCES, MAX_RAIL_SOURCES,
    )
    # Road: top screened sources
    all_road = [(db_val, {}) for db_val in screened_energies if db_val > 0]
    top_roads = _adaptive_select(all_road, max_n=MAX_ROAD_SOURCES)
    road_e = sum(10 ** (l / 10) for l, _ in top_roads)
    road_db = 10 * math.log10(road_e) if road_e > 0 else 0.0

    # Rail: use already-computed values
    rail_db = feats["rail_screened_db_max"]

    road_leq = (road_db - L10_TO_LEQ_DB) if road_db > 0 else 0.0
    rail_leq = rail_db
    leq_d = max(_energy_sum(road_leq + _DAY_ADJ if road_leq > 0 else 0, rail_leq), AMBIENT_DB)
    leq_e = max(_energy_sum(road_leq + _EVE_ADJ if road_leq > 0 else 0,
                            max(rail_leq - 5, 0) if rail_leq > 0 else 0), AMBIENT_DB)
    leq_n = max(_energy_sum(road_leq + _NIGHT_ADJ if road_leq > 0 else 0, 0), AMBIENT_DB)
    lden = _lden(leq_d, leq_e, leq_n)
    score = max(0, min(100, round((75 - lden) / 35 * 100)))

    feats["physics_lden"] = round(lden, 1)
    feats["physics_road_db"] = round(road_db, 1)
    feats["physics_rail_db"] = round(rail_db, 1)
    feats["physics_max_facade"] = round(lden, 1)  # simplified (full facade needs sector calc)
    feats["physics_min_facade"] = round(max(lden - feats["sector_range_db"], AMBIENT_DB), 1)
    feats["physics_score"] = score

    # Drop useless features
    for k in DROP_FEATURES:
        feats.pop(k, None)

    return feats


def load_noisecapture(data_dir: str = "data/noisecapture",
                      la50_min: float = 40, la50_max: float = 80,
                      min_count: int = 5) -> list[dict]:
    points = []
    for f in glob.glob(f"{data_dir}/*.areas.geojson"):
        parts = os.path.basename(f).replace(".areas.geojson", "").split("_")
        state = parts[1] if len(parts) > 1 else "?"
        with open(f) as fh:
            data = json.load(fh)
        for feat in data.get("features", []):
            props = feat["properties"]
            geom = feat["geometry"]
            if geom["type"] != "Polygon":
                continue
            la50 = props.get("la50")
            count = props.get("measure_count", 0)
            if not la50 or count < min_count:
                continue
            la50 = float(la50)
            if la50 < la50_min or la50 > la50_max:
                continue
            coords = geom["coordinates"][0]
            clng = sum(c[0] for c in coords) / len(coords)
            clat = sum(c[1] for c in coords) / len(coords)
            points.append({"lat": clat, "lng": clng, "la50": la50,
                           "state": state, "count": count})
    return points


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=0, help="Max points (0=all)")
    parser.add_argument("--force", action="store_true", help="Force re-extract features")
    args = parser.parse_args()

    MODEL_PATH = "data/noise_ml_model_la50.pkl"

    print("=" * 70)
    print("Production Noise Model Training (LA50 background noise)")
    print("=" * 70)

    # Load data
    points = load_noisecapture()
    print(f"NoiseCapture points (LA50 40-80, ≥5 measures): {len(points)}")

    # Sample if needed
    if args.max > 0 and len(points) > args.max:
        points.sort(key=lambda p: p["la50"])
        step = len(points) / args.max
        points = [points[int(i * step)] for i in range(args.max)]
        print(f"Sampled to {len(points)}")

    N = len(points)

    # 80/20 train/test split (stratified by state)
    import random
    random.seed(42)
    random.shuffle(points)
    split = int(N * 0.8)
    train_points = points[:split]
    test_points = points[split:]
    print(f"Train: {len(train_points)} | Test: {len(test_points)} (held out)")

    # Feature extraction with cache
    cache = f"data/feature_cache_prod_{N}.npz"
    if os.path.exists(cache) and not args.force:
        print(f"\nLoading cached features from {cache}")
        d = np.load(cache, allow_pickle=True)
        all_features = list(d["features"])
        all_targets = list(d["targets"])
        all_weights = list(d["weights"])
        all_states = list(d["states"])
        all_is_train = list(d["is_train"])
    else:
        print(f"\nExtracting features for {N} points...")
        all_features, all_targets, all_weights, all_states, all_is_train = [], [], [], [], []
        train_set = set(id(p) for p in train_points)
        t0 = time.time()
        for i, p in enumerate(points):
            try:
                f = extract_features(p["lat"], p["lng"])
                all_features.append(f)
                all_targets.append(p["la50"])
                all_weights.append(min(p["count"] / 10, 5.0))  # weight by measurement count
                all_states.append(p["state"])
                all_is_train.append(id(p) in train_set)
            except Exception:
                pass
            if (i + 1) % 100 == 0:
                elapsed = time.time() - t0
                eta = elapsed / (i + 1) * (N - i - 1)
                print(f"  {i+1}/{N} ({elapsed:.0f}s, ETA {eta:.0f}s)")

        elapsed = time.time() - t0
        print(f"Extracted {len(all_features)} features in {elapsed:.0f}s")
        np.savez(cache,
                 features=np.array(all_features, dtype=object),
                 targets=np.array(all_targets),
                 weights=np.array(all_weights),
                 states=np.array(all_states),
                 is_train=np.array(all_is_train))
        print(f"Cached to {cache}")

    # Convert to arrays
    fn = sorted(all_features[0].keys())
    X = np.array([[f[k] for k in fn] for f in all_features])
    y = np.array(all_targets)
    w = np.array(all_weights)
    sa = np.array(all_states)
    is_train = np.array(all_is_train)

    X_train, y_train, w_train = X[is_train], y[is_train], w[is_train]
    X_test, y_test, w_test = X[~is_train], y[~is_train], w[~is_train]
    sa_test = sa[~is_train]

    print(f"\nFeatures: {len(fn)} | Train: {len(X_train)} | Test: {len(X_test)}")
    print(f"Target LA50: train mean={np.mean(y_train):.1f} test mean={np.mean(y_test):.1f}")

    # Physics baseline
    phys_idx = fn.index("physics_lden")
    phys_train = X_train[:, phys_idx]
    phys_test = X_test[:, phys_idx]
    print(f"\nPhysics baseline (test): MAE={mean_absolute_error(y_test, phys_test):.1f} Bias={np.mean(phys_test-y_test):+.1f}")

    # Residual target
    y_resid_train = y_train - phys_train
    y_resid_test = y_test - phys_test

    # Cross-validation on train set
    print("\n--- 5-Fold CV on Train Set ---")
    kf = KFold(n_splits=5, shuffle=True, random_state=42)
    cv_maes = []
    for fold, (tr, va) in enumerate(kf.split(X_train)):
        m = xgb.XGBRegressor(
            n_estimators=300, max_depth=5, learning_rate=0.03,
            subsample=0.8, colsample_bytree=0.8,
            reg_alpha=0.5, reg_lambda=2.0, min_child_weight=5,
            random_state=42,
        )
        m.fit(X_train[tr], y_resid_train[tr], sample_weight=w_train[tr], verbose=False)
        pred = phys_train[va] + m.predict(X_train[va])
        mae = mean_absolute_error(y_train[va], pred)
        w5 = np.mean(np.abs(pred - y_train[va]) <= 5) * 100
        cv_maes.append(mae)
        print(f"  Fold {fold+1}: MAE={mae:.2f} W5={w5:.0f}%")
    print(f"  CV Mean: MAE={np.mean(cv_maes):.2f} ± {np.std(cv_maes):.2f}")

    # Train final model on full train set
    print("\nTraining final model...")
    model = xgb.XGBRegressor(
        n_estimators=300, max_depth=5, learning_rate=0.03,
        subsample=0.8, colsample_bytree=0.8,
        reg_alpha=0.5, reg_lambda=2.0, min_child_weight=5,
        random_state=42,
    )
    model.fit(X_train, y_resid_train, sample_weight=w_train, verbose=False)

    # Evaluate on held-out TEST set
    test_pred = phys_test + model.predict(X_test)
    test_mae = mean_absolute_error(y_test, test_pred)
    test_bias = np.mean(test_pred - y_test)
    test_w5 = np.mean(np.abs(test_pred - y_test) <= 5) * 100
    test_w10 = np.mean(np.abs(test_pred - y_test) <= 10) * 100

    print(f"\n{'='*70}")
    print(f"HELD-OUT TEST SET RESULTS (never seen during training)")
    print(f"{'='*70}")
    print(f"  MAE:  {test_mae:.2f} dB")
    print(f"  Bias: {test_bias:+.2f} dB")
    print(f"  W5:   {test_w5:.0f}%")
    print(f"  W10:  {test_w10:.0f}%")

    # Test by noise level
    print(f"\nBy LA50 level (test set):")
    for lo, hi, lb in [(40, 50, "Quiet 40-50"), (50, 60, "Moderate 50-60"),
                        (60, 70, "Loud 60-70"), (70, 80, "V.Loud 70-80")]:
        mask = (y_test >= lo) & (y_test < hi)
        if mask.sum() >= 5:
            pe = phys_test[mask] - y_test[mask]
            me = test_pred[mask] - y_test[mask]
            print(f"  {lb}: n={mask.sum():3d} | Phys MAE={np.mean(np.abs(pe)):.1f} | ML MAE={np.mean(np.abs(me)):.1f} W5={np.mean(np.abs(me)<=5)*100:.0f}%")

    # Test by state
    print(f"\nBy state (test set):")
    for state in sorted(set(sa_test)):
        mask = sa_test == state
        if mask.sum() >= 5:
            me = test_pred[mask] - y_test[mask]
            print(f"  {state:30s}: n={mask.sum():3d} MAE={np.mean(np.abs(me)):.1f} Bias={np.mean(me):+.1f} W5={np.mean(np.abs(me)<=5)*100:.0f}%")

    # Feature importance
    imp = sorted(zip(fn, model.feature_importances_), key=lambda x: x[1], reverse=True)
    print(f"\nTop 10 features:")
    for n, v in imp[:10]:
        print(f"  {n:35s} {v:.3f}")

    # Retrain on ALL data for production deployment
    print("\nRetraining on ALL data for production...")
    phys_all = X[:, phys_idx]
    y_resid_all = y - phys_all
    prod_model = xgb.XGBRegressor(
        n_estimators=300, max_depth=5, learning_rate=0.03,
        subsample=0.8, colsample_bytree=0.8,
        reg_alpha=0.5, reg_lambda=2.0, min_child_weight=5,
        random_state=42,
    )
    prod_model.fit(X, y_resid_all, sample_weight=w, verbose=False)

    # Save
    pickle.dump({
        "model": prod_model,
        "feature_names": fn,
        "mode": "residual",
        "target": "LA50_background_noise",
        "n_train": len(X),
        "test_mae": float(test_mae),
        "test_bias": float(test_bias),
        "test_w5": float(test_w5),
        "cv_mae": float(np.mean(cv_maes)),
    }, open(MODEL_PATH, "wb"))

    size_kb = os.path.getsize(MODEL_PATH) // 1024
    print(f"\nSaved production model: {MODEL_PATH} ({size_kb} KB)")
    print(f"  Test MAE: {test_mae:.2f} | CV MAE: {np.mean(cv_maes):.2f}")


if __name__ == "__main__":
    main()
