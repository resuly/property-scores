"""On-demand LiDAR bare-earth elevation for the flood HAND read (NSW + QLD).

Only NSW and QLD publish open, CC BY 4.0 ArcGIS ImageServers we can read live:
  NSW_5M_Elevation      — statewide 5 m bare-earth DTM (uniform)
  QLD Elevation/QldDem   — 0.5-1 m LiDAR DTM where captured, SRTM 30 m fill else

HAND samples a 300 m / 16-point ring, so ONE exportImage window fetch (not 17
point queries) covers the whole ring. Windows are fetched at a fixed 5 m pixel
(QLD's native 0.5 m is resampled server-side — 5 m is ample for a drainage ring
and keeps the payload small) and cached process-wide per ~500 m grid cell, so
the 17 ring samples reuse one fetch and adjacent addresses share it.

Everything degrades to None on timeout / failure / outside NSW-QLD / QLD SRTM
fill, so the caller falls back to the local DEM-H 30 m and labels the read
'medium' confidence instead of blocking. NSW's service is fast-but-flaky
(intermittent request drops), so fetches use a tight timeout + one retry, and a
failed cell is negatively cached for a short TTL to avoid hammering an endpoint
that is down while still recovering quickly once it is back.
"""

import json
import logging
import math
import re
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict

logger = logging.getLogger(__name__)

_ENDPOINTS = {
    "NSW": ("https://maps.six.nsw.gov.au/arcgis/rest/services/"
            "public/NSW_5M_Elevation/ImageServer"),
    "QLD": ("https://spatial-img.information.qld.gov.au/arcgis/rest/services/"
            "Elevation/QldDem/ImageServer"),
}

_CELL_DEG = 0.005        # ~500 m grid cell — cache-key granularity
_RING_MARGIN_M = 400     # HAND ring is 300 m; pad the window so any point in the
                         # cell keeps its full ring inside the single fetch
_PX_M = 5.0              # sample everything at 5 m (QLD 0.5 m resampled down)
_TIMEOUT = 5.0           # fail fast: DEM-H fallback is instant and honestly
_RETRIES = 0             # flagged 'medium', so a slow/throttled cell is not
                         # worth blocking a live score on a retry
_NEG_TTL = 90.0          # re-try a failed cell after this many seconds
_MAX_CACHE = 256
# ArcGIS ImageServers throttle/deny UA-less requests under load; identify as a
# normal client so a burst of nearby lookups is not mistaken for a scraper.
_UA = "property-scores-flood/1.0 (+https://limontech.net)"

# QLD mosaic items whose name marks SRTM fill rather than a LiDAR capture.
_SRTM_RE = re.compile(r"srtm|_dem[_-]?h|1sec", re.I)

_WIN_CACHE: "OrderedDict[tuple, tuple]" = OrderedDict()  # key -> (bytes|None, ts)
_CAT_CACHE: "OrderedDict[tuple, bool]" = OrderedDict()   # key -> is_lidar
_LOCK = threading.Lock()


def covered(state) -> bool:
    """True if the state has an open LiDAR ImageServer we read on demand."""
    return state in _ENDPOINTS


def _cell(lat, lng):
    return (math.floor(lat / _CELL_DEG), math.floor(lng / _CELL_DEG))


def _lru_put(cache, key, val):
    with _LOCK:
        cache[key] = val
        cache.move_to_end(key)
        while len(cache) > _MAX_CACHE:
            cache.popitem(last=False)


