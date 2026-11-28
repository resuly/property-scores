"""Read the per-city aircraft overflight grids and answer point/grid queries.

Ported from the limon-ops prototype (labs/aircraft-activity/aa_core.py); the
JSON shapes of describe() / lookup() / grid() follow it so a front end written
against the prototype works unchanged. Differences, all deliberate:

* Data directory comes from AIRCRAFT_ACTIVITY_DIR. Unset or missing means the
  feature reports "unavailable"; nothing here raises at import or startup.
* Coverage labels are enforced, not only reported. A city labelled
  ``insufficient`` (receivers do not see low traffic there) returns no numbers
  at all; a city labelled ``large_aircraft_only`` returns jet counts only and
  says light aircraft and helicopters are undercounted.
* Memory. The 14 cities are ~590 MB once decompressed (the 24-hour array is
  ~70% of it), so nothing is loaded eagerly. Per city, arrays come either from
  an unpacked ``.npy`` directory opened with mmap (``scripts/
  aircraft_activity_unpack.py`` writes it; resident cost is the pages a query
  touches and the page cache is shared between workers) or, when that
  directory is absent, decompressed from the ``.npz`` into a small LRU of
  whole cities (AIRCRAFT_ACTIVITY_MAX_CITIES, default 2).

Data contract (see scripts/aircraft_activity/README.md):
  grid_<city>.npz      scalars lat0, lon0, dlat, dlon, ny, nx; float32 [ny, nx]
                       arrays; hourly [ny, nx, 24]. Row i grows north, column j
                       east; cell centre = (lat0 + i*dlat, lon0 + j*dlon).
  grid_<city>_qN.npz   per calendar quarter: passes_per_day, night_per_night,
                       jet_per_day, heli_per_day.
  meta_<city>.json     period, coverage label/note, missing_days, airports.
  helipads_<city>.json landing sites with movement counts.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
from collections import OrderedDict
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

SCALAR_KEYS = ("lat0", "lon0", "dlat", "dlon", "ny", "nx")
METRIC_KEYS = (
    "passes_per_day",
    "night_per_night",
    "weekday_per_day",
    "weekend_per_day",
    "median_agl_ft",
    "heli_per_day",
    "heli_night_per_night",
    "jet_per_day",
    "light_per_day",
)
QUARTER_KEYS = ("passes_per_day", "night_per_night", "jet_per_day", "heli_per_day")
QUARTERS = ("q1", "q2", "q3", "q4")
HOURLY_METRICS = tuple(f"hourly_{h:02d}" for h in range(24))
# Metrics that stay meaningful where only large aircraft are seen.
JET_ONLY_METRICS = ("jet_per_day",)
HELIPAD_RADIUS_KM = 5.0
# An unnamed site is only shown when it has at least this many movements
# (arrivals + departures) over the period: a handful of touchdowns is a
# paddock or a one-off job, not a landing site. Named sites always show.
PUBLIC_MIN_MOVEMENTS_UNNAMED = 20
UNNAMED_SITE = "Unidentified landing site"
# name_kind values (from the naming step) that settle the public kind.
NAME_KIND_TO_KIND = {"hospital": "hospital", "other": "other", "airport": "other"}
GRID_MAX_CELLS = 480
EARTH_R_M = 6371008.8

COVERAGE_OK = "ok"
COVERAGE_LARGE_ONLY = "large_aircraft_only"
COVERAGE_INSUFFICIENT = "insufficient"

SOURCE = "adsb.lol"
LICENCE = "ODbL-1.0"
LICENCE_URL = "https://opendatacommons.org/licenses/odbl/1-0/"
ATTRIBUTION = ("Aircraft positions from adsb.lol (https://adsb.lol), available "
               "under the Open Database License (ODbL) 1.0")

INSUFFICIENT_MESSAGE = ("Receiver coverage here is not good enough to count low "
                        "aircraft overflights, so no numbers are given.")
LARGE_ONLY_MESSAGE = ("Receivers here only see larger aircraft reliably: jet "
                      "counts are given, light aircraft and helicopters are "
                      "undercounted and are left out.")
UNAVAILABLE_MESSAGE = "Aircraft activity data is not available on this server."
NOT_COVERED_MESSAGE = "This location is outside the areas covered by the data."


def data_dir_from_env() -> Path | None:
    raw = os.environ.get("AIRCRAFT_ACTIVITY_DIR", "").strip()
    return Path(raw).expanduser() if raw else None


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_M * math.asin(min(1.0, math.sqrt(a)))


def clean(value):
    """Recursively convert numpy types and non-finite floats into JSON-safe values."""
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, np.ndarray):
        return [clean(v) for v in value.tolist()]
    if isinstance(value, (np.floating, float)):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    return value


def helipad_movements(h) -> int:
    return (h.get("arrivals") or 0) + (h.get("departures") or 0)


def helipad_name(h):
    """The site's name, or None when unnamed."""
    name = h.get("name")
    return None if not name or name == UNNAMED_SITE else name


