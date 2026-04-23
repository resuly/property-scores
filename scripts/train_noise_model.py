"""Train ML noise correction model using Ambient Maps ground truth."""

import csv
import json
import math
import time
import sys
import os
import pickle

import numpy as np
from sklearn.model_selection import KFold
from sklearn.metrics import mean_absolute_error, mean_squared_error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from property_scores.common.overture import (
    get_db, roads_near, rail_near, aadt_near, nfdh_near, gtfs_rail_near,
)
from property_scores.noise.buildings import buildings_in_radius, barrier_attenuation
from property_scores.noise.score import (
    noise_score, _crtn_noise, CLASS_TO_AADT, DEFAULT_SPEED_KMH,
    _bearing, NUM_FACADE_SECTORS,
)


def extract_features(lat: float, lng: float, radius_m: int = 500) -> dict:
    """Extract ~30 features for a single location."""
    db = get_db()
    feats = {}

    # --- Buildings ---
    bldgs = buildings_in_radius(db, lat, lng, radius_m)
    feats["building_count"] = len(bldgs)
    if bldgs:
        heights = [h for h, _, _ in bldgs]
        feats["building_height_mean"] = np.mean(heights)
        feats["building_height_max"] = max(heights)
        feats["building_height_p75"] = np.percentile(heights, 75)
    else:
        feats["building_height_mean"] = 0
        feats["building_height_max"] = 0
        feats["building_height_p75"] = 0

    # --- Roads by class ---
    roads = roads_near(db, lat, lng, radius_m)
    motor_roads = [r for r in roads if r[0] not in
                   ("footway", "path", "steps", "cycleway", "pedestrian", "track")]

    feats["road_count"] = len(motor_roads)
    for cls in ["motorway", "trunk", "primary", "secondary", "tertiary", "residential", "service"]:
        cls_roads = [r for r in motor_roads if r[0] == cls]
        feats[f"road_{cls}_count"] = len(cls_roads)
        feats[f"road_{cls}_min_dist"] = min((r[1] for r in cls_roads), default=radius_m)

    # Speed limit coverage
    with_speed = [r for r in motor_roads if r[2]]
    feats["roads_with_speed_pct"] = len(with_speed) / max(len(motor_roads), 1)

    # Road energy features (unscreened, for ML to learn screening effect)
    road_energies = []
    for cls, dist_m, speed, slng, slat in motor_roads:
        aadt = CLASS_TO_AADT.get(cls, 400)
        db_val = _crtn_noise(aadt, dist_m)
        if db_val > 0:
            road_energies.append(db_val)
    if road_energies:
        feats["road_db_max"] = max(road_energies)
        feats["road_db_mean"] = np.mean(road_energies)
        feats["road_db_sum_energy"] = 10 * math.log10(sum(10 ** (e / 10) for e in road_energies))
    else:
        feats["road_db_max"] = 0
        feats["road_db_mean"] = 0
        feats["road_db_sum_energy"] = 0

    # Screened road energy (top sources with screening)
    screened_energies = []
    max_screening = 0
    for cls, dist_m, speed, slng, slat in motor_roads:
        aadt = CLASS_TO_AADT.get(cls, 400)
        db_val = _crtn_noise(aadt, dist_m)
        if db_val > 0:
            scr = barrier_attenuation(bldgs, slng, slat, lng, lat, dist_m)
            screened = max(db_val - scr, 0)
            if screened > 0:
                screened_energies.append(screened)
            if scr > max_screening:
                max_screening = scr
    if screened_energies:
        feats["road_screened_max"] = max(screened_energies)
        feats["road_screened_sum"] = 10 * math.log10(
            sum(10 ** (e / 10) for e in sorted(screened_energies, reverse=True)[:8])
        )
    else:
        feats["road_screened_max"] = 0
        feats["road_screened_sum"] = 0
    feats["max_screening_db"] = max_screening

    # --- AADT / NFDH ---
    aadt_segs = aadt_near(db, lat, lng, radius_m)
    nfdh_stns = nfdh_near(db, lat, lng, radius_m)
    feats["aadt_count"] = len(aadt_segs)
    feats["nfdh_count"] = len(nfdh_stns)
    if aadt_segs:
        feats["aadt_max"] = max(a[0] for a in aadt_segs)
        feats["aadt_nearest_dist"] = min(a[3] for a in aadt_segs)
    else:
        feats["aadt_max"] = 0
        feats["aadt_nearest_dist"] = radius_m
    if nfdh_stns:
        feats["nfdh_max_aadt"] = max(n[0] for n in nfdh_stns)
        feats["nfdh_nearest_dist"] = min(n[3] for n in nfdh_stns)
    else:
        feats["nfdh_max_aadt"] = 0
        feats["nfdh_nearest_dist"] = radius_m

    # --- Rail (with screening) ---
    gtfs = gtfs_rail_near(db, lat, lng, radius_m)
    feats["rail_route_count"] = len(gtfs)
    trains = [g for g in gtfs if g[0] != 0]
    trams = [g for g in gtfs if g[0] == 0]
    feats["train_count"] = len(trains)
    feats["tram_count"] = len(trams)
    feats["train_min_dist"] = min((t[2] for t in trains), default=radius_m)
    feats["tram_min_dist"] = min((t[2] for t in trams), default=radius_m)
    if trains:
        feats["train_max_peak_svc"] = max(t[3] for t in trains)
    else:
        feats["train_max_peak_svc"] = 0
    if trams:
        feats["tram_max_peak_svc"] = max(t[3] for t in trams)
    else:
        feats["tram_max_peak_svc"] = 0

    # Rail noise: raw vs screened (let ML learn the right screening amount)
    from property_scores.noise.score import _rail_noise_freq
    rail_raw_db = 0.0
    rail_screened_db = 0.0
    max_rail_screening = 0.0
    for rt, rn, dist_m, pk, opk, slng, slat in gtfs:
        rtype = "tram" if rt == 0 else ("vline" if pk < 4 else "train")
        svc = pk * 0.4 + opk * 0.6
        l = _rail_noise_freq(rtype, dist_m, svc)
        if l > 0:
            rail_raw_db = max(rail_raw_db, l)
            scr = barrier_attenuation(bldgs, slng, slat, lng, lat, dist_m) if dist_m > 20 else 0
            screened = max(l - scr, 0)
            rail_screened_db = max(rail_screened_db, screened)
            if scr > max_rail_screening:
                max_rail_screening = scr
    feats["rail_raw_db_max"] = rail_raw_db
    feats["rail_screened_db_max"] = rail_screened_db
    feats["rail_screening_max"] = max_rail_screening
    feats["rail_screening_delta"] = rail_raw_db - rail_screened_db

    # --- Directional features (road energy variance across sectors) ---
    sector_energy = [0.0] * NUM_FACADE_SECTORS
    sector_width = 2 * math.pi / NUM_FACADE_SECTORS
    for cls, dist_m, speed, slng, slat in motor_roads:
        aadt = CLASS_TO_AADT.get(cls, 400)
        db_val = _crtn_noise(aadt, dist_m)
        if db_val > 0:
            bearing = _bearing(lat, lng, slat, slng)
            idx = int(bearing / sector_width) % NUM_FACADE_SECTORS
            sector_energy[idx] += 10 ** (db_val / 10)

    sector_db = [10 * math.log10(e) if e > 0 else 0 for e in sector_energy]
    if any(s > 0 for s in sector_db):
        active = [s for s in sector_db if s > 0]
        feats["sector_max_db"] = max(sector_db)
        feats["sector_min_db"] = min(active) if active else 0
        feats["sector_range_db"] = max(sector_db) - (min(active) if active else 0)
        feats["sector_std_db"] = np.std(active)
        feats["sectors_active"] = len(active)
    else:
        feats["sector_max_db"] = 0
        feats["sector_min_db"] = 0
        feats["sector_range_db"] = 0
        feats["sector_std_db"] = 0
        feats["sectors_active"] = 0

    # --- Physics model output as features ---
    try:
        phys = noise_score(lat, lng, radius_m)
        feats["physics_lden"] = phys["lden_db"]
        feats["physics_road_db"] = phys["road_db"]
        feats["physics_rail_db"] = phys.get("rail_db", 0)
        feats["physics_max_facade"] = phys.get("lden_max_facade", phys["lden_db"])
        feats["physics_min_facade"] = phys.get("lden_min_facade", phys["lden_db"])
        feats["physics_score"] = phys["score"]
    except Exception:
        feats["physics_lden"] = 0
        feats["physics_road_db"] = 0
        feats["physics_rail_db"] = 0
        feats["physics_max_facade"] = 0
        feats["physics_min_facade"] = 0
        feats["physics_score"] = 0

    return feats


