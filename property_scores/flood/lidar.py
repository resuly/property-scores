"""On-demand LiDAR bare-earth elevation for the flood HAND read.

Where a state publishes open (CC BY 4.0) LiDAR-derived elevation we read it live
for the HAND ring, so near-water low lots get survey-grade height instead of the
30 m DEM-H's +/-4-6 m noise. Two provider shapes, both self-contained:

  RASTER (ArcGIS ImageServer, direct point values):
    NSW  — NSW_5M_Elevation, statewide 5 m bare-earth DTM
    QLD  — Elevation/QldDem, 0.5-1 m LiDAR where captured, SRTM 30 m fill else
  CONTOUR (ArcGIS Feature/MapServer, elevation iso-lines -> IDW point value):
    VIC  — Vicmap Elevation, 1 m metro / 5 m rest (LiDAR; the map's source)
    TAS  — theLIST TopographyAndRelief, 5 m (LiDAR)
    WA   — SLIP DPIRD Terrain, 2 m interval over SW/coastal (NOT LiDAR: a
           10 m-grid Land Monitor DEM, ~2000; finer than DEM-H but only medium)

VIC's *raster* DEM (VaaS) is government-licensed, but its 1 m contours are open,
so we interpolate a point value by inverse-distance weighting over the contour
vertices in the cell. A deep 2026-07-15 sweep confirmed SA / ACT / NT publish
their (real, 1 m) LiDAR only as batch downloads, and NT's one live gov server
resets every client — none has an open on-demand endpoint, so they stay DEM-H.
WA's finer Landgate topo contours are subscription-locked; the open DPIRD 2 m
layer is the usable-but-coarser fallback (same locked-raster / open-contour
shape as VIC). Confidence is interval-driven, so only genuine <=1.5 m (VIC
metro LiDAR) reads as survey-grade 'high'; everything else is 'medium'.

HAND samples a 300 m / 16-point ring, so ONE fetch per ~500 m cell (an
exportImage window, or one contour envelope query) feeds the whole ring and is
cached process-wide; the 17 samples reuse it and adjacent addresses share it.
Everything degrades to None on timeout / failure / outside coverage, so the
caller falls back to the local DEM-H 30 m instead of blocking. Fetches use a
tight timeout (some services drop requests under load) and a failed cell is
negatively cached for a short TTL.
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

# Raster point-sample services (exportImage window).
_RASTER = {
    "NSW": ("https://maps.six.nsw.gov.au/arcgis/rest/services/"
            "public/NSW_5M_Elevation/ImageServer"),
    "QLD": ("https://spatial-img.information.qld.gov.au/arcgis/rest/services/"
            "Elevation/QldDem/ImageServer"),
}

# Contour iso-line services (query envelope -> IDW point value). `field` = the
# elevation attribute; the same endpoints the da_leads map's contour layer uses.
_CONTOUR = {
    "VIC": {"url": ("https://services-ap1.arcgis.com/P744lA0wf4LlBZ84/arcgis/rest/"
                    "services/Vicmap_Elevation_METRO_1_to_5_metre/FeatureServer/1/query"),
            "field": "altitude"},
    "TAS": {"url": ("https://services.thelist.tas.gov.au/arcgis/rest/services/"
                    "Public/TopographyAndRelief/MapServer/13/query"),
            "field": "ELEVATION"},
    "WA": {"url": ("https://public-services.slip.wa.gov.au/public/rest/services/"
                   "SLIP_Public_Services/Terrain/MapServer/0/query"),
           "field": "elevation_m"},  # 2 m interval, SW/coastal only, non-LiDAR
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
# Contour interval (m) at/under which we trust the read as survey-grade 'high';
# coarser LiDAR-derived contours still give a better point value than DEM-H but
# are labelled 'medium'. Above _CONTOUR_MAX_STEP we do not bother (no gain).
_CONTOUR_HIGH_STEP = 1.5
_CONTOUR_MAX_STEP = 7.0
_IDW_K = 12              # nearest contour vertices used per interpolated point
# ArcGIS services throttle/deny UA-less requests under load; identify as a
# normal client so a burst of nearby lookups is not mistaken for a scraper.
_UA = "property-scores-flood/1.0 (+https://limontech.net)"

# QLD mosaic items whose name marks SRTM fill rather than a LiDAR capture.
_SRTM_RE = re.compile(r"srtm|_dem[_-]?h|1sec", re.I)

_WIN_CACHE: "OrderedDict[tuple, tuple]" = OrderedDict()  # key -> (bytes|None, ts)
_CAT_CACHE: "OrderedDict[tuple, bool]" = OrderedDict()   # key -> is_lidar
_CTR_CACHE: "OrderedDict[tuple, tuple]" = OrderedDict()  # key -> (verts|None, ts)
_LOCK = threading.Lock()


def covered(state) -> bool:
    """True if the state has an open LiDAR source we read on demand."""
    return state in _RASTER or state in _CONTOUR


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
    url = _RASTER["QLD"] + "/identify?" + urllib.parse.urlencode({
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
    url = _RASTER[state] + "/exportImage?" + urllib.parse.urlencode({
        "bbox": f"{xmin},{ymin},{xmax},{ymax}", "bboxSR": 4326, "imageSR": 4326,
        "size": f"{ncol},{nrow}", "format": "tiff", "pixelType": "F32",
        "noData": "-9999", "interpolation": "RSP_BilinearInterpolation",
        "f": "image"})
    raw = _http(url)
    _lru_put(_WIN_CACHE, key, (raw, now))
    return raw


class Window:
    """Raster (NSW/QLD) elevation window; sample the point + ring off it, then
    close(). The bytes are shared/cached, but each Window opens its own handle so
    concurrent flood requests never race on one GDAL cursor. `source` /
    `uncertain_thresh` let the caller label the read and trust low relief."""

    source = "lidar_5m"       # ~5 m bare-earth DTM -> high confidence
    uncertain_thresh = 1.0    # trust relief down to 1 m (LiDAR noise is sub-m)

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


def _contour_verts(lat, lng, state):
    """Contour vertices [(lat, lng, alt), ...] in the point's cell + margin, from
    the state's open iso-line service. Cached per cell (positive / negative TTL)."""
    key = (state,) + _cell(lat, lng)
    now = time.time()
    with _LOCK:
        if key in _CTR_CACHE:
            payload, ts = _CTR_CACHE[key]
            if payload is not None or (now - ts) < _NEG_TTL:
                _CTR_CACHE.move_to_end(key)
                return payload
    ci, cj = _cell(lat, lng)
    lat0, lat1 = ci * _CELL_DEG, (ci + 1) * _CELL_DEG
    lng0, lng1 = cj * _CELL_DEG, (cj + 1) * _CELL_DEG
    coslat = max(math.cos(math.radians(lat)), 0.2)
    mlat = _RING_MARGIN_M / 111_320.0
    mlng = _RING_MARGIN_M / (111_320.0 * coslat)
    cfg = _CONTOUR[state]
    field = cfg["field"]
    env = f"{lng0 - mlng},{lat0 - mlat},{lng1 + mlng},{lat1 + mlat}"
    url = cfg["url"] + "?" + urllib.parse.urlencode({
        "where": "1=1", "geometry": env, "geometryType": "esriGeometryEnvelope",
        "inSR": 4326, "outSR": 4326, "spatialRel": "esriSpatialRelIntersects",
        "outFields": field, "returnGeometry": "true",
        "resultRecordCount": 2000, "f": "json"})
    raw = _http(url)
    verts = None
    if raw:
        try:
            d = json.loads(raw)
            verts = []
            for f in d.get("features", []):
                a = (f.get("attributes") or {}).get(field)
                if a is None:
                    continue
                a = float(a)
                for path in (f.get("geometry") or {}).get("paths", []):
                    for x, y in path:
                        verts.append((y, x, a))
        except Exception as e:
            logger.debug("contour parse failed: %s", e)
            verts = None
    _lru_put(_CTR_CACHE, key, (verts, now))
    return verts


