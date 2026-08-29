"""
Contamination risk score for Australian properties.

Two signal layers:
1. Official EPA registers — VIC (WFS), NSW (ArcGIS), WA (ArcGIS)
2. Industrial proximity — Overture POI fuel stations, factories, dry cleaners

Score 0-100 where 100 = cleanest / lowest contamination risk.
"""

import logging
import math
import time as _time
from collections import OrderedDict

import requests

from property_scores.common.overture import get_db, pois_near_detailed

logger = logging.getLogger(__name__)

TIMEOUT = 10

# ---------------------------------------------------------------------------
# State detection
# ---------------------------------------------------------------------------

STATE_BOUNDS: list[tuple[str, float, float, float, float]] = [
    ("ACT", -35.93, -35.12, 148.76, 149.40),
    ("VIC", -39.20, -33.98, 140.96, 149.98),
    ("TAS", -43.65, -39.60, 143.50, 148.50),
    ("SA",  -38.10, -25.95, 129.00, 141.00),
    ("NSW", -37.55, -28.15, 140.99, 153.64),
    ("QLD", -29.18, -10.05, 137.95, 153.55),
    ("WA",  -35.13, -13.69, 112.92, 129.00),
    ("NT",  -26.00, -10.97, 129.00, 138.00),
]


def _detect_state(lat: float, lng: float) -> str | None:
    """Shared border-true state detection (common.au_state).

    The old private overlapping-bbox copy routed southern inland NSW
    (Albury, Wagga, Goulburn, Griffith, Cooma) into VIC, so those towns
    were checked against the wrong state register (2026-06-11 audit).
    """
    from property_scores.common.au_state import detect_state
    return detect_state(lat, lng)


# ---------------------------------------------------------------------------
# EPA register queries
# ---------------------------------------------------------------------------

# ArcGIS REST wraps a failed query in {"error": {...}} and GeoServer WFS in
# {"exceptions": [...]}, both under HTTP 200. A green status code is therefore
# not evidence that the register was searched, and it is the more common of the
# two failure modes for these services (2026-08-10 fail-closed audit).
_UPSTREAM_ERROR_KEYS = ("error", "exceptions", "exceptionReport")
_EARTH_RADIUS_M = 6_371_008.8
_EPA_RADIUS_M = 2000


def _features_or_none(data) -> list | None:
    """Return the feature list of a successful query, or None for any payload
    that is not one. A body with no usable `features` list is an outage, not an
    empty register, and the two must stay distinguishable all the way up."""
    if not isinstance(data, dict):
        return None
    if any(k in data for k in _UPSTREAM_ERROR_KEYS):
        return None
    features = data.get("features")
    return features if isinstance(features, list) else None


def _search_envelope(
    lat: float, lng: float, radius_m: int
) -> tuple[float, float, float, float]:
    """Return a conservative WGS84 envelope around an Australian point.

    Longitude degrees cover less ground away from the equator. Reusing the
    latitude delta for longitude made the nominal 2 km search only about
    1.58 km wide east-west in Melbourne. The envelope is only a prefilter;
    `_distance_m` below remains authoritative for the circular cutoff.
    """
    if not (-90 < lat < 90) or not math.isfinite(lng) or radius_m <= 0:
        raise ValueError("finite non-polar coordinates and a positive radius are required")
    angular = radius_m / _EARTH_RADIUS_M
    lat_delta = math.degrees(angular)
    cos_lat = max(abs(math.cos(math.radians(lat))), 1e-12)
    lon_delta = math.degrees(math.asin(min(1.0, math.sin(angular) / cos_lat)))
    return lng - lon_delta, lat - lat_delta, lng + lon_delta, lat + lat_delta


def _distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres between two WGS84 coordinates."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lng2 - lng1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def _nearest_point_on_segment(
    lat: float, lng: float, a_lng: float, a_lat: float, b_lng: float, b_lat: float
) -> tuple[float, float, float]:
    """Return local-planar distance and nearest WGS84 point on a segment."""
    metres_per_lng = 111_320 * math.cos(math.radians(lat))
    ax, ay = (a_lng - lng) * metres_per_lng, (a_lat - lat) * 111_320
    bx, by = (b_lng - lng) * metres_per_lng, (b_lat - lat) * 111_320
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    t = 0.0 if denom == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / denom))
    nx, ny = ax + t * dx, ay + t * dy
    nearest_lng = lng + nx / metres_per_lng
    nearest_lat = lat + ny / 111_320
    return math.hypot(nx, ny), nearest_lng, nearest_lat


def _inside_polygon_rings(lat: float, lng: float, rings: list[list]) -> bool:
    """Even-odd point-in-polygon for ArcGIS multipart polygons with holes."""
    inside = False
    for ring in rings:
        j = len(ring) - 1
        for i, point in enumerate(ring):
            xi, yi = float(point[0]), float(point[1])
            xj, yj = float(ring[j][0]), float(ring[j][1])
            if (yi > lat) != (yj > lat):
                crossing = (xj - xi) * (lat - yi) / (yj - yi) + xi
                if lng < crossing:
                    inside = not inside
            j = i
    return inside


def _nearest_polygon_boundary(
    lat: float, lng: float, rings: list[list]
) -> tuple[float, float, float]:
    candidates = (
        _nearest_point_on_segment(lat, lng, *ring[i - 1], *ring[i])
        for ring in rings for i in range(len(ring))
    )
    return min(candidates, key=lambda candidate: candidate[0])


def _sites_within_radius(sites: list[dict], radius_m: int) -> list[dict] | None:
    """Validate an adapter result and apply the public circular contract."""
    filtered = []
    for site in sites:
        if not isinstance(site, dict):
            return None
        try:
            distance = float(site["distance_m"])
        except (KeyError, TypeError, ValueError):
            return None
        if not math.isfinite(distance) or distance < 0:
            return None
        if distance <= radius_m:
            filtered.append(site)
    return filtered


