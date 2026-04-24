"""Flood score cache — nearest-neighbor lookup from pre-computed grids."""

import math

from property_scores.common.config import DATA_DIR

_cache: list[tuple] = []
_loaded = False


def _load():
    global _loaded, _cache
    if _loaded:
        return
    _loaded = True
    try:
        import pandas as pd
    except ImportError:
        return
    for f in DATA_DIR.glob("flood_cache_*.parquet"):
        try:
            df = pd.read_parquet(f)
            valid = df.dropna(subset=["score"])
            _cache.extend(list(valid.itertuples(index=False)))
        except Exception:
            continue


def lookup(lat: float, lng: float, max_dist_m: float = 350) -> dict | None:
    _load()
    if not _cache:
        return None

    max_deg = max_dist_m / 111_320
    best_dist = max_deg
    best = None

    for row in _cache:
        dlat = abs(row.lat - lat)
        if dlat > max_deg:
            continue
        dlng = abs(row.lng - lng)
        if dlng > max_deg * 1.5:
            continue
        d = math.sqrt(dlat ** 2 + dlng ** 2)
        if d < best_dist:
            best_dist = d
            best = row

    if best is None:
        return None

    result = {
        "score": int(best.score),
        "label": best.label,
        "flood_zones": best.flood_zones.split(",") if best.flood_zones else [],
        "state": None,
        "zone_count": len(best.flood_zones.split(",")) if best.flood_zones else 0,
        "cached": True,
        "cache_dist_m": round(best_dist * 111_320),
    }
    if hasattr(best, "hand_m") and best.hand_m is not None:
        result["hand"] = {"hand_m": best.hand_m}
    if hasattr(best, "jrc_flood_cells") and best.jrc_flood_cells is not None:
        result["jrc"] = {
            "flood_cells": int(best.jrc_flood_cells),
            "max_occurrence_pct": int(best.jrc_max_occ) if best.jrc_max_occ else 0,
        }
    return result