def helipad_kind(h):
    """Public kind: the naming step's name_kind wins when it is definite
    (older files without name_kind keep their kind)."""
    return NAME_KIND_TO_KIND.get(h.get("name_kind"), h.get("kind"))


def is_public_helipad(h) -> bool:
    return (helipad_name(h) is not None
            or helipad_movements(h) >= PUBLIC_MIN_MOVEMENTS_UNNAMED)


def round_sig(x, digits: int = 4):
    if x is None:
        return None
    x = float(x)
    if not math.isfinite(x):
        return None
    if x == 0:
        return 0.0
    return round(x, max(0, digits - 1 - int(math.floor(math.log10(abs(x))))))


class _Arrays:
    """The arrays of one city, either mmap'd .npy files or decompressed npz."""

    def __init__(self, metrics: dict, hourly, quarters: dict, mmapped: bool):
        self.metrics = metrics
        self.hourly = hourly
        self.quarters = quarters
        self.mmapped = mmapped

    def nbytes_resident(self) -> int:
        if self.mmapped:
            return 0
        n = sum(a.nbytes for a in self.metrics.values())
        n += self.hourly.nbytes if self.hourly is not None else 0
        n += sum(a.nbytes for q in self.quarters.values() for a in q.values())
        return n


class CityGrid:
    def __init__(self, city: str, data_dir: Path, store: "Store"):
        self.city = city
        self.data_dir = data_dir
        self._store = store
        self.meta = json.loads((data_dir / f"meta_{city}.json").read_text())
        hp = data_dir / f"helipads_{city}.json"
        self.helipads = json.loads(hp.read_text()) if hp.exists() else []
        with np.load(data_dir / f"grid_{city}.npz") as z:
            missing = [k for k in SCALAR_KEYS if k not in z.files]
            if missing:
                raise ValueError(f"{city}: grid scalars missing {missing}")
            self.lat0 = float(z["lat0"])
            self.lon0 = float(z["lon0"])
            self.dlat = float(z["dlat"])
            self.dlon = float(z["dlon"])
            self.ny = int(z["ny"])
            self.nx = int(z["nx"])
            self.metric_names = tuple(k for k in METRIC_KEYS if k in z.files)
            self.has_hourly = "hourly" in z.files
        self.coverage = self.meta.get("coverage") or COVERAGE_INSUFFICIENT
        self._scale_cache: dict[str, float] = {}

    # ---- geometry -------------------------------------------------------
    @property
    def bounds(self):
        """[west, south, east, north] of the outer edge of the grid cells."""
        return [
            self.lon0 - self.dlon / 2,
            self.lat0 - self.dlat / 2,
            self.lon0 + (self.nx - 0.5) * self.dlon,
            self.lat0 + (self.ny - 0.5) * self.dlat,
        ]

    def cell_center(self, i: int, j: int):
        return (self.lat0 + i * self.dlat, self.lon0 + j * self.dlon)

    def cell_index(self, lat: float, lng: float):
        i = int(math.floor((lat - self.lat0) / self.dlat + 0.5))
        j = int(math.floor((lng - self.lon0) / self.dlon + 0.5))
        if 0 <= i < self.ny and 0 <= j < self.nx:
            return i, j
        return None

    def contains(self, lat: float, lng: float) -> bool:
        return self.cell_index(lat, lng) is not None

    def centre(self):
        w, s, e, n = self.bounds
        return ((s + n) / 2, (w + e) / 2)

    # ---- arrays ---------------------------------------------------------
    def arrays(self) -> _Arrays:
        return self._store._arrays_for(self)

    def allowed_metrics(self) -> tuple[str, ...]:
        if self.coverage == COVERAGE_OK:
            return self.metric_names
        if self.coverage == COVERAGE_LARGE_ONLY:
            return tuple(m for m in JET_ONLY_METRICS if m in self.metric_names)
        return ()

    def has_metric(self, metric: str) -> bool:
        if metric in HOURLY_METRICS:
            return self.has_hourly and self.coverage == COVERAGE_OK
        return metric in self.allowed_metrics()

    def _array(self, metric: str):
        a = self.arrays()
        if metric in a.metrics:
            return a.metrics[metric]
        if a.hourly is not None and metric in HOURLY_METRICS:
            return a.hourly[:, :, int(metric[-2:])]
        raise KeyError(metric)

    def metric_scale(self, metric: str) -> float:
        """Upper value for the (log) colour scale: the city maximum."""
        if metric not in self._scale_cache:
            a = np.asarray(self._array(metric))
            v = a[np.isfinite(a) & (a > 0)]
            self._scale_cache[metric] = float(v.max()) if v.size else 0.0
        return self._scale_cache[metric]

    # ---- descriptions ---------------------------------------------------
    def meta_summary(self):
        m = self.meta
        missing = m.get("missing_days") or []
        return {
            "city": m.get("city", self.city),
            "key": self.city,
            "date_start": m.get("date_start"),
            "date_end": m.get("date_end"),
            "n_days": m.get("n_days"),
            "n_weekdays": m.get("n_weekdays"),
            "n_weekend_days": m.get("n_weekend_days"),
            "missing_days_count": len(missing),
            "radius_m": m.get("radius_m"),
            "ceiling_ft_agl": m.get("ceiling_ft_agl"),
            "timezone": m.get("timezone"),
            "source": m.get("source"),
            "fixture": bool(m.get("fixture", False)),
            "coverage": self.coverage,
            "coverage_note": m.get("coverage_note"),
        }

    def _public_helipads(self):
        # Helipad counts are helicopter movements: only where helicopters are seen.
        # Every public list (helipads, lookup, cities, counts) goes through here.
        if self.coverage != COVERAGE_OK:
            return []
        public = [h for h in self.helipads if is_public_helipad(h)]
        # One name, one marker: a minor cluster (< 20 movements) that carries
        # the same name as a busier site is the same pad seen twice.
        best = {}
        for h in public:
            key = (helipad_name(h) or "").strip().lower()
            if key and helipad_movements(h) > best.get(key, -1):
                best[key] = helipad_movements(h)
        out = []
        for h in public:
            key = (helipad_name(h) or "").strip().lower()
            mv = helipad_movements(h)
            if key and mv < PUBLIC_MIN_MOVEMENTS_UNNAMED and mv < best[key]:
                continue
            out.append(h)
        return out

    def describe(self, include_helipads: bool = True):
        d = self.meta_summary()
        d["bounds"] = self.bounds
        d["centre"] = list(self.centre())
        d["cell_size_deg"] = [self.dlat, self.dlon]
        d["shape"] = [self.ny, self.nx]
        d["airports"] = self.public_airports()
        allowed = self.allowed_metrics()
        d["metrics"] = sorted(allowed)
        d["scales"] = {k: round_sig(self.metric_scale(k)) for k in allowed if k != "median_agl_ft"}
        d["has_hourly"] = self.has_hourly and self.coverage == COVERAGE_OK
        d["helipads"] = self.public_helipads() if include_helipads else None
        d["helipad_count"] = len(self._public_helipads())
        if self.coverage == COVERAGE_INSUFFICIENT:
            d["message"] = INSUFFICIENT_MESSAGE
        elif self.coverage == COVERAGE_LARGE_ONLY:
            d["message"] = LARGE_ONLY_MESSAGE
        return d

    def public_airports(self):
        """Airports of this region with what the meta knows about them.

        Shown for every coverage label (an `insufficient` city's ratios of 0
        are the evidence for its label). The visible ratios are our counted
        movements over Airservices' published movements for the months in
        `official_month` (low = 100 ft threshold, high = 250 ft). Observed
        movement totals are not in the data contract yet, so those two
        fields are null rather than estimated.
        """
        out = []
        for a in self.meta.get("airports", []) or []:
            item = dict(a)
            item.setdefault("observed_movements_12m", None)
            item.setdefault("observed_movements_per_day", None)
            out.append(item)
        return out

    def public_helipads(self, bbox=None, min_movements: int = 0):
        """Landing sites as map points: compact, filterable.

        name is null for sites OurAirports does not name ("Unidentified
        landing site"). movements = arrivals + departures over the period.
        """
        out = []
        for h in self._public_helipads():
            lat, lon = h.get("lat"), h.get("lon")
            if bbox is not None:
                w, s_, e, n = bbox
                if not (s_ <= lat <= n and w <= lon <= e):
                    continue
            mv = helipad_movements(h)
            if mv < min_movements:
                continue
            kind = helipad_kind(h)
            out.append({
                "name": helipad_name(h),
                "kind": kind,
                "is_hospital": kind == "hospital",
                "lat": lat,
                "lon": lon,
                "movements": mv,
                "arrivals": h.get("arrivals") or 0,
                "departures": h.get("departures") or 0,
                "night_movements": h.get("night_movements") or 0,
            })
        return out

    def nearby_helipads(self, lat, lng, radius_km=HELIPAD_RADIUS_KM):
        out = []
        for h in self._public_helipads():
            d = haversine_m(lat, lng, h["lat"], h["lon"]) / 1000
            if d <= radius_km:
                item = dict(h)
                item["distance_km"] = round(d, 2)
                item["movements"] = helipad_movements(h)
                item["kind"] = helipad_kind(h)
                out.append(item)
        out.sort(key=lambda x: x["distance_km"])
        return out

    def nearest_airport(self, lat, lng):
        best = None
        for a in self.meta.get("airports", []) or []:
            d = haversine_m(lat, lng, a["lat"], a["lon"]) / 1000
            if best is None or d < best["distance_km"]:
                best = dict(a)
                best["distance_km"] = round(d, 2)
        return best

    def cell_values(self, i: int, j: int) -> dict:
        """Raw values of one cell (all metrics, hourly, quarters), no gating.

        Internal: the noise estimate needs the hourly profile and the class
        split. Anything public goes through lookup(), which gates by coverage.
        """
        a = self.arrays()
        metrics = {k: float(v[i, j]) for k, v in a.metrics.items()}
        hourly = ([float(x) for x in a.hourly[i, j]] if a.hourly is not None else None)
        quarters = {q: {k: float(v[i, j]) for k, v in qa.items()}
                    for q, qa in a.quarters.items()}
        return {"metrics": metrics, "hourly": hourly, "quarters": quarters}

    def quarter_range(self, quarters: dict, key: str):
        vals = [q[key] for q in quarters.values() if key in q and math.isfinite(q[key])]
        if not vals:
            return None
        return {"min": min(vals), "max": max(vals), "n_quarters": len(vals),
                "by_quarter": {q: quarters[q].get(key) for q in sorted(quarters)}}

    def lookup(self, lat, lng):
        idx = self.cell_index(lat, lng)
        if idx is None:
            return None
        i, j = idx
        clat, clng = self.cell_center(i, j)
        base = {
            "status": self.coverage,
            "query": {"lat": lat, "lng": lng},
            "city": self.meta_summary(),
            "cell": {"i": i, "j": j, "lat": clat, "lng": clng,
                     "offset_m": round(haversine_m(lat, lng, clat, clng), 1)},
            "helipad_radius_km": HELIPAD_RADIUS_KM,
            "attribution": ATTRIBUTION,
            "licence": LICENCE,
        }
        if self.coverage == COVERAGE_INSUFFICIENT:
            base.update({"message": INSUFFICIENT_MESSAGE, "metrics": None,
                         "hourly": None, "quarters": None, "helipads": [],
                         "nearest_airport": None})
            return base
        v = self.cell_values(i, j)
        allowed = self.allowed_metrics()
        metrics = {k: x for k, x in v["metrics"].items() if k in allowed}
        if self.coverage == COVERAGE_OK:
            quarters = {"passes_per_day": self.quarter_range(v["quarters"], "passes_per_day"),
                        "jet_per_day": self.quarter_range(v["quarters"], "jet_per_day")}
            hourly = v["hourly"]
        else:
            quarters = {"jet_per_day": self.quarter_range(v["quarters"], "jet_per_day")}
            hourly = None
            base["message"] = LARGE_ONLY_MESSAGE
            base["undercounted"] = ["light", "helicopter"]
        base.update({
            "metrics": metrics,
            "hourly": hourly,
            "quarters": quarters,
            "helipads": self.nearby_helipads(lat, lng),
            "nearest_airport": self.nearest_airport(lat, lng),
        })
        return base

    def grid(self, metric: str, bbox=None, max_cells: int = 180):
        """Block-averaged slice of one metric for drawing a heat layer."""
        arr = self._array(metric)
        max_cells = max(8, min(int(max_cells), GRID_MAX_CELLS))
        if bbox is None:
            i0, i1, j0, j1 = 0, self.ny, 0, self.nx
        else:
            w, s, e, n = bbox
            i0 = max(0, int(math.floor((s - self.lat0) / self.dlat + 0.5)))
            i1 = min(self.ny, int(math.floor((n - self.lat0) / self.dlat + 0.5)) + 1)
            j0 = max(0, int(math.floor((w - self.lon0) / self.dlon + 0.5)))
            j1 = min(self.nx, int(math.floor((e - self.lon0) / self.dlon + 0.5)) + 1)
        if i1 <= i0 or j1 <= j0:
            return {"status": self.coverage, "city": self.city, "metric": metric,
                    "rows": 0, "cols": 0, "values": []}
        stride = max(1, math.ceil(max(i1 - i0, j1 - j0) / max_cells))
        i0 -= i0 % stride
        j0 -= j0 % stride
        sub = np.asarray(arr[i0:i1, j0:j1], dtype=np.float32)
        rows = math.ceil(sub.shape[0] / stride)
        cols = math.ceil(sub.shape[1] / stride)
        pad = np.full((rows * stride, cols * stride), np.nan, dtype=np.float32)
        pad[: sub.shape[0], : sub.shape[1]] = sub
        blocks = pad.reshape(rows, stride, cols, stride)
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            agg = np.nanmean(blocks, axis=(1, 3))
        vals = [round_sig(float(x), 3) for x in agg.ravel()]
        south = self.lat0 + (i0 - 0.5) * self.dlat
        west = self.lon0 + (j0 - 0.5) * self.dlon
        north = south + rows * stride * self.dlat
        east = west + cols * stride * self.dlon
        out = {
            "status": self.coverage,
            "city": self.city,
            "metric": metric,
            "stride": stride,
            "rows": rows,
            "cols": cols,
            "bounds": [west, south, east, north],
            "scale": (round_sig(self.metric_scale(metric))
                      if metric in self.metric_names and metric != "median_agl_ft" else None),
            "order": "row-major, south to north, west to east",
            "values": vals,
            "attribution": ATTRIBUTION,
            "licence": LICENCE,
        }
        if self.coverage == COVERAGE_LARGE_ONLY:
            out["message"] = LARGE_ONLY_MESSAGE
        return out