def _vic_epa_sites(lat: float, lng: float, radius_m: int = 2000) -> list[dict] | None:
    """Query VIC EPA Priority Sites Register via WFS."""
    west, south, east, north = _search_envelope(lat, lng, radius_m)
    bbox = f"{west},{south},{east},{north},EPSG:4326"
    try:
        resp = requests.get(
            "https://opendata.maps.vic.gov.au/geoserver/wfs",
            params={
                "service": "WFS",
                "version": "2.0.0",
                "request": "GetFeature",
                "typeNames": "open-data-platform:psr_point",
                "bbox": bbox,
                "outputFormat": "application/json",
                "count": "50",
            },
            timeout=TIMEOUT,
        )
        if not resp.ok:
            # a non-2xx is an outage, not an empty register: same semantics
            # as the except branch below (2026-08-10 fail-closed audit)
            return None
        features = _features_or_none(resp.json())
        if features is None:
            return None
        results = []
        for f in features:
            if not isinstance(f, dict):
                return None
            geom, props = f.get("geometry"), f.get("properties")
            if not isinstance(geom, dict) or not isinstance(props, dict):
                return None
            coords = geom.get("coordinates")
            if not isinstance(coords, (list, tuple)) or len(coords) < 2:
                return None
            flng, flat = float(coords[0]), float(coords[1])
            if not math.isfinite(flng) or not math.isfinite(flat):
                return None
            dist = _distance_m(lat, lng, flat, flng)
            if dist > radius_m:
                continue
            results.append({
                "name": props.get("address", "Unknown"),
                "issue": props.get("issue", ""),
                "distance_m": round(dist),
                "lng": round(flng, 6),
                "lat": round(flat, 6),
                "source": "VIC EPA PSR",
                "geom": "point",
            })
        return sorted(results, key=lambda x: x["distance_m"])
    except (requests.RequestException, ValueError, KeyError):
        return None  # error must stay distinguishable from a clean register


def _nsw_epa_sites(lat: float, lng: float, radius_m: int = 2000) -> list[dict] | None:
    """Query NSW EPA Contaminated Land Notified Sites."""
    url = (
        "https://mapprod2.environment.nsw.gov.au/arcgis/rest/services"
        "/EPA/Contaminated_land_notified_sites/MapServer/0/query"
    )
    west, south, east, north = _search_envelope(lat, lng, radius_m)
    try:
        resp = requests.get(url, params={
            "geometry": f"{west},{south},{east},{north}",
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "outSR": "4326",
            "outFields": "SiteName,Longitude,Latitude,ManagementClass,ContaminationActivityType",
            "f": "json",
        }, timeout=TIMEOUT)
        if not resp.ok:
            # a non-2xx is an outage, not an empty register: same semantics
            # as the except branch below (2026-08-10 fail-closed audit)
            return None
        features = _features_or_none(resp.json())
        if features is None:
            return None
        results = []
        for feat in features:
            if not isinstance(feat, dict) or not isinstance(feat.get("attributes"), dict):
                return None
            a = feat["attributes"]
            flng = a.get("Longitude")
            flat = a.get("Latitude")
            if flng is None or flat is None:
                return None
            flng, flat = float(flng), float(flat)
            if not math.isfinite(flng) or not math.isfinite(flat):
                return None
            dist = _distance_m(lat, lng, flat, flng)
            if dist > radius_m:
                continue
            results.append({
                "name": a.get("SiteName", "Unknown"),
                "issue": a.get("ContaminationActivityType", ""),
                "distance_m": round(dist),
                "lng": round(flng, 6),
                "lat": round(flat, 6),
                "source": "NSW EPA CLR",
                "geom": "point",
            })
        return sorted(results, key=lambda x: x["distance_m"])
    except (requests.RequestException, ValueError, KeyError):
        return None  # error must stay distinguishable from a clean register


def _wa_epa_sites(lat: float, lng: float, radius_m: int = 2000) -> list[dict] | None:
    """Query WA DWER Contaminated Sites Database."""
    url = (
        "https://public-services.slip.wa.gov.au/public/rest/services"
        "/SLIP_Public_Services/Environment/MapServer/5/query"
    )
    try:
        # returnCentroid: the DWER register is POLYGONS; geometry.x/y is
        # never present for them, which made every one of the 6,877 records
        # read as "Unknown / 2000m / no coords" (standing INSIDE a
        # 'remediation required' site scored 90 Very Clean, 2026-06-11
        # audit). Field names in this service are lowercase.
        resp = requests.get(url, params={
            "geometry": f"{lng},{lat}",
            "geometryType": "esriGeometryPoint",
            "inSR": "4326",
            "outSR": "4326",
            "distance": radius_m,
            "units": "esriSRUnit_Meter",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "*",
            "returnCentroid": "true",
            "f": "json",
        }, timeout=TIMEOUT)
        if not resp.ok:
            # a non-2xx is an outage, not an empty register: same semantics
            # as the except branch below (2026-08-10 fail-closed audit)
            return None
        features = _features_or_none(resp.json())
        if features is None:
            return None
        results = []
        for feat in features:
            if not isinstance(feat, dict):
                return None
            if not isinstance(feat.get("attributes"), dict):
                return None
            a = {str(k).lower(): v for k, v in feat.get("attributes", {}).items()}
            geom = feat.get("geometry", {})
            if not isinstance(geom, dict):
                return None
            rings = geom.get("rings") or []
            flng = geom.get("x")
            flat_ = geom.get("y")
            if rings:
                if not all(isinstance(ring, list) and len(ring) >= 2 for ring in rings):
                    return None
                try:
                    valid_coords = all(
                        len(point) >= 2
                        and math.isfinite(float(point[0]))
                        and math.isfinite(float(point[1]))
                        for ring in rings for point in ring
                    )
                except (TypeError, ValueError):
                    return None
                if not valid_coords:
                    return None
                if _inside_polygon_rings(lat, lng, rings):
                    dist = 0.0
                    flng, flat_ = lng, lat
                else:
                    dist, flng, flat_ = _nearest_polygon_boundary(lat, lng, rings)
                geom = "polygon"
            elif flng is not None and flat_ is not None:
                flng, flat_ = float(flng), float(flat_)
                if not math.isfinite(flng) or not math.isfinite(flat_):
                    return None
                dist = _distance_m(lat, lng, flat_, flng)
                geom = "point"
            else:
                return None
            name = (a.get("sitename") or a.get("site_name")
                    or a.get("plan_lot_number") or a.get("reg_no") or "Registered site")
            results.append({
                "name": str(name),
                "issue": a.get("classification") or a.get("class") or "",
                "distance_m": round(dist),
                "lng": round(float(flng), 6) if flng is not None else None,
                "lat": round(float(flat_), 6) if flat_ is not None else None,
                "report_url": a.get("report_url"),
                "source": "WA DWER",
                "geom": geom,
            })
        return sorted(results, key=lambda x: x["distance_m"])
    except (requests.RequestException, ValueError, KeyError):
        return None  # error must stay distinguishable from a clean register


