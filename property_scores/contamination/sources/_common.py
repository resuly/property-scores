"""Shared HTTP + geometry helpers for the contamination source adapters.

Fail-closed discipline (2026-08-10 audit, inherited from ``score.py``):
a failed query returns ``None`` and an empty register returns ``[]``. Every
helper here that can fail returns ``None`` rather than an empty container, so
callers keep the two distinguishable all the way up.

Politeness: 10s timeout, one attempt plus one retry per request, 2s backoff
before the retry, browser User-Agent. Retries cover transport-level failures
only (connection errors, non-2xx, undecodable bodies). An HTTP 200 carrying an
upstream error document is a logical failure, not a flaky socket, so it is not
retried.

Wall-clock budget (2026-08-27, latency review): the politeness budget alone
bounds a single request, not a signal. ``_landfill_signal`` chains three of
them and Sands pages up to eight times, so 10s x 2 attempts x N calls put the
contamination branch over the 25s ``/scores`` batch deadline. A caller can
therefore wrap a whole signal in :func:`budget`; every request made inside it
clamps its socket timeout to the time left, shortens the retry backoff to the
time left, and raises :class:`BudgetExceeded` rather than starting a call that
cannot finish. Callers turn that into the existing fail-closed ``status:
"error"`` (no reassuring label, not cached) - a slow register is exactly as
unread as an unreachable one.
"""

import contextvars
import logging
import time as _time
from contextlib import contextmanager

import requests

# Reused rather than re-derived so the adapters cannot drift from the
# already-audited implementations in score.py. score.py is not modified.
from property_scores.contamination.score import (  # noqa: F401
    TIMEOUT,
    _distance_m,
    _features_or_none,
    _inside_polygon_rings,
    _search_envelope,
)

logger = logging.getLogger(__name__)

