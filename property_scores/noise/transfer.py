"""EU->AU transfer noise model (RandomForest + per-state affine calibration).

A geometry-only Random Forest trained on EU (Netherlands + UK) Lden ground truth,
transferred to Australia and re-anchored per state with an affine fit against the
VIC/NSW/SA/WA/TAS/ACT/NT SoundPLAN samples (QLD falls back to the global affine).

The RF predicts a raw EU-scale Lden from 75 geometry/terrain/landcover features
(no measured AADT, no Mapbox — geometry suffices, see the calibration JSON note).
Production schema differs from the POC training tables, so the feature SQL here is
adapted:
  * roads: `overture_roads.parquet` bbox is a STRUCT -> bbox.xmin / bbox.ymin
  * buildings: no clng/clat columns -> ST_X/ST_Y(ST_Centroid(geometry)),
    COALESCE(height, 6.0), bbox bbox.xmin/ymin for the crop
  * pois: ST_X/ST_Y(geometry)
  * DEM / land cover via raster_sample against data/global/{dem,lc}.vrt

Lazy module-level singletons keep the 190 MB RF and the calibration JSON loaded
once per process (load ~3 s), mirroring noise/ml_model.py.
"""

import json
import logging
import math
import os
import pickle
from pathlib import Path

import numpy as np

from property_scores.noise import model_registry as _registry
from property_scores.noise import raster_sample as rs

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent.parent / "data"

# Resolved through the registry (property_scores/noise/model_registry.py) so
# "which model is live?" has an answer and rollback is one env var, not a file
# copy. Falls back to the historical flat paths when no registry is present, so
# an unmigrated box keeps serving its model instead of dropping to physics.
# Module-level for backwards compatibility: several scripts import RF_PATH.
_MODEL = _registry.resolve()
RF_PATH = _MODEL["rf"]
CALIB_PATH = _MODEL["calibration"]
MODEL_ID = _MODEL["id"]

# Feature geometry (must match the POC that produced the model)
CLASSES = ["motorway", "trunk", "primary", "secondary", "tertiary",
           "residential", "service", "unclassified"]
RINGS = [50, 100, 200, 400, 800]
LC_CLASSES = {10: "tree", 30: "grass", 40: "crop", 50: "built", 80: "water"}
DEM = str(_DATA_DIR / "global" / "dem.vrt")
LC = str(_DATA_DIR / "global" / "lc.vrt")

MIN_LDEN = 30.0

# --- Quiet-end recalibration (opt-in, NOISE_QUIET_RECAL=1; NSW only) ---
# The per-state affine was fit on SoundPLAN urban FACADE samples. Against clean
# Class-1 LAeq truth (NorthConnex residential, 2026-06-10) it over-reads
# set-back suburban homes by ~+11 dB, while kerbside / activity-centre sensors
# (Lake Macquarie) genuinely need the affine's full lift: the RF raw is
# near-unbiased at set-back homes (mean meas-raw = -1.0, n=9) but ~-8 low at
# kerbs. One raw value cannot serve both receptor contexts, so the relief
# removes the affine's add (back toward raw) only where every gate below says
# "ordinary suburban dwelling", and keeps the affine elsewhere:
#   w_band   raw <= 66 full, 0 at >= 70 -- the loud anchor (Pacific Hwy raws
#            70.4/71.0, measured 77-79) stays exactly on the affine
#   w_built  lc_built_300 <= 0.70 full, 0 at >= 0.80 -- dense built-up keeps
#            the urban-facade calibration (Charlestown piazzas 0.84/0.91;
#            NorthConnex homes are 0.21-0.61)
#   w_poi    poi_n100 <= 25 full, 0 at >= 45 -- activity centres keep it
#   w_dwell  bldg_n100 >= 8 full, 0 at 0 -- relief is for dwelling receptors;
#            open-ground sensors (junction interiors, reserves: Five Islands
#            roundabout has 0 buildings in 100m) keep the facade calibration.
#            This is a domain constraint, not a fitted constant.
# Every gate is a smooth ramp so adjacent overlay cells cannot flip.
QUIET_RECAL_ENABLED = os.environ.get("NOISE_QUIET_RECAL", "0") == "1"
QUIET_RECAL_STATES = {"NSW"}
_RECAL_RAW_FULL = 66.0
_RECAL_RAW_ZERO = 70.0
_RECAL_BUILT_FULL = 0.70
_RECAL_BUILT_ZERO = 0.80
_RECAL_POI_FULL = 25.0
_RECAL_POI_ZERO = 45.0
_RECAL_DWELL_FULL = 8.0