# ---------------------------------------------------------------------------
# Industrial proximity (Overture POIs — national coverage)
# ---------------------------------------------------------------------------

INDUSTRIAL_KEYWORDS = {
    "fuel_station", "gas_station", "petrol",
    "chemical_plant",
    "dry_cleaning",
    "recycling_center", "scrap",
    "waste_management", "waste_disposal",
    "auto_repair", "car_repair", "mechanic",
}

INDUSTRIAL_EXCLUDE = {
    "business_manufacturing", "industrial_equipment",
    "painting", "laundry_service", "warehouse",
    "commercial_industrial", "b2b_cleaning",
}

_NAME_FALSE_POSITIVES = [
    "sneaker", "shoe clean", "tailor", "alteration", "sewing",
    "end of lease", "carpet clean", "office clean", "house clean",
    "window clean", "oven clean", "bond clean",
    "ironing", "pressing", "mending",
    "skip bin", "bin hire", "rubbish removal",
]


def _industrial_proximity(lat: float, lng: float) -> dict:
    """Count industrial/contamination-risk POIs within 500m using Overture."""
    try:
        db = get_db()
        pois = pois_near_detailed(db, lat, lng, radius_m=500)

        industrial: list[dict] = []
        for cat, dist_m, plng, plat, pname in pois:
            if not cat:
                continue
            cat_lower = cat.lower()
            if any(ex in cat_lower for ex in INDUSTRIAL_EXCLUDE):
                continue
            if not any(kw in cat_lower for kw in INDUSTRIAL_KEYWORDS):
                continue
            name_lower = (pname or "").lower()
            if any(fp in name_lower for fp in _NAME_FALSE_POSITIVES):
                continue
            if cat_lower == "dry_cleaning" and not any(w in name_lower for w in ["dry clean", "launder"]):
                continue
            if True:
                industrial.append({
                    "type": cat.replace("_", " "),
                    "name": pname or cat.replace("_", " "),
                    "distance_m": round(dist_m),
                    "lng": round(plng, 6) if plng else None,
                    "lat": round(plat, 6) if plat else None,
                })

        industrial.sort(key=lambda x: x["distance_m"])
        return {
            "count_500m": len(industrial),
            "nearest_m": industrial[0]["distance_m"] if industrial else None,
            "nearest_type": industrial[0]["type"] if industrial else None,
            "sites": industrial[:10],
            "industrial_status": "ok",
        }
    except Exception:
        # count_500m 0 is indistinguishable from "queried fine, nothing near",
        # which turned an Overture outage into a 95 "Very Clean". The status
        # field is what callers must branch on (2026-08-10 fail-closed audit);
        # the legacy keys stay for backward compatibility.
        logger.exception("industrial proximity lookup failed at %s,%s", lat, lng)
        return {
            "count_500m": 0,
            "nearest_m": None,
            "nearest_type": None,
            "sites": [],
            "industrial_status": "error",
        }


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

# "former" was removed from these hints on 2026-08-27: VIC PSR writes issues
# like "Former petroleum storage site. Requires assessment and/or clean up",
# where "former" describes the historical land use while the notice is ACTIVE.
# Treating it as remediated sent an active clean-up site to a "Clean" band
# (review finding, hit on live VIC data).
_REMEDIATED_HINTS = ("remediated", "no longer", "removed from register")
# Explicit active-notice wording beats any remediated hint.
_ACTIVE_OVERRIDES = ("requires assessment", "requires clean", "requires remediation",
                     "clean up notice", "cleanup notice")


def _entry_is_active(st: dict) -> bool:
    """One definition for both the score and the on_site block: the two used
    to carry private copies, which is how a classification fix would have
    landed in one and not the other."""
    issue = str(st.get("issue", "")).lower()
    if any(k in issue for k in _ACTIVE_OVERRIDES):
        return True
    return not any(h in issue for h in _REMEDIATED_HINTS)


def _entry_is_on_site(st: dict) -> bool:
    """Polygon geometries carry a TRUE boundary distance, so anything not
    inside (dist > 0) is a different parcel however close the fence is; only
    point geometries need the jitter radius. A 40m neighbouring polygon must
    never be reported as "this address is on the register" (review finding)."""
    if st.get("geom") == "polygon":
        return st["distance_m"] <= 0
    return st["distance_m"] <= _ON_SITE_M


# A register geometry within this distance is treated as evidence about the
# site itself rather than its neighbourhood: it absorbs geocode jitter and
# point-for-parcel registrations. Beyond it, a register entry describes a
# DIFFERENT site, and contamination does not walk across a boundary on its
# own — it stays with the land it was made on unless groundwater carries it
# (2026-08-26, Hesperia development-manager review: "most contam is related
# to past uses on the site. And unless it's in groundwater, it doesn't
# migrate"). We hold no groundwater-plume data, so the disclaimer owns that
# gap; nearby entries are kept as screening context only.
_ON_SITE_M = 75


