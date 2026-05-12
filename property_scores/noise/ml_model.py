"""ML residual correction for noise score.

Loads the production XGBoost model (Physics + ML residual → LA50).
Called by score.py after physics computation to apply correction.
"""

import logging
import os
import pickle
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

_MODEL = None
_FEATURE_NAMES = None

MODEL_PATH = Path(__file__).parent.parent.parent / "data" / "noise_ml_model_la50.pkl"


def _load():
    global _MODEL, _FEATURE_NAMES
    if _MODEL is not None:
        return True
    if not MODEL_PATH.exists():
        logger.warning("ML model not found: %s", MODEL_PATH)
        return False
    try:
        with open(MODEL_PATH, "rb") as f:
            data = pickle.load(f)
        _MODEL = data["model"]
        _FEATURE_NAMES = data["feature_names"]
        logger.info("Loaded noise ML model (%d features, mode=%s)",
                     len(_FEATURE_NAMES), data.get("mode"))
        return True
    except Exception as e:
        logger.exception("Failed to load ML model: %s", e)
        return False


def predict_correction(features: dict) -> float | None:
    """Predict residual correction given a feature dict.

    Returns the correction in dB to add to physics_lden,
    or None if the model is not available.
    """
    if not _load():
        return None
    try:
        row = [features.get(k, 0) for k in _FEATURE_NAMES]
        X = np.array([row])
        residual = _MODEL.predict(X)[0]
        return float(residual)
    except Exception as e:
        logger.warning("ML prediction failed: %s", e)
        return None
