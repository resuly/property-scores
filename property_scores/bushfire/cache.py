"""Runtime spatial cache for bushfire satellite data.

WorldCover is 10m, DEM is 30m, so results within ~200m are nearly identical.
Cache full score results keyed by rounded coordinates.
"""

import math
import threading

_lock = threading.Lock()
_cache: dict[tuple[float, float], dict] = {}
_GRID = 0.003  # ~330m grid cells (WorldCover 10m, DEM 30m — safe margin)


def _key(lat: float, lng: float) -> tuple[int, int]:
    return (round(lat / _GRID), round(lng / _GRID))


def get(lat: float, lng: float) -> dict | None:
    with _lock:
        cached = _cache.get(_key(lat, lng))
    if cached is None:
        return None
    result = dict(cached)
    result["cached"] = True
    return result


def put(lat: float, lng: float, result: dict):
    if result.get("score") is None:
        return
    with _lock:
        if len(_cache) > 5000:
            _cache.clear()
        _cache[_key(lat, lng)] = dict(result)