def _epa_to_score(sites: list[dict]) -> int:
    """Convert EPA sites to a score component.

    On-site evidence dominates; proximity is context. The pre-2026-08-26
    version scored pure distance decay (10/25/45/65 out to 1 km), which
    rated a clean lot at 10 because its neighbour 80 m away is on the
    register — the exact failure a developer reviewer called out.

    Severity-aware: a site whose classification says it has been REMEDIATED
    must not score like an active "remediation required" one (Killara was
    rated 30 off a remediated servo 301m away, same mechanism as genuinely
    active Botany, 2026-06-11 audit).
    """
    if not sites:
        return 95

    on_site = [st for st in sites if _entry_is_on_site(st)]
    if any(_entry_is_active(st) for st in on_site):
        # The address itself carries an active register entry.
        return 10
    if on_site:
        # The address itself was on the register and is recorded remediated:
        # a real history, materially different from an active notice.
        return 55

    active_sites = [st for st in sites if _entry_is_active(st)]
    if not active_sites:
        # only remediated/historical records nearby
        nearest_r = sites[0]["distance_m"]
        return 85 if nearest_r < 250 else 90

    # Nearby active entries. The two geometries mean different things:
    # a polygon distance is a TRUE gap to another parcel's boundary, so it is
    # neighbourhood context; a point 75-250m out may still be the register
    # pin of THIS parcel (large industrial lots pin far from any given query
    # point), so points inside 250m stay a caution band, not "Low Risk"
    # (review finding: the pre-fix 60 under-warned exactly the big-lot case).
    score = 95
    for st in active_sites:
        d = st["distance_m"]
        if st.get("geom") == "polygon":
            band = 70 if d < 250 else (80 if d < 1000 else 85)
        else:
            band = 45 if d < 250 else (75 if d < 1000 else 85)
        score = min(score, band)
    return score


def _industrial_to_score(ind: dict) -> int:
    """Convert industrial proximity to a score component.

    Same on-site-first logic as the register component: a fuel station or
    factory operating AT the address is a land-use signal about this site;
    the same business three blocks over is somebody else's site. Density
    still moves the score a little — an address inside a working industrial
    precinct is more likely to have carried such uses itself — but it can
    no longer drag a lot to 30-45 on neighbours alone.
    """
    count = ind["count_500m"]
    nearest = ind["nearest_m"]

    if count == 0:
        return 95

    if nearest is not None and nearest <= _ON_SITE_M:
        # An industrial generator on the address itself (current use).
        return 40
    if count > 5:
        # Dense working precinct around the address.
        return 70
    if count > 1:
        return 80

    return 85


# ---------------------------------------------------------------------------
# Labelling
# ---------------------------------------------------------------------------

# Labels a reader takes as "this address was checked and it came back clean".
# They are the ones that must never be produced off an incomplete check.
_REASSURING_LABELS = {"No Mapped Red Flag", "Lower Mapped Risk"}

LABEL_CHECK_UNAVAILABLE = "Check Unavailable"
LABEL_INCOMPLETE = "Incomplete Check"
LABEL_REGISTER_NOT_CHECKED = "No Mapped Red Flag (Register Not Checked)"
LABEL_MAPPED_CONTEXT = "Mapped Context - Review"


def _score_band_label(score: int) -> str:
    if score >= 90:
        return "No Mapped Red Flag"
    if score >= 70:
        return "Lower Mapped Risk"
    if score >= 50:
        return "Mapped Risk - Review"
    if score >= 30:
        return "Elevated Mapped Risk"
    if score >= 15:
        return "High Mapped Risk"
    return "Very High Mapped Risk"


def _contamination_label(score: int | None, epa_status: str, ind_failed: bool,
                         context_flagged: bool = False) -> str:
    """Pick the public label, refusing to sound reassuring off a partial check.

    A score computed while a core signal was down (or while the state has no
    integrated register) can still be a useful risk warning, so a bad band is
    kept as is. What it may never do is tell the reader the address is clean,
    because the evidence that would show otherwise was never retrieved.
    """
    if score is None:
        return LABEL_CHECK_UNAVAILABLE

    label = _score_band_label(score)
    if label not in _REASSURING_LABELS:
        return label
    if epa_status == "error" or ind_failed:
        return LABEL_INCOMPLETE
    if context_flagged:
        return LABEL_MAPPED_CONTEXT
    if epa_status == "not_integrated":
        return LABEL_REGISTER_NOT_CHECKED
    return label


# ---------------------------------------------------------------------------
# 2026-08-27 signals: historical land use / landfills / groundwater zones
# (ICP-driven: "most contam is related to past uses on the site"). Sources
# package is imported lazily inside each builder: sources._common reuses this
# module's helpers, so a module-level import here would be circular.
# Each builder returns {"status": ok|error|not_integrated, "score", entries};
# status "error" must degrade the label exactly like an EPA outage, or a
# failed history check turns into a "Very Clean" lie (same class of bug as
# Keele St, 2026-06-11).
# ---------------------------------------------------------------------------