_RF = None
_CALIB = None
_FEATURE_KEYS = None


def _ramp_down(x: float, full: float, zero: float) -> float:
    """1.0 at/below `full`, 0.0 at/above `zero`, linear between."""
    return max(0.0, min(1.0, (zero - x) / (zero - full)))


def quiet_relief(raw: float, affine_lden: float, built300: float, poi100: float,
                 bldg100: float, state: str | None) -> float:
    """dB to subtract from the affine lden for set-back dwelling receptors.

    0.0 when the flag is off, the state is not covered, or any context gate
    (loud band / dense built-up / activity centre / open ground) vetoes.
    Smooth in every input. Never lifts (affine below raw -> no relief).
    """
    if not QUIET_RECAL_ENABLED or state not in QUIET_RECAL_STATES:
        return 0.0
    add = affine_lden - raw
    if add <= 0:
        return 0.0
    w = (_ramp_down(raw, _RECAL_RAW_FULL, _RECAL_RAW_ZERO)
         * _ramp_down(built300, _RECAL_BUILT_FULL, _RECAL_BUILT_ZERO)
         * _ramp_down(poi100, _RECAL_POI_FULL, _RECAL_POI_ZERO)
         * max(0.0, min(1.0, bldg100 / _RECAL_DWELL_FULL)))
    return add * w


def _load() -> bool:
    """Lazy-load the RF + calibration once. Returns True on success."""
    global _RF, _CALIB, _FEATURE_KEYS
    if _RF is not None:
        return True
    if not RF_PATH.exists() or not CALIB_PATH.exists():
        logger.warning("Transfer model files missing: %s / %s", RF_PATH, CALIB_PATH)
        return False
    try:
        with open(RF_PATH, "rb") as f:
            _RF = pickle.load(f)
        with open(CALIB_PATH) as f:
            _CALIB = json.load(f)
        _FEATURE_KEYS = list(_CALIB["_feature_keys"])
        logger.info("Loaded noise transfer model %s via %s (%d features, %d trees)",
                    MODEL_ID, _MODEL["source"], len(_FEATURE_KEYS),
                    getattr(_RF, "n_estimators", -1))
        return True
    except Exception as e:
        logger.exception("Failed to load transfer model: %s", e)
        _RF = None
        return False