def load_ambient_data(csv_path: str) -> list[dict]:
    """Load Ambient Maps ground truth."""
    buildings = []
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            geom = row["geometry"].replace("POINT (", "").replace(")", "").split()
            lat, lng = float(geom[0]), float(geom[1])
            sf = lambda v: float(v) if v else 0
            rd_d = sf(row["sp_rd_max_d"])
            rd_e = sf(row["sp_rd_max_e"])
            rd_n = sf(row["sp_rd_max_n"])
            rd_min_d = sf(row["sp_rd_min_d"])
            rd_min_e = sf(row["sp_rd_min_e"])
            rd_min_n = sf(row["sp_rd_min_n"])

            if rd_d > 0:
                lden_max = 10 * math.log10(
                    (12 * 10 ** (rd_d / 10) + 4 * 10 ** ((rd_e + 5) / 10)
                     + 8 * 10 ** ((rd_n + 10) / 10)) / 24
                )
            else:
                lden_max = 0

            if rd_min_d > 0:
                lden_min = 10 * math.log10(
                    (12 * 10 ** (rd_min_d / 10) + 4 * 10 ** ((rd_min_e + 5) / 10)
                     + 8 * 10 ** ((rd_min_n + 10) / 10)) / 24
                )
            else:
                lden_min = 0

            buildings.append({
                "lat": lat, "lng": lng,
                "lden_max": lden_max, "lden_min": lden_min,
                "pid": row["building_pid"],
            })
    return buildings