# Historical directory geocoding wobbles more than modern registers; tighter
# than this misses the shopfront itself, looser starts scoring the neighbours
# (the exact failure the on-site rework removed).
_SANDS_ONSITE_M = 30
_TAS_EVIDENCE_RADIUS_M = 500
_TAS_SOURCE_RIGHTS = {
    "TAS EPA Regulated Sites": {
        "attribution": "EPA Regulated Sites [Documents] from theLIST © State of Tasmania",
        "licence": "CC BY 3.0 AU",
        "licence_url": "https://creativecommons.org/licenses/by/3.0/au/",
    },
    "TAS EPA Underground Petroleum Storage Systems": {
        "attribution": (
            "EPA Underground Petroleum Storage Systems from theLIST "
            "© State of Tasmania"
        ),
        "licence": "CC BY 3.0 AU",
        "licence_url": "https://creativecommons.org/licenses/by/3.0/au/",
    },
}
_ACT_SOURCE_RIGHTS = {
    "ACT EPA Register of contaminated sites": {
        "attribution": "Register of contaminated sites © Australian Capital Territory",
        "licence": "CC BY 4.0",
        "licence_url": "https://creativecommons.org/licenses/by/4.0/",
    },
}
# Density gate, measured 2026-08-27: Melbourne CBD has 1,684 deduped rows and
# 15 tier-A trades within 20m because directory geocoding clusters whole block
# faces onto near-identical points; Malvern (residential) has 26 rows at the
# same radius. Radius alone cannot say "this parcel" in a dense precinct, so
# above this row count the tier-A hit becomes evidence-only until parcel
# matching (cadastre) provides real attribution. Malvern 30m=36 in, Footscray
# 30m=55 in, CBD 30m=1,685 out.
_SANDS_DENSE_ROWS = 120

# Wall-clock budget for ONE signal builder, enforced in sources/_common before
# every HTTP attempt and every retry backoff.
#
# Why it exists (latency review, 2026-08-27). The politeness budget bounds a
# request, not a signal. Worst case WITHOUT this cap:
#   _landfill_signal = VLR polygon + VLR point + GA, serial,
#                      each 2 attempts x 10s timeout          = 60s
#   _historical_use_signal = up to 8 Sands pages, serial      = 160s
# The three signals share the /scores ThreadPoolExecutor with the EPA and
# industrial legs, whose own worst case is one 10s request. So the branch face
# went from ~10s to ~60s while _BATCH_DEADLINE_S in api/main.py is 25s and the
# gunicorn timeout is 60s: the deadline fires, the answer is thrown away, and
# the abandoned thread leaks as a STRAGGLER.
#
# Worst case WITH the cap:
#   each builder             <= 8s  (budget checked before every attempt, and
#                                    the socket timeout clamped to what is
#                                    left, so nothing overshoots it)
#   the three run in parallel -> signal face <= ~8s, not 3 x 8s
#   VLR polygon + point now also run side by side inside the landfill budget
#   branch face = max(EPA 10s, industrial, ~8s) = ~10s
# which is where the branch sat before the signals landed, and leaves the 25s
# deadline its margin back.
#
# Exhausting the budget is fail-closed: status "error", exactly like an
# unreachable register. A register we ran out of time to read has not been
# read, so it blocks reassuring labels and the result is not cached.
_SIGNAL_BUDGET_S = 8.0


def _historical_use_signal(lat: float, lng: float, state: str | None) -> dict:
    """Sands directory trades at this address, whitelist-classified.

    Tier A (service station / dry cleaner / tannery ...) scores as a SIGNAL
    band, never the register band: a directory listing says what operated
    here, not that contamination was found (official disclaimer wording).
    Tier B is evidence-only.
    """
    if state in ("SA", "QLD", "TAS"):
        from property_scores.contamination.sources import _common
        parcel_flags = None
        try:
            with _common.budget(_SIGNAL_BUDGET_S):
                if state == "SA":
                    from property_scores.contamination.sources import sa_licensed
                    rows = sa_licensed.activities_near(
                        lat, lng, radius_m=_SANDS_ONSITE_M,
                        include_coordinates=True)
                    source = "SA EPA Licensed Activities"
                    if rows:
                        from property_scores.contamination import parcel_attribution
                        remaining = _common.remaining_budget()
                        parcel_flags = parcel_attribution.same_parcel_flags(
                            "SA", lat, lng,
                            [(row["lat"], row["lng"]) for row in rows],
                            timeout_s=remaining,
                        )
                else:
                    if state == "QLD":
                        from property_scores.contamination.sources import qld_ea
                        rows = qld_ea.activities_at(lat, lng)
                        source = "QLD Environmental Authority"
                    else:
                        from property_scores.contamination.sources import tas_epa
                        regulated = tas_epa.regulated_sites_near(
                            lat, lng, radius_m=_TAS_EVIDENCE_RADIUS_M)
                        upss = tas_epa.upss_near(
                            lat, lng, radius_m=_TAS_EVIDENCE_RADIUS_M)
                        failed = regulated is None or upss is None
                        rows = None if failed and not (regulated or upss) else [
                            *[{
                                **row,
                                "source": "TAS EPA Regulated Sites",
                            } for row in (regulated or [])],
                            *[{
                                **row,
                                "source": "TAS EPA Underground Petroleum Storage Systems",
                            } for row in (upss or [])],
                        ]
                        source = "TAS EPA LIST"
        except _common.BudgetExceeded:
            rows = None
        if rows is None:
            return {"status": "error", "score": None, "entries": [],
                    "dense_precinct": False, "unattributed_a": False,
                    "on_site": False}
        # Evidence-only until each jurisdiction's activity vocabulary has its
        # own reviewed contamination mapping. A licence proves an activity was
        # approved here; it does not prove contamination or a severity band.
        parcel_attributed = (state == "SA" and parcel_flags is not None
                             and len(parcel_flags) == len(rows))
        cadastre_partial = (state == "SA" and bool(rows)
                            and not parcel_attributed)
        if parcel_attributed:
            rows = [row for row, same_parcel in zip(rows, parcel_flags)
                    if same_parcel]
        entries = []
        for row in rows:
            public_row = {key: value for key, value in row.items()
                          if key not in ("lat", "lng")}
            entries.append({**public_row, "source": public_row.get("source") or source,
                            "evidence_only": True})
        if state == "TAS":
            return {"status": "partial" if failed else "ok",
                    "score": None, "entries": entries[:10],
                    "dense_precinct": False, "unattributed_a": False,
                    "on_site": False,
                    "evidence_radius_m": _TAS_EVIDENCE_RADIUS_M,
                    "representative_points_only": True}
        result = {"status": "partial" if cadastre_partial else "ok",
                  "score": None, "entries": entries[:10],
                "dense_precinct": False, "unattributed_a": False,
                "on_site": bool(entries)}
        if state == "SA":
            result["parcel_attributed"] = parcel_attributed
        return result
    if state != "VIC":
        return {"status": "not_integrated", "score": None, "entries": [],
                "dense_precinct": False, "unattributed_a": False,
                "on_site": False}
    from property_scores.contamination.sources import _common, vic_wfs
    from property_scores.contamination.sources.sands_whitelist import classify
    classified_rows = []
    flags = None
    try:
        with _common.budget(_SIGNAL_BUDGET_S):
            rows = vic_wfs.sands_near(lat, lng, radius_m=_SANDS_ONSITE_M)
            if rows is not None:
                for row in rows:
                    hit = classify(row.get("business_type"))
                    if hit is None:
                        continue
                    tier, activity = hit
                    classified_rows.append((row, tier, activity))

                evidence_points = [(row.get("lat"), row.get("lng"))
                                   for row, _, _ in classified_rows]
                can_attribute = bool(evidence_points) and all(
                    isinstance(point_lat, (int, float))
                    and isinstance(point_lng, (int, float))
                    for point_lat, point_lng in evidence_points
                )
                if can_attribute:
                    from property_scores.contamination import parcel_attribution
                    flags = parcel_attribution.same_parcel_flags(
                        "VIC", lat, lng, evidence_points,
                        timeout_s=_common.remaining_budget())
    except _common.BudgetExceeded:
        rows = None  # out of time == not read (see _SIGNAL_BUDGET_S)
    if rows is None:
        return {"status": "error", "score": None, "entries": [],
                "dense_precinct": False, "unattributed_a": False,
                "on_site": False}
    entries = []
    # Radius is the safe fallback, but where the shared cadastre is available
    # it can answer the question the directory geocoder cannot: whether the
    # historical point falls inside THIS lot.  Address points may be snapped
    # from the kerb; evidence points are strict containment only.
    parcel_attributed = (flags is not None
                         and len(flags) == len(classified_rows))
    if not parcel_attributed:
        flags = [True] * len(classified_rows)

    a_hits = 0
    for (row, tier, activity), same_parcel in zip(classified_rows, flags):
        if parcel_attributed and not same_parcel:
            continue
        years = row.get("directories") or []
        entries.append({
            "business_type": row.get("business_type"),
            "activity_class": activity,
            "tier": tier,
            "first_year": years[0] if years else None,
            "last_year": years[-1] if years else None,
            "distance_m": row.get("distance_m"),
        })
        if tier == "A":
            a_hits += 1
    dense = len(rows) > _SANDS_DENSE_ROWS
    if dense and not parcel_attributed:
        # A whole block face geocoded onto this point: proximity no longer
        # implies identity, so no score until parcel matching lands.
        score = None
    else:
        score = 45 if a_hits >= 2 else (50 if a_hits == 1 else None)
    # A tier-A trade metres away that we CANNOT attribute must not coexist
    # with a reassuring label ("Very Clean next to an unattributed service
    # station", review P1-3): the caller blocks reassuring labels on this.
    return {"status": ("partial" if classified_rows
                        and not parcel_attributed else "ok"),
            "score": score,
            "dense_precinct": dense,
            "parcel_attributed": parcel_attributed,
            "unattributed_a": dense and not parcel_attributed and a_hits > 0,
            "on_site": score is not None,
            "entries": entries[:10]}