def transfer_feats(db, lat: float, lng: float) -> tuple[dict, bool]:
    """Compute the 75 transfer features for a point against the production parquet.

    Returns (feature_dict, raster_ok). raster_ok is False when DEM or land cover
    sampling falls outside coverage (sentinel defaults), so the caller can fall
    back to physics rather than trust an out-of-domain prediction.
    """
    deg = 0.013
    mpd = 111_320 * math.cos(math.radians(lat))

    # --- Roads (bbox STRUCT in production) ---
    # ST_Distance on linestrings is the cold-path hotspot in dense areas
    # (thousands of segments in the bbox). Compute it ONCE in a subquery and
    # filter on the alias, instead of calling ST_Distance in both SELECT and
    # WHERE — same rows, same distances, half the geometry math (Bo, 2026-07-15).
    rows = db.execute(f"""
        SELECT class, d FROM (
            SELECT class,
                   ST_Distance(geometry, ST_Point({lng},{lat})) * {mpd} AS d
            FROM read_parquet('{_DATA_DIR / "overture_roads.parquet"}')
            WHERE bbox.xmin BETWEEN {lng-deg} AND {lng+deg}
              AND bbox.ymin BETWEEN {lat-deg} AND {lat+deg}
              AND subtype = 'road'
        ) WHERE d < 1000
    """).fetchall()

    f: dict = {}
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

    # --- Buildings (no clng/clat; centroid + COALESCE height) ---
    b = db.execute(f"""
        SELECT COALESCE(height, 6.0) AS h,
               ST_X(ST_Centroid(geometry)) AS clng,
               ST_Y(ST_Centroid(geometry)) AS clat
        FROM read_parquet('{_DATA_DIR / "overture_buildings.parquet"}')
        WHERE bbox.xmin BETWEEN {lng-0.004} AND {lng+0.004}
          AND bbox.ymin BETWEEN {lat-0.003} AND {lat+0.003}
    """).fetchall()
    bd = [(h, math.sqrt(((clng - lng) * mpd) ** 2 + ((clat - lat) * 111_320) ** 2))
          for h, clng, clat in b]
    h100 = [h for h, d in bd if d <= 100]
    f["bldg_n100"] = len(h100)
    f["bldg_n200"] = sum(1 for h, d in bd if d <= 200)
    f["bldg_h_mean100"] = float(np.mean(h100)) if h100 else 0.0
    f["bldg_h_max200"] = max((h for h, d in bd if d <= 200), default=0.0)
    f["canyon"] = (f["bldg_h_mean100"] / max(nm, 5)) if h100 else 0.0

    # --- POIs (ST_X/ST_Y of geometry) ---
    p = db.execute(f"""
        SELECT ST_X(geometry) AS plng, ST_Y(geometry) AS plat
        FROM read_parquet('{_DATA_DIR / "overture_pois.parquet"}')
        WHERE bbox.xmin BETWEEN {lng-0.006} AND {lng+0.006}
          AND bbox.ymin BETWEEN {lat-0.0045} AND {lat+0.0045}
    """).fetchall()
    pd = [math.sqrt(((plng - lng) * mpd) ** 2 + ((plat - lat) * 111_320) ** 2)
          for plng, plat in p]
    f["poi_n100"] = sum(1 for d in pd if d <= 100)
    f["poi_n300"] = sum(1 for d in pd if d <= 300)
    f["poi_n500"] = sum(1 for d in pd if d <= 500)

    # --- DEM (single value + window range) ---
    raster_ok = True
    elev = rs.sample(DEM, lat, lng, default=float("nan"))
    if math.isnan(elev):
        raster_ok = False
        elev = 0.0
    f["elev"] = elev
    er = rs.window_stats(DEM, lat, lng, 300)
    if er:
        f["elev_range300"] = (er.get("max", 0) - er.get("mean", 0)) * 2
    else:
        raster_ok = False
        f["elev_range300"] = 0.0

    # --- Land cover fractions ---
    lc = rs.window_stats(LC, lat, lng, 300, categorical=True, classes=list(LC_CLASSES.keys()))
    if not lc:
        raster_ok = False
    for code, name in LC_CLASSES.items():
        f[f"lc_{name}_300"] = lc.get(f"frac_{code}", 0.0)
    lc100 = rs.window_stats(LC, lat, lng, 100, categorical=True, classes=[50])
    f["lc_built_100"] = lc100.get("frac_50", 0.0)

    return f, raster_ok


def transfer_lden(db, lat: float, lng: float, state: str | None) -> tuple[float, float, bool]:
    """Predict calibrated Lden for a point via the EU transfer RF.

    Returns (lden, raw, raster_ok):
      raw  = RF prediction on the EU scale
      lden = per-state (or global) affine recalibration, floored at MIN_LDEN
      raster_ok = whether DEM/LC sampling was inside coverage
    Raises if the model is unavailable (caller falls back to physics).
    """
    if not _load():
        raise RuntimeError("transfer model unavailable")
    f, raster_ok = transfer_feats(db, lat, lng)
    X = [[f[k] for k in _FEATURE_KEYS]]
    raw = float(_RF.predict(X)[0])
    cal = (_CALIB["states"].get(state) if state else None) or _CALIB["global_affine"]
    lden = cal["slope"] * raw + cal["intercept"]
    lden -= quiet_relief(raw, lden, f["lc_built_300"], f["poi_n100"],
                         f["bldg_n100"], state)
    return max(lden, MIN_LDEN), raw, raster_ok


def state_low_confidence(state: str | None) -> bool:
    """True when the per-state calibration is flagged low-confidence (weak or no
    SoundPLAN sample: NSW/NT/WA/QLD under the unified constrained-slope scheme).
    Reads only the calibration JSON (no RF load), so it is safe on the physics
    fallback path too."""
    if not state:
        return False
    cal = _CALIB
    if cal is None:
        try:
            cal = json.loads(CALIB_PATH.read_text())
        except Exception:
            return False
    return bool(cal.get("states", {}).get(state, {}).get("low_confidence", False))
