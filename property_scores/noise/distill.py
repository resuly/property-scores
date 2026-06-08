"""Distilled noise model: a RandomForest that predicts SoundPLAN road Lden from
open geodata features (physics + road-class buffers + buildings + measured AADT).

Trained on the free AURIN 7-city SoundPLAN sample (the slice of the A$100k
professional product) — see scripts/poc_soundplan_distill.py and
logs/da-leads/2026-06-06_noise-model-accuracy-investigation.md.

Purpose: a DE-BIASING calibration layer. Validated (leave-one-city-out) to cut
the physics over-prediction (bias +4.1 -> -1.9 dB, MAE 8.5 -> 6.7) and remove
the dangerous +9..10 dB over-prediction in sparse-AADT states, while tracking
within-city variation better than the (retired) crowdsourced-NoiseCapture model.
NOT a SOTA-accurate model — the free 7-SA2 sample is too sparse to generalise to
r~0.8; this is the best free de-biaser until denser training data is acquired.
"""

import os
import pickle

import numpy as np

from property_scores.common.config import data_path

MODEL_FILE = "noise_rf_soundplan.pkl"

_MODEL = None
_FEATURES = None


def distill_features(db, lat: float, lng: float) -> dict:
    """The canonical feature dict — MUST match training exactly."""
    # imported lazily to avoid heavy deps at module import
    from scripts.train_production_model import extract_features
    from scripts.experiment_retrain_noise import measured_aadt_features
    from scripts.poc_soundplan_distill import sota_road_features
    f = extract_features(lat, lng)
    f.update(measured_aadt_features(db, lat, lng))
    f.update(sota_road_features(db, lat, lng))
    return f


def _load():
    global _MODEL, _FEATURES
    if _MODEL is not None:
        return True
    path = data_path(MODEL_FILE)
    if not path.exists():
        return False
    with open(path, "rb") as fh:
        d = pickle.load(fh)
    _MODEL = d["model"]
    _FEATURES = d["feature_names"]
    return True


def predict(db, lat: float, lng: float) -> float | None:
    """Predicted road Lden (dB) for a coordinate, or None if model missing."""
    if not _load():
        return None
    feats = distill_features(db, lat, lng)
    row = np.array([[feats.get(k, 0) for k in _FEATURES]], dtype=float)
    return float(_MODEL.predict(row)[0])


def predict_from_features(feats: dict) -> float | None:
    """Predict from an already-computed feature dict (avoids re-querying)."""
    if not _load():
        return None
    row = np.array([[feats.get(k, 0) for k in _FEATURES]], dtype=float)
    return float(_MODEL.predict(row)[0])


def train_from_cache(cache_path: str, out_path: str | None = None) -> dict:
    """Train the production RF on ALL points in a poc feature cache (no holdout)."""
    from sklearn.ensemble import RandomForestRegressor
    d = np.load(cache_path, allow_pickle=True)
    feats = list(d["feats"])
    tgt = np.array(d["tgt"], dtype=float)
    keys = sorted(feats[0].keys())
    X = np.array([[f.get(k, 0) for k in keys] for f in feats], dtype=float)
    rf = RandomForestRegressor(n_estimators=500, min_samples_leaf=3,
                               max_features="sqrt", n_jobs=-1, random_state=42)
    rf.fit(X, tgt)
    out = out_path or str(data_path(MODEL_FILE))
    with open(out, "wb") as fh:
        pickle.dump({"model": rf, "feature_names": keys,
                     "n_train": len(tgt), "target": "soundplan_road_lden"}, fh)
    return {"out": out, "n_train": len(tgt), "n_features": len(keys)}


if __name__ == "__main__":
    import glob
    cache = sorted(glob.glob(str(data_path("feature_cache_soundplan_*.npz"))))[-1]
    print("training from", cache)
    print(train_from_cache(cache))