class Store:
    """All cities in a data directory. Metadata loads eagerly; arrays on demand."""

    def __init__(self, data_dir: Path | str | None, max_cities: int | None = None):
        self.data_dir = Path(data_dir).expanduser() if data_dir else None
        if max_cities is None:
            try:
                max_cities = int(os.environ.get("AIRCRAFT_ACTIVITY_MAX_CITIES", "2"))
            except ValueError:
                max_cities = 2
        self.max_cities = max(1, max_cities)
        self._lock = threading.Lock()
        self._cities: dict[str, CityGrid] = {}
        self._lru: "OrderedDict[str, _Arrays]" = OrderedDict()
        self.errors: dict[str, str] = {}
        self.reload()

    @property
    def available(self) -> bool:
        return bool(self._cities)

    def reload(self):
        found, errors = {}, {}
        d = self.data_dir
        if d is not None and d.is_dir():
            for p in sorted(d.glob("meta_*.json")):
                city = p.stem[len("meta_"):]
                if not (d / f"grid_{city}.npz").exists():
                    errors[city] = "grid file missing"
                    continue
                try:
                    found[city] = CityGrid(city, d, self)
                except Exception as e:  # keep serving the other cities
                    errors[city] = f"{type(e).__name__}: {e}"
        with self._lock:
            self._cities = found
            self.errors = errors
            self._lru.clear()
        if errors:
            logger.warning("aircraft activity: skipped cities %s", errors)

    @property
    def cities(self):
        return dict(self._cities)

    # ---- array loading --------------------------------------------------
    def _unpacked_dir(self, city: str) -> Path:
        return self.data_dir / "unpacked" / city

    def _load_arrays(self, c: CityGrid) -> _Arrays:
        up = self._unpacked_dir(c.city)
        if (up / "COMPLETE").exists():
            metrics = {k: np.load(up / f"{k}.npy", mmap_mode="r") for k in c.metric_names}
            hourly = np.load(up / "hourly.npy", mmap_mode="r") if c.has_hourly else None
            quarters = {}
            for q in QUARTERS:
                qa = {k: np.load(up / f"{q}_{k}.npy", mmap_mode="r")
                      for k in QUARTER_KEYS if (up / f"{q}_{k}.npy").exists()}
                if qa:
                    quarters[q] = qa
            arrays = _Arrays(metrics, hourly, quarters, mmapped=True)
        else:
            with np.load(c.data_dir / f"grid_{c.city}.npz") as z:
                metrics = {k: np.asarray(z[k], dtype=np.float32) for k in c.metric_names}
                hourly = np.asarray(z["hourly"], dtype=np.float32) if c.has_hourly else None
            quarters = {}
            for q in QUARTERS:
                f = c.data_dir / f"grid_{c.city}_{q}.npz"
                if f.exists():
                    with np.load(f) as z:
                        quarters[q] = {k: np.asarray(z[k], dtype=np.float32)
                                       for k in QUARTER_KEYS if k in z.files}
            arrays = _Arrays(metrics, hourly, quarters, mmapped=False)
        for k, a in arrays.metrics.items():
            if a.shape != (c.ny, c.nx):
                raise ValueError(f"{c.city}: {k} has shape {a.shape}, expected {(c.ny, c.nx)}")
        if arrays.hourly is not None and arrays.hourly.shape != (c.ny, c.nx, 24):
            raise ValueError(f"{c.city}: hourly has shape {arrays.hourly.shape}")
        for q, qa in arrays.quarters.items():
            for k, a in qa.items():
                if a.shape != (c.ny, c.nx):
                    raise ValueError(f"{c.city}: {q} {k} has shape {a.shape}")
        return arrays

    def _arrays_for(self, c: CityGrid) -> _Arrays:
        with self._lock:
            hit = self._lru.get(c.city)
            if hit is not None:
                self._lru.move_to_end(c.city)
                return hit
        arrays = self._load_arrays(c)  # outside the lock: decompression is ~0.3 s
        with self._lock:
            self._lru[c.city] = arrays
            self._lru.move_to_end(c.city)
            while len(self._lru) > self.max_cities:
                self._lru.popitem(last=False)
        return arrays

    def resident_bytes(self) -> int:
        with self._lock:
            return sum(a.nbytes_resident() for a in self._lru.values())

    # ---- queries ----------------------------------------------------------
    def city_for(self, lat, lng):
        hits = [c for c in self._cities.values() if c.contains(lat, lng)]
        if not hits:
            return None
        return min(hits, key=lambda c: haversine_m(lat, lng, *c.centre()))

    def lookup(self, lat, lng):
        c = self.city_for(lat, lng)
        if c is None:
            return {
                "status": "not_covered",
                "query": {"lat": lat, "lng": lng},
                "message": NOT_COVERED_MESSAGE,
                "cities": [{"key": k, "city": v.meta.get("city", k), "bounds": v.bounds}
                           for k, v in self._cities.items()],
            }
        return c.lookup(lat, lng)


_store: Store | None = None
_store_key = None
_store_lock = threading.Lock()


def get_store() -> Store:
    """Process-wide store for the directory in AIRCRAFT_ACTIVITY_DIR."""
    global _store, _store_key
    key = str(data_dir_from_env())
    with _store_lock:
        if _store is None or _store_key != key:
            _store = Store(data_dir_from_env())
            _store_key = key
        return _store