# Several of these government endpoints sit behind WAFs that answer a bare
# python-requests UA with a challenge page. A plain browser UA avoids it.
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept": "application/json, */*"}

# 2026-08-27: was 0.5s. These are unauthenticated government services and the
# retry only fires on a transport-level failure, i.e. exactly when the far end
# is already unhappy; 0.5s is a hammer, not a backoff. Safe to lengthen now
# that a budget bounds the total wall clock regardless.
_RETRY_SLEEP_S = 2.0

# Indirection so tests can drive the budget from a fake clock instead of
# actually sleeping through it.
_now = _time.monotonic

_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "contam_fetch_deadline", default=None)


class BudgetExceeded(RuntimeError):
    """The enclosing :func:`budget` ran out before this request could run."""


@contextmanager
def budget(seconds: float):
    """Bound every fetch made inside this block to ``seconds`` of wall clock.

    Nesting replaces the deadline for the duration of the inner block; the
    adapters do not nest, and a signal owns exactly one budget.

    The deadline lives in a :class:`~contextvars.ContextVar`, so a thread that
    did not inherit the context simply runs unbudgeted rather than inheriting a
    stale deadline. A worker thread that SHOULD share the budget must be
    submitted through :func:`child_context` (see ``vic_wfs.landfills_near``).
    """
    token = _deadline.set(_now() + seconds)
    try:
        yield
    finally:
        _deadline.reset(token)


def remaining_budget() -> float | None:
    """Seconds left in the enclosing budget, or ``None`` when unbudgeted."""
    deadline = _deadline.get()
    return None if deadline is None else deadline - _now()


def check_budget() -> None:
    """Raise :class:`BudgetExceeded` if the enclosing budget is spent."""
    left = remaining_budget()
    if left is not None and left <= 0:
        raise BudgetExceeded("contamination fetch budget exhausted")


def child_context() -> contextvars.Context:
    """A context copy carrying the current deadline into one worker thread.

    ``ThreadPoolExecutor`` does not propagate context, and a single
    ``Context`` object cannot be entered by two threads at once, so callers
    take one copy per submitted call.
    """
    return contextvars.copy_context()


def _effective_timeout(timeout: float) -> float:
    """Socket timeout clamped to the budget. Raises if the budget is spent.

    Single clock read: with check_budget() and remaining_budget() reading the
    clock separately, a deadline landing between the two reads produced a
    zero/negative timeout, and requests raises ValueError on those (caught by
    neither RequestException nor BudgetExceeded handlers; delta review P2).
    """
    left = remaining_budget()
    if left is not None and left <= 0:
        raise BudgetExceeded("wall-clock budget exhausted")
    return timeout if left is None else min(timeout, left)


def _backoff() -> None:
    """Sleep before a retry, never past the budget."""
    check_budget()
    left = remaining_budget()
    delay = _RETRY_SLEEP_S if left is None else min(_RETRY_SLEEP_S, left)
    if delay > 0:
        _time.sleep(delay)


def fetch_json(url: str, params: dict | None = None, timeout: int = TIMEOUT):
    """GET ``url`` and return the decoded JSON body, or ``None`` on failure.

    ``None`` covers every way the request can fail to produce a body we can
    read: connection errors, timeouts, non-2xx status, and undecodable JSON.
    It deliberately does NOT distinguish "empty result" - that judgement
    belongs to the caller, which inspects the decoded body.

    One attempt plus one retry, per the politeness budget for these
    unauthenticated government services. Inside a :func:`budget` block the
    socket timeout is clamped to the time left and :class:`BudgetExceeded` is
    raised instead of starting an attempt that cannot finish.
    """
    last_error = None
    for attempt in range(2):
        if attempt:
            _backoff()
        try:
            resp = requests.get(url, params=params,
                                timeout=_effective_timeout(timeout),
                                headers=HEADERS)
        except requests.RequestException as exc:
            last_error = exc
            continue
        if not resp.ok:
            # A non-2xx is an outage, not an empty register.
            last_error = f"HTTP {resp.status_code}"
            continue
        try:
            return resp.json()
        except ValueError as exc:
            last_error = exc
            continue
    logger.warning("fetch_json failed for %s: %s", url, last_error)
    return None


def fetch_bytes(url: str, timeout: int = TIMEOUT) -> bytes | None:
    """GET ``url`` and return the raw body, or ``None`` on failure.

    Same retry budget and same fail-closed meaning as :func:`fetch_json`.
    Used for the whole-of-state GeoJSON bundle that SA publishes as a file
    rather than as a query service.
    """
    last_error = None
    for attempt in range(2):
        if attempt:
            _backoff()
        try:
            resp = requests.get(url, timeout=_effective_timeout(timeout),
                                headers=HEADERS)
        except requests.RequestException as exc:
            last_error = exc
            continue
        if not resp.ok:
            last_error = f"HTTP {resp.status_code}"
            continue
        return resp.content
    logger.warning("fetch_bytes failed for %s: %s", url, last_error)
    return None


def geojson_features_or_none(data) -> list | None:
    """Feature list of a successful GeoJSON/Esri body, else ``None``.

    Thin wrapper over ``score._features_or_none`` so that the adapters read
    the same way whichever flavour of upstream they talk to. GeoServer WFS
    reports failures as ``{"exceptions": [...]}`` under HTTP 200 and ArcGIS
    REST as ``{"error": {...}}``; both are already handled there.
    """
    return _features_or_none(data)


def point_coords(feature: dict) -> tuple[float, float] | None:
    """Return ``(lat, lng)`` of a GeoJSON Point feature, or ``None``.

    ``None`` here means "this feature is not shaped the way we expect", which
    the callers escalate into a whole-query ``None``: a register that changed
    shape has not been searched, whatever the status code said.
    """
    if not isinstance(feature, dict):
        return None
    geom = feature.get("geometry")
    if not isinstance(geom, dict) or geom.get("type") != "Point":
        return None
    coords = geom.get("coordinates")
    if not isinstance(coords, (list, tuple)) or len(coords) < 2:
        return None
    try:
        lng, lat = float(coords[0]), float(coords[1])
    except (TypeError, ValueError):
        return None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
        return None
    return lat, lng


def polygon_rings(geometry) -> list[list] | None:
    """Flatten a GeoJSON Polygon/MultiPolygon into a list of rings.

    Rings keep GeoJSON ``[lng, lat]`` ordering, which is what
    ``score._inside_polygon_rings`` expects. Returns ``None`` for anything
    that is not a polygon we can read.
    """
    if not isinstance(geometry, dict):
        return None
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if not isinstance(coords, (list, tuple)):
        return None
    if gtype == "Polygon":
        polygons = [coords]
    elif gtype == "MultiPolygon":
        polygons = list(coords)
    else:
        return None
    rings: list[list] = []
    for polygon in polygons:
        if not isinstance(polygon, (list, tuple)):
            return None
        for ring in polygon:
            if not isinstance(ring, (list, tuple)) or len(ring) < 3:
                return None
            clean = []
            for point in ring:
                if not isinstance(point, (list, tuple)) or len(point) < 2:
                    return None
                try:
                    clean.append((float(point[0]), float(point[1])))
                except (TypeError, ValueError):
                    return None
            rings.append(clean)
    return rings or None


def polygon_distance_m(lat: float, lng: float, rings: list[list]) -> float:
    """0.0 when the point is inside, else distance to the nearest vertex.

    Vertex distance is a conservative over-estimate of the true distance to
    the boundary (never an under-estimate by more than the vertex spacing);
    these registers are cadastre-resolution polygons with dense vertices, so
    it is close enough for a proximity band and needs no segment maths.
    """
    if _inside_polygon_rings(lat, lng, rings):
        return 0.0
    best = float("inf")
    for ring in rings:
        for point_lng, point_lat in ring:
            best = min(best, _distance_m(lat, lng, point_lat, point_lng))
    return best
