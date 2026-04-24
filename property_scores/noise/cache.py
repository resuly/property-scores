"""Noise score cache — nearest-neighbor lookup from pre-computed grids."""

import math
from pathlib import Path

from property_scores.common.config import DATA_DIR

_cache: dict[str, list[tuple]] = {}
_loaded = False


def _load_caches():
    global _loaded
    if _loaded:
        return
    _loaded = True

    try:
        import pandas as pd
    except ImportError:
        return

    for f in DATA_DIR.glob("noise_cache_*.parquet"):
        try:
            df = pd.read_parquet(f)
            valid = df.dropna(subset=["score"])
            region = f.stem.replace("noise_cache_", "")
            _cache[region] = list(valid.itertuples(index=False))
        except Exception:
            continue


def lookup(lat: float, lng: float, max_dist_m: float = 150) -> dict | None:
    """Find nearest pre-computed noise score within max_dist_m.

    Returns the cached result dict, or None if no cache hit.
    """
    _load_caches()
    if not _cache:
        return None

    m_per_deg = 111_320 * math.cos(math.radians(lat))
    max_deg = max_dist_m / 111_320

    best_dist = max_deg
    best_row = None

    for rows in _cache.values():
        for row in rows:
            dlat = abs(row.lat - lat)
            if dlat > max_deg:
                continue
            dlng = abs(row.lng - lng)
            if dlng > max_deg * 1.5:
                continue
            d = math.sqrt(dlat ** 2 + (dlng * m_per_deg / 111_320) ** 2)
            if d < best_dist:
                best_dist = d
                best_row = row

    if best_row is None:
        return None

    return {
        "score": int(best_row.score),
        "estimated_db": best_row.estimated_db,
        "road_db": getattr(best_row, "road_db", None),
        "rail_db": getattr(best_row, "rail_db", None),
        "label": getattr(best_row, "label", None),
        "dominant_source": getattr(best_row, "dominant_source", None),
        "cached": True,
        "cache_dist_m": round(best_dist * 111_320),
    }