def _landfill_signal(lat: float, lng: float, state: str | None) -> dict:
    """Legacy and operating landfills. Landfills are the exception to the
    stays-with-the-site rule: gas and leachate do move, so nearby carries a
    real (bounded) component rather than evidence-only."""
    from property_scores.contamination.sources import _common, ga_waste, vic_wfs
    entries = []
    failed = False
    try:
        # One budget for the whole builder, not per source: this is the
        # longest chain of the three (VLR polygon + VLR point + GA).
        with _common.budget(_SIGNAL_BUDGET_S):
            if state == "VIC":
                vlr = vic_wfs.landfills_near(lat, lng, radius_m=1000)
                if vlr is None:
                    failed = True
                else:
                    entries += [{**r, "source": "VIC VLR"} for r in vlr]
            ga = ga_waste.landfills_near(lat, lng, radius_m=1000)
            if ga is None:
                failed = True
            else:
                entries += [{**r, "source": "GA WMF"} for r in ga]
    except _common.BudgetExceeded:
        # Whatever arrived before the clock ran out is dropped on purpose: a
        # half-read landfill picture that says "nothing within 1km" is the
        # Keele St failure shape, so the honest answer is "not read".
        return {"status": "error", "score": None, "entries": []}
    if failed and not entries:
        return {"status": "error", "score": None, "entries": []}
    entries.sort(key=lambda e: e.get("distance_m") or 0)
    score = None
    if entries:
        nearest = entries[0].get("distance_m") or 0
        if nearest <= _ON_SITE_M:
            score = 45
        elif nearest < 250:
            score = 70
        else:
            score = 85
    return {"status": "partial" if failed else "ok",
            "score": score, "entries": entries[:10]}