def load_all_cities(sample_dir: str = "data/ambient_sample",
                    max_per_city: int = 0) -> list[dict]:
    """Load Ambient data from all available cities."""
    cities = ["melbourne", "sydney", "perth", "adelaide",
              "hobart", "darwin", "canberra"]
    all_buildings = []
    for city in cities:
        path = os.path.join(sample_dir, f"antn_{city}_buildings_.csv")
        if not os.path.exists(path):
            continue
        buildings = load_ambient_data(path)
        buildings = [b for b in buildings if b["lden_max"] > 20]
        if max_per_city > 0 and len(buildings) > max_per_city:
            buildings.sort(key=lambda b: b["lden_max"])
            step = len(buildings) / max_per_city
            buildings = [buildings[int(i * step)] for i in range(max_per_city)]
        for b in buildings:
            b["city"] = city
        all_buildings.extend(buildings)
        print(f"  {city:12s}: {len(buildings)} buildings")
    return all_buildings


def main():
    model_path = "data/noise_ml_model.pkl"
    max_per_city = int(sys.argv[1]) if len(sys.argv) > 1 else 200

    print("=" * 60)
    print(f"Training ML Noise Model (max {max_per_city}/city)")
    print("=" * 60)

    # Load ground truth from all cities
    print("\nLoading cities...")
    ambient = load_all_cities(max_per_city=max_per_city)
    print(f"Total: {len(ambient)} buildings")

    # Extract features
    print("\nExtracting features...")
    all_features = []
    all_targets_max = []
    all_targets_min = []
    city_labels = []
    t0 = time.time()

    for i, b in enumerate(ambient):
        try:
            feats = extract_features(b["lat"], b["lng"])
            all_features.append(feats)
            all_targets_max.append(b["lden_max"])
            all_targets_min.append(b["lden_min"])
            city_labels.append(b.get("city", "unknown"))
        except Exception as e:
            pass
        if (i + 1) % 50 == 0:
            elapsed = time.time() - t0
            print(f"  {i+1}/{len(ambient)} ({elapsed:.0f}s)")

    elapsed = time.time() - t0
    print(f"Features extracted: {len(all_features)} buildings, {elapsed:.0f}s")

    # Convert to arrays
    feature_names = sorted(all_features[0].keys())
    X = np.array([[f[k] for k in feature_names] for f in all_features])
    y_max = np.array(all_targets_max)
    y_min = np.array(all_targets_min)

    print(f"Features: {len(feature_names)}")
    print(f"Target range: {y_max.min():.1f} - {y_max.max():.1f} dB")

    # Physics-only baseline
    physics_lden = X[:, feature_names.index("physics_lden")]
    physics_max_f = X[:, feature_names.index("physics_max_facade")]
    base_mae = mean_absolute_error(y_max, physics_lden)
    base_mae_f = mean_absolute_error(y_max, physics_max_f)
    print(f"\nPhysics baseline (omni vs max): MAE = {base_mae:.2f} dB")
    print(f"Physics baseline (max facade):  MAE = {base_mae_f:.2f} dB")

    # Train with cross-validation
    print("\n--- XGBoost (MAX facade target) ---")
    import xgboost as xgb

    kf = KFold(n_splits=5, shuffle=True, random_state=42)
    cv_maes = []
    cv_biases = []
    cv_w5 = []

    for fold, (train_idx, val_idx) in enumerate(kf.split(X)):
        X_tr, X_val = X[train_idx], X[val_idx]
        y_tr, y_val = y_max[train_idx], y_max[val_idx]

        model = xgb.XGBRegressor(
            n_estimators=200, max_depth=4, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8,
            reg_alpha=1.0, reg_lambda=2.0,
            random_state=42,
        )
        model.fit(X_tr, y_tr, eval_set=[(X_val, y_val)], verbose=False)

        pred = model.predict(X_val)
        mae = mean_absolute_error(y_val, pred)
        bias = np.mean(pred - y_val)
        w5 = np.mean(np.abs(pred - y_val) <= 5) * 100
        cv_maes.append(mae)
        cv_biases.append(bias)
        cv_w5.append(w5)
        print(f"  Fold {fold+1}: MAE={mae:.2f} Bias={bias:+.2f} W5={w5:.0f}%")

    print(f"\n  CV Mean: MAE={np.mean(cv_maes):.2f} ± {np.std(cv_maes):.2f}")
    print(f"  CV Mean: Bias={np.mean(cv_biases):+.2f} W5={np.mean(cv_w5):.0f}%")

    # Train final model on all data
    print("\nTraining final model on all data...")
    final_model = xgb.XGBRegressor(
        n_estimators=200, max_depth=4, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8,
        reg_alpha=1.0, reg_lambda=2.0,
        random_state=42,
    )
    final_model.fit(X, y_max)

    # Feature importance
    importances = final_model.feature_importances_
    top_feats = sorted(zip(feature_names, importances), key=lambda x: x[1], reverse=True)
    print("\nTop 10 features:")
    for name, imp in top_feats[:10]:
        print(f"  {name:30s} {imp:.3f}")

    # By-level comparison (physics vs ML, using LOO-style from CV)
    print("\n--- Physics vs ML by Ambient level ---")
    all_preds = np.zeros(len(y_max))
    for train_idx, val_idx in kf.split(X):
        m = xgb.XGBRegressor(
            n_estimators=200, max_depth=4, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8,
            reg_alpha=1.0, reg_lambda=2.0, random_state=42,
        )
        m.fit(X[train_idx], y_max[train_idx], verbose=False)
        all_preds[val_idx] = m.predict(X[val_idx])

    for lo, hi, label in [(0, 50, "<50"), (50, 60, "50-60"), (60, 70, "60-70"), (70, 100, "70+")]:
        mask = (y_max >= lo) & (y_max < hi)
        if mask.sum() == 0:
            continue
        phys_err = physics_max_f[mask] - y_max[mask]
        ml_err = all_preds[mask] - y_max[mask]
        print(f"  {label:6s}: n={mask.sum():3d} | Physics bias={np.mean(phys_err):+5.1f} MAE={np.mean(np.abs(phys_err)):.1f}"
              f" | ML bias={np.mean(ml_err):+5.1f} MAE={np.mean(np.abs(ml_err)):.1f}")

    # Leave-one-city-out cross-validation
    cities_arr = np.array(city_labels)
    unique_cities = sorted(set(city_labels))
    if len(unique_cities) > 1:
        print("\n--- Leave-One-City-Out CV ---")
        print(f"{'City':12s} {'N':>5s} {'Phys Bias':>10s} {'Phys MAE':>9s} {'ML Bias':>9s} {'ML MAE':>8s} {'ML W5':>6s}")
        print("-" * 62)
        loco_preds = np.zeros(len(y_max))
        for city in unique_cities:
            test_mask = cities_arr == city
            train_mask = ~test_mask
            if test_mask.sum() == 0 or train_mask.sum() == 0:
                continue
            m = xgb.XGBRegressor(
                n_estimators=200, max_depth=4, learning_rate=0.05,
                subsample=0.8, colsample_bytree=0.8,
                reg_alpha=1.0, reg_lambda=2.0, random_state=42,
            )
            m.fit(X[train_mask], y_max[train_mask], verbose=False)
            preds = m.predict(X[test_mask])
            loco_preds[test_mask] = preds
            phys_e = physics_max_f[test_mask] - y_max[test_mask]
            ml_e = preds - y_max[test_mask]
            w5 = np.mean(np.abs(ml_e) <= 5) * 100
            print(f"  {city:12s} {test_mask.sum():5d} {np.mean(phys_e):+10.1f} {np.mean(np.abs(phys_e)):9.1f}"
                  f" {np.mean(ml_e):+9.1f} {np.mean(np.abs(ml_e)):8.1f} {w5:5.0f}%")

        # Aggregate LOCO
        phys_e_all = physics_max_f - y_max
        ml_e_all = loco_preds - y_max
        valid = loco_preds != 0
        print("-" * 62)
        print(f"  {'LOCO Total':12s} {valid.sum():5d} {np.mean(phys_e_all[valid]):+10.1f} {np.mean(np.abs(phys_e_all[valid])):9.1f}"
              f" {np.mean(ml_e_all[valid]):+9.1f} {np.mean(np.abs(ml_e_all[valid])):8.1f} {np.mean(np.abs(ml_e_all[valid]) <= 5) * 100:5.0f}%")

    # Save model + metadata
    model_data = {
        "model": final_model,
        "feature_names": feature_names,
        "cv_mae": float(np.mean(cv_maes)),
        "cv_bias": float(np.mean(cv_biases)),
        "n_train": len(X),
        "target": "lden_max_facade",
    }
    with open(model_path, "wb") as f:
        pickle.dump(model_data, f)
    print(f"\nModel saved to {model_path}")
    print(f"Features: {len(feature_names)} | CV MAE: {np.mean(cv_maes):.2f} dB")


if __name__ == "__main__":
    main()
