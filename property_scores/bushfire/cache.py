"""Runtime spatial cache for bushfire satellite data.

WorldCover is 10m, DEM is 30m, so results within ~200m are nearly identical.
Cache full score results keyed by rounded coordinates.
"""

import math
import threading
from collections import OrderedDict

_lock = threading.Lock()
_cache: OrderedDict[tuple[int, int], dict] = OrderedDict()
_CACHE_MAX = 5000
_GRID = 0.003  # ~330m grid cells (WorldCover 10m, DEM 30m -- safe margin)


def _key(lat: float, lng: float) -> tuple[int, int]:
    return (round(lat / _GRID), round(lng / _GRID))


def get(lat: float, lng: float) -> dict | None:
    k = _key(lat, lng)
    with _lock:
        cached = _cache.get(k)
        if cached is not None:
            _cache.move_to_end(k)
    if cached is None:
        return None
    result = dict(cached)
    result["cached"] = True
    return result


def put(lat: float, lng: float, result: dict):
    if result.get("score") is None:
        return
    k = _key(lat, lng)
    with _lock:
        _cache[k] = dict(result)
        if len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