def _groundwater_signal(lat: float, lng: float, state: str | None) -> dict:
    """Official groundwater context (VIC GQRUZ, SA GPA, NSW vulnerability):
    the regulator's own statement that groundwater HERE carries historical
    industrial contamination. Inside a zone is the one case where the
    "doesn't migrate" discount must give ground."""
    from property_scores.contamination.sources import _common
    if state == "VIC":
        from property_scores.contamination.sources import vic_wfs
        source = "VIC EPA GQRUZ"
    elif state == "SA":
        from property_scores.contamination.sources import sa_gpa
        source = "SA EPA GPA"
    elif state == "NSW":
        from property_scores.contamination.sources import nsw_groundwater
        source = "NSW DPHI EPI Groundwater Vulnerability"
    else:
        return {"status": "not_integrated", "score": None, "entries": []}
    try:
        with _common.budget(_SIGNAL_BUDGET_S):
            if state == "VIC":
                zones = vic_wfs.gqruz_near(lat, lng, radius_m=500)
            elif state == "SA":
                zones = sa_gpa.areas_near(lat, lng, radius_m=500)
            else:
                zones = nsw_groundwater.vulnerability_at(lat, lng)
    except _common.BudgetExceeded:
        zones = None  # out of time == not read (see _SIGNAL_BUDGET_S)
    if zones is None:
        return {"status": "error", "score": None, "entries": []}
    inside = [z for z in zones if z.get("inside")]
    # Only statutory restriction/prohibition zones carry a score. NSW is a
    # planning sensitivity overlay and remains evidence-only.
    score = 55 if inside and state in ("VIC", "SA") else None
    entries = [{**z, "source": source} for z in zones]
    return {"status": "ok", "score": score, "entries": entries[:5]}


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------

_contam_cache: OrderedDict[tuple[float, float], tuple[dict, float]] = OrderedDict()
# Measured 2026-08-10: an entry is ~372 B empty and ~2.9 KB at its worst
# (Sydney CBD, 19 EPA sites), so this cap costs 0.7-5.9 MB. The round(4) key
# above made each entry cover ~98 m2 instead of ~9,800 m2, so the same cap now
# holds roughly one entry per queried address rather than one per block. Raise
# it if the hit rate is measured to have dropped; do not lower the key
# precision to buy the hit rate back.
_CONTAM_CACHE_MAX = 2000
_CONTAM_CACHE_TTL = 3600


