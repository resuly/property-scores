"""Modelled noise field on a grid around a point.

The DA Leads map paints this by calling /scores/noise once per choropleth cell
from the browser. The licensed property API needs the same thing for ONE
address as data, and doing that fan-out over HTTP from the web process was the
wrong shape twice over: 49 requests trip the per-IP rate limit on
/scores/noise, and every hop out of this process is a chance for the caller to
end up on a different model path than production.

Building the grid HERE fixes both. It runs inside the service that already
holds NOISE_TRANSFER / NOISE_QUIET_RECAL / NOISE_RAIL_RECAL and the rasters the
transfer model needs, so a grid node cannot be computed under a configuration
production does not use. The one thing a caller could still get wrong is
reading the result as uniform when it is not, so `model_path` counts the nodes
by `lden_source` and the caller is told when more than one path contributed.

Nodes go through `noise_score` (the live point model), NOT through
`noise.cache.lookup`. That is deliberate and not an optimisation missed:
the precomputed regional grids hold a 220 m quincunx AREA MEAN and
`lookup` will answer from a node up to 150 m away, so those values are a
different quantity from the point value the same address gets in
`scores.noise`. Measured 2026-08-05 in inner Melbourne, the two disagreed by up
to 6.1 dB at the same coordinate. A surface whose centre contradicts the
address's own score is worse than a slower one. `noise_score` still has its own
24 h result cache underneath, so repeats stay cheap.
"""

from __future__ import annotations

import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError

from property_scores.noise.score import noise_score, _TRANSFER_ENABLED

logger = logging.getLogger(__name__)

# What lden_source the environment says every node SHOULD carry. Reported
# alongside what the nodes actually carried, because "all 49 nodes agree" is
# not the same as "all 49 nodes ran the configured model" and the failure looks
# identical from outside: transfer_lden falls back to physics on any exception,
# so a grid computed with the transfer model unavailable comes back uniform,
# complete, and plausible.
#
# This is not hypothetical. property_scores/noise/transfer.py resolves its
# parquet inputs from `Path(__file__).parent.parent.parent / "data"` and
# ignores DATA_DIR, so ANY checkout that is not the deployed one (a git
# worktree, for example) fails to read overture_roads.parquet and silently
# serves physics. Caught exactly that way on 2026-08-05 while building this
# module: local 65.5 physics against production 65.3 transfer for the same
# coordinate, with nothing in the local response saying so.
_EXPECTED_PATH = "transfer" if _TRANSFER_ENABLED else "physics"

# Odd, so the subject sits ON a node instead of between four of them.
CELLS_DEFAULT = 7
CELLS_ALLOWED = (5, 7, 9)
RADIUS_DEFAULT_M = 1500
RADIUS_MIN_M = 200
RADIUS_MAX_M = 2000
# 2 uvicorn workers under MemoryMax=1500M, each fresh node a ~0.5 s DuckDB +
# RandomForest stack. Measured on prod 2026-08-05: 8 concurrent fresh points
# 3.0 s, 24 concurrent 5.5 s with a 1.5 GB RSS excursion. 6 keeps a grid
# responsive without making this the reason the service gets OOM-killed while
# it is also serving the map.
CONCURRENCY = 6
DEADLINE_S = 25.0


def _deg_per_m(lat: float) -> tuple[float, float]:
    """Per-axis metres-to-degrees.

    A degree of longitude covers cos(lat) less ground than a degree of
    latitude; one shared factor makes the window an ellipse and under-covers
    the requested radius east-west.
    """
    return (1.0 / 111_320.0,
            1.0 / (111_320.0 * max(math.cos(math.radians(lat)), 0.1)))


def _node(lat: float, lng: float) -> dict | None:
    try:
        return noise_score(lat, lng)
    except Exception:
        logger.warning("noise surface node failed at %s,%s", lat, lng, exc_info=True)
        return None


def noise_surface(lat: float, lng: float, radius_m: int = RADIUS_DEFAULT_M,
                  cells: int = CELLS_DEFAULT,
                  deadline_s: float = DEADLINE_S) -> dict | None:
    """Grid of modelled Lden around a point.

    Row 0 is the northern edge and column 0 the western edge (row-major, the
    same orientation as the land-cover grid the map already consumes, so one
    loop paints both). The subject is the centre node.
    """
    if cells not in CELLS_ALLOWED:
        cells = CELLS_DEFAULT
    radius_m = int(max(RADIUS_MIN_M, min(radius_m, RADIUS_MAX_M)))

    dlat_per_m, dlng_per_m = _deg_per_m(lat)
    half = (cells - 1) // 2
    step_m = radius_m / half
    dlat = step_m * dlat_per_m
    dlng = step_m * dlng_per_m

    nodes = [(r, c, lat + (half - r) * dlat, lng + (c - half) * dlng)
             for r in range(cells) for c in range(cells)]

    lden = [[None] * cells for _ in range(cells)]
    score = [[None] * cells for _ in range(cells)]
    paths: dict[str, int] = {}
    started = time.monotonic()

    # Not a `with` block: ThreadPoolExecutor.__exit__ joins every running
    # worker, which would make the deadline decorative.
    pool = ThreadPoolExecutor(max_workers=CONCURRENCY)
    try:
        futures = {pool.submit(_node, nlat, nlng): (r, c)
                   for r, c, nlat, nlng in nodes}
        try:
            for fut in as_completed(futures, timeout=deadline_s):
                r, c = futures[fut]
                try:
                    got = fut.result()
                except Exception:
                    continue
                if not isinstance(got, dict):
                    continue
                value = got.get("lden_db")
                if value is None:
                    value = got.get("estimated_db")
                if value is None:
                    continue
                lden[r][c] = round(float(value), 1)
                if got.get("score") is not None:
                    score[r][c] = int(got["score"])
                path = got.get("lden_source") or "physics"
                paths[path] = paths.get(path, 0) + 1
        except (TimeoutError, FuturesTimeoutError):
            logger.warning("noise surface hit its %.0fs deadline at %s,%s",
                           deadline_s, lat, lng)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    filled = sum(1 for row in lden for v in row if v is not None)
    if not filled:
        return None
    missing = cells * cells - filled

    out = {
        "bbox": [round(lng - half * dlng, 6), round(lat - half * dlat, 6),
                 round(lng + half * dlng, 6), round(lat + half * dlat, 6)],
        "nrows": cells,
        "ncols": cells,
        "cell_size_m": int(round(step_m)),
        "radius_m": radius_m,
        "lden_db": lden,
        "score": score,
        "cells_missing": missing,
        "partial": missing > 0,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "model_path": paths,
        "model_path_uniform": len(paths) <= 1,
        "model_path_expected": _EXPECTED_PATH,
        # False means the run did NOT use the model this deployment is
        # configured for. Some nodes legitimately fall back (no DEM or land
        # cover coverage), so this is a flag on the grid, not a refusal; the
        # counts in model_path say how much of it fell back.
        "model_path_as_configured": paths.get(_EXPECTED_PATH, 0) == filled,
    }
    if not out["model_path_as_configured"]:
        logger.error(
            "noise surface at %s,%s expected lden_source=%s but got %s "
            "(check the transfer model's data inputs are readable from THIS "
            "checkout: transfer.py resolves them relative to its own file, "
            "not DATA_DIR)", lat, lng, _EXPECTED_PATH, paths)
    return out