class ContourWindow:
    """VIC/TAS elevation from open contour iso-lines: IDW over the contour
    vertices in the cell gives a point value, the local drainage is just the
    lowest contour nearby. Confidence follows the observed contour interval —
    1 m (VIC metro) is survey-grade 'high'; coarser is a better point value than
    DEM-H but labelled 'medium'."""

    def __init__(self, verts):
        self._v = verts
        alts = sorted({v[2] for v in verts})
        step = min((b - a for a, b in zip(alts, alts[1:]) if b > a), default=None)
        self.step = step
        if step is not None and step <= _CONTOUR_HIGH_STEP:
            self.source = "lidar_contour_1m"      # high (VIC metro 1 m LiDAR)
            self.uncertain_thresh = 1.0
        else:
            # medium: finer than DEM-H but not survey-grade. Mixed provenance
            # (VIC-rural/TAS are LiDAR, WA is a 10 m-grid DEM), so no 'lidar' tag.
            self.source = "contour_med"
            self.uncertain_thresh = max(2.5, (step or 5.0) / 2.0)

    def elev(self, lat, lng):
        coslat = math.cos(math.radians(lat))
        near = []  # (dist2, alt)
        for vlat, vlng, alt in self._v:
            dx = (vlng - lng) * coslat
            dy = vlat - lat
            d2 = dx * dx + dy * dy
            if d2 < 1e-14:
                return alt
            near.append((d2, alt))
        if not near:
            return None
        near.sort(key=lambda t: t[0])
        num = den = 0.0
        for d2, alt in near[:_IDW_K]:
            w = 1.0 / (d2 * d2)  # inverse-distance^2 weighting
            num += w * alt
            den += w
        return num / den if den else None

    def close(self):
        self._v = None


def open_window(lat, lng, state):
    """A point+ring elevation window for the state, or None outside coverage /
    on fetch failure / (QLD) SRTM fill / (contour) too coarse to beat DEM-H.

    Duck-typed result exposes elev(lat,lng), close(), source, uncertain_thresh."""
    if state in _RASTER:
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
    if state in _CONTOUR:
        verts = _contour_verts(lat, lng, state)
        # Need enough vertices spanning >=2 contour levels to interpolate.
        if not verts or len({v[2] for v in verts}) < 2 or len(verts) < 20:
            return None
        win = ContourWindow(verts)
        if win.step is None or win.step > _CONTOUR_MAX_STEP:
            return None  # too coarse (e.g. WA 10 m) — no gain over DEM-H
        return win
    return None