def contamination_score(lat: float, lng: float) -> dict:
    """Compute contamination risk score for an Australian coordinate.

    Combines official EPA registers (VIC/NSW/ACT) with industrial POI
    proximity from Overture data for national coverage.
    """
    # EPA sites have specific locations and the score bands break at 100m /
    # 250m, so a round(3) key (~111m grid) let neighbouring parcels on either
    # side of a band edge share one answer. round(4) is ~11m (2026-08-10).
    key = (round(lat, 4), round(lng, 4))
    now = _time.time()
    if key in _contam_cache:
        cached, ts = _contam_cache[key]
        if now - ts < _CONTAM_CACHE_TTL:
            _contam_cache.move_to_end(key)
            return {**cached, "cached": True}
        else:
            del _contam_cache[key]

    state = _detect_state(lat, lng)
    if state is None:
        return {
            "score": None,
            "label": "Outside Australia",
            "state": None,
            "epa_sites": [],
            "industrial": {},
        }

    # --- Both phases in parallel ---
    from concurrent.futures import ThreadPoolExecutor

    def _fetch_epa():
        if state == "VIC":
            return _vic_epa_sites(lat, lng)
        elif state == "NSW":
            return _nsw_epa_sites(lat, lng)
        elif state == "ACT":
            from property_scores.contamination.sources import act_register
            return act_register.sites_at(lat, lng)
        # WA DWER-059 is technically queryable, but its Custom Active
        # Acceptance licence requires written permission for external derived
        # products. Keep the adapter tested below, but do not call it from the
        # public score until DWER grants that permission.
        return []

    with ThreadPoolExecutor(max_workers=5) as pool:
        f_epa = pool.submit(_fetch_epa)
        f_ind = pool.submit(_industrial_proximity, lat, lng)
        f_hist = pool.submit(_historical_use_signal, lat, lng, state)
        f_lf = pool.submit(_landfill_signal, lat, lng, state)
        f_gw = pool.submit(_groundwater_signal, lat, lng, state)

    epa_sites = f_epa.result()
    # None = the register query FAILED; [] = queried fine, nothing nearby.
    # Conflating them turned outages into "Very Clean" and cached the lie
    # for an hour (Keele St 10 -> 70 on a dropped connection, 2026-06-11).
    epa_failed = epa_sites is None
    if epa_failed:
        epa_sites = []
    else:
        # Final contract guard shared by all register adapters. Each upstream
        # has different spatial-query semantics; none may leak wider context
        # into the public 2 km count or score even if its own prefilter does.
        epa_sites = _sites_within_radius(epa_sites, _EPA_RADIUS_M)
        if epa_sites is None:
            epa_failed = True
            epa_sites = []
    epa_score = (_epa_to_score(epa_sites)
                 if not epa_failed and (epa_sites or state in ("VIC", "NSW", "ACT"))
                 else None)

    industrial = f_ind.result()
    ind_failed = industrial.get("industrial_status") == "error"
    ind_score = None if ind_failed else _industrial_to_score(industrial)

    historical = f_hist.result()
    landfill = f_lf.result()
    groundwater = f_gw.result()
    # A failed OR partial history/landfill/groundwater check is a partial
    # check, same as an EPA outage: the score may stand but a reassuring
    # label may not, and the result must not be cached (review P0-1: VIC
    # legacy landfills live in VLR alone, so VLR-down + GA-ok = "partial"
    # WAS producing a cached Very Clean, the Keele St failure shape again).
    aux_failed = any(sig["status"] in ("error", "partial")
                     for sig in (historical, landfill, groundwater))
    # Dense-precinct tier-A evidence that cannot be attributed to this parcel
    # blocks reassuring labels too, but is cacheable (it is stable data, not
    # an outage).
    unattributed = bool(historical.get("unattributed_a"))

    # --- Combine ---
    components = [s for s in (epa_score, ind_score, historical["score"],
                              landfill["score"], groundwater["score"])
                  if s is not None]
    score = max(0, min(100, min(components))) if components else None

    epa_status = ("error" if epa_failed
                  else "ok" if state in ("VIC", "NSW", "ACT")
                  else "not_integrated")
    # Any delivered evidence must block a reassuring "Clean" label, even
    # when it belongs to a neighbouring site and correctly does not lower the
    # subject parcel into a risk band. NSW CBD/Wagga/Killara returned 7-18
    # official register rows while the headline still said Clean because this
    # gate only looked at historical-use and groundwater context. The detail
    # was technically present, but the commercial headline contradicted it.
    context_flagged = bool(
        epa_sites
        or industrial.get("sites")
        or historical.get("entries")
        or landfill.get("entries")
        or groundwater.get("entries")
    )
    label = _contamination_label(
        score,
        epa_status=epa_status,
        ind_failed=ind_failed or aux_failed or unattributed,
        context_flagged=context_flagged,
    )
    # An incomplete screen may still carry a useful bad signal, but it must
    # never export a reassuring 70-100 number that downstream customers can
    # sort as "safe" while the official register was not checked. Known WA
    # remediation-required anchors previously returned 95 with only a prose
    # caveat. Preserve <=65 warnings; null optimistic scores structurally.
    incomplete_coverage = (
        epa_status != "ok" or ind_failed or aux_failed or unattributed)
    if score is None:
        score_status = "unavailable"
    elif incomplete_coverage and score >= 70:
        score = None
        score_status = "unavailable_incomplete_coverage"
    elif incomplete_coverage:
        score_status = "partial_risk_signal"
    else:
        score_status = "available"

    # On-site summary, so consumers can show "this address" separately from
    # "the neighbourhood" instead of implying a nearby entry is site risk.
    epa_on_site = [s for s in epa_sites if _entry_is_on_site(s)]
    ind_nearest = industrial.get("nearest_m")
    lf_entries = landfill.get("entries") or []
    on_site = {
        "epa_active": any(_entry_is_active(s) for s in epa_on_site),
        "epa_remediated": bool(epa_on_site) and not any(_entry_is_active(s) for s in epa_on_site),
        "industrial": (not ind_failed and ind_nearest is not None
                       and ind_nearest <= _ON_SITE_M),
        # 2026-08-27 signals (review P1-5: a 45 driven by a 7m Sands hit must
        # not render as "nothing at this address"):
        # attributed tier-A directory trade at this address
        "historical_use": historical.get(
            "on_site", historical.get("score") is not None),
        "landfill": bool(lf_entries) and (
            (lf_entries[0].get("distance_m") or 10**9) <= _ON_SITE_M),
        # inside an official restricted-groundwater zone
        "groundwater": any(z.get("inside") for z in (groundwater.get("entries") or [])),
        "radius_m": _ON_SITE_M,
    }

    result: dict = {
        "score": score,
        "score_status": score_status,
        "label": label,
        "disclaimer": ("Score reflects register entries and industrial land "
                       "use at the address itself; nearby entries are shown "
                       "as context only, since contamination stays with the "
                       "site that produced it unless groundwater carries it. "
                       "No groundwater-plume data is included. Most "
                       "contamination stems from past on-site uses that no "
                       "register captures, so a clean screen is not a clean "
                       "site. Not a substitute for site contamination "
                       "assessment."),
        "state": state,
        "on_site": on_site,
        "epa_sites_count": len(epa_sites),
        "epa_sites": epa_sites[:10],
        "epa_sites_returned": min(len(epa_sites), 10),
        "industrial": industrial,
        "historical_use": historical,
        "landfill": landfill,
        "groundwater": groundwater,
    }
    result["epa_status"] = epa_status
    result["industrial_status"] = industrial.get("industrial_status", "ok")
    delivered_rights_sources = {
        entry.get("source")
        for entry in [*epa_sites, *historical.get("entries", [])]
        if entry.get("source") in {**_TAS_SOURCE_RIGHTS, **_ACT_SOURCE_RIGHTS}
    }
    if delivered_rights_sources:
        rights = {**_TAS_SOURCE_RIGHTS, **_ACT_SOURCE_RIGHTS}
        result["attribution"] = [
            {"source": source, **rights[source]}
            for source in sorted(delivered_rights_sources)
        ]

    notes: list[str] = []
    if epa_failed:
        notes.append(f"The {state} EPA register could not be reached for this "
                     "check, so any listed site near this address would not "
                     "show up here.")
    elif epa_status == "not_integrated":
        notes.append(f"No {state} EPA register is integrated. Check the state "
                     "register directly for this address.")
    if ind_failed:
        notes.append("The industrial land use data could not be reached for "
                     "this check.")
    if notes:
        if score is None:
            notes.append("No score could be produced for this address. Try again later.")
        else:
            notes.append("The result is incomplete and may understate risk.")
        result["note"] = " ".join(notes)

    # A degraded result must not be pinned for an hour. `not epa_failed` has
    # been here since the 2026-06-11 dropped-connection fix; `not ind_failed`
    # is the new half, because before it an Overture outage cached a 95
    # "Very Clean" for every later caller on the same grid cell.
    # `not aux_failed` (2026-08-27 review P1-2) extends the same rule to the
    # historical/landfill/groundwater signals: their outage pinned an
    # OPTIMISTIC score (a down groundwater check simply never contributed its
    # 55). A not_integrated state, and unattributed dense-precinct evidence,
    # are stable facts, not outages, so they still cache.
    if not epa_failed and not ind_failed and not aux_failed:
        _contam_cache[key] = (result, _time.time())
        if len(_contam_cache) > _CONTAM_CACHE_MAX:
            _contam_cache.popitem(last=False)

    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Compute contamination risk score")
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lng", type=float, required=True)
    args = parser.parse_args()

    result = contamination_score(args.lat, args.lng)
    print(f"Contamination: {result['score']}/100 ({result['label']})")
    print(f"State: {result['state']}")
    if result.get("nearest_epa_site"):
        s = result["nearest_epa_site"]
        print(f"Nearest EPA site: {s['name']} ({s['distance_m']}m) — {s['source']}")
    print(f"EPA sites within 2km: {result['epa_sites_count']}")
    ind = result["industrial"]
    print(f"Industrial POIs 500m: {ind['count_500m']}" +
          (f" (nearest: {ind['nearest_type']} at {ind['nearest_m']}m)" if ind['nearest_m'] else ""))