def _http(url):
    """GET with a tight timeout (+ optional retry). Returns bytes or None."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    for _ in range(_RETRIES + 1):
        try:
            return urllib.request.urlopen(req, timeout=_TIMEOUT).read()
        except Exception as e:
            logger.debug("LiDAR http failed: %s", e)
    return None


def _qld_is_lidar(lat, lng) -> bool:
    """QLD only: is the point covered by a real LiDAR capture (not SRTM fill)?

    The QldDem mosaic serves its highest-resolution item, so the point reads
    from LiDAR whenever any non-SRTM capture covers it. One identify per ~500 m
    cell, cached, tells us whether to trust the window as survey-grade.
    """
    key = _cell(lat, lng)
    with _LOCK:
        if key in _CAT_CACHE:
            _CAT_CACHE.move_to_end(key)
            return _CAT_CACHE[key]
    geom = json.dumps({"x": lng, "y": lat, "spatialReference": {"wkid": 4326}})
    url = _ENDPOINTS["QLD"] + "/identify?" + urllib.parse.urlencode({
        "geometry": geom, "geometryType": "esriGeometryPoint", "inSR": 4326,
        "returnGeometry": "false", "returnCatalogItems": "true",
        "maxItemCount": 5, "f": "json"})
    raw = _http(url)
    is_lidar = False
    if raw:
        try:
            d = json.loads(raw)
            v = d.get("value")
            if v not in (None, "", "NoData"):
                feats = ((d.get("catalogItems") or {}).get("features")) or []
                names = []
                for f in feats:
                    a = f.get("attributes", {}) or {}
                    names.append(str(a.get("Name") or a.get("name") or ""))
                is_lidar = any(n and not _SRTM_RE.search(n) for n in names)
        except Exception as e:
            logger.debug("QLD catalog parse failed: %s", e)
    _lru_put(_CAT_CACHE, key, is_lidar)
    return is_lidar


def _window_bytes(lat, lng, state):
    """One exportImage GeoTIFF window covering the point's ~500 m cell + ring
    margin, at 5 m px. Cached per cell (positive forever, negative for a TTL)."""
    key = (state,) + _cell(lat, lng)
    now = time.time()
    with _LOCK:
        if key in _WIN_CACHE:
            payload, ts = _WIN_CACHE[key]
            if payload is not None or (now - ts) < _NEG_TTL:
                _WIN_CACHE.move_to_end(key)
                return payload
    ci, cj = _cell(lat, lng)
    lat0, lat1 = ci * _CELL_DEG, (ci + 1) * _CELL_DEG
    lng0, lng1 = cj * _CELL_DEG, (cj + 1) * _CELL_DEG
    coslat = max(math.cos(math.radians(lat)), 0.2)
    mlat = _RING_MARGIN_M / 111_320.0
    mlng = _RING_MARGIN_M / (111_320.0 * coslat)
    xmin, ymin, xmax, ymax = lng0 - mlng, lat0 - mlat, lng1 + mlng, lat1 + mlat
    ncol = max(16, int((xmax - xmin) * 111_320.0 * coslat / _PX_M))
    nrow = max(16, int((ymax - ymin) * 111_320.0 / _PX_M))
    url = _ENDPOINTS[state] + "/exportImage?" + urllib.parse.urlencode({
        "bbox": f"{xmin},{ymin},{xmax},{ymax}", "bboxSR": 4326, "imageSR": 4326,
        "size": f"{ncol},{nrow}", "format": "tiff", "pixelType": "F32",
        "noData": "-9999", "interpolation": "RSP_BilinearInterpolation",
        "f": "image"})
    raw = _http(url)
    _lru_put(_WIN_CACHE, key, (raw, now))
    return raw


class Window:
    """An opened in-memory elevation window; sample the point + ring off it, then
    close(). The raster bytes are shared/cached, but each Window opens its own
    handle so concurrent flood requests never race on one GDAL cursor."""

    def __init__(self, raw):
        from rasterio.io import MemoryFile
        self._mf = MemoryFile(raw)
        self.src = self._mf.open()
        self.nod = self.src.nodata if self.src.nodata is not None else -9999.0

    def elev(self, lat, lng):
        try:
            row, col = self.src.index(lng, lat)
            if not (0 <= row < self.src.height and 0 <= col < self.src.width):
                return None
            v = float(self.src.read(1, window=((row, row + 1),
                                               (col, col + 1)))[0, 0])
        except Exception:
            return None
        if v == self.nod or v != v:  # nodata or NaN
            return None
        return v

    def close(self):
        try:
            self.src.close()
        finally:
            self._mf.close()


def open_window(lat, lng, state):
    """A Window for the point, or None outside coverage / on fetch failure /
    (QLD) where the point is SRTM fill rather than a LiDAR capture."""
    if not covered(state):
        return None
    if state == "QLD" and not _qld_is_lidar(lat, lng):
        return None
    raw = _window_bytes(lat, lng, state)
    if not raw:
        return None
    try:
        return Window(raw)
    except Exception as e:
        logger.debug("LiDAR window open failed: %s", e)
        return None
