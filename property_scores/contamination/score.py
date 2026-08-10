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

from property_scores.common.overture import get_db, pois_near, pois_near_detailed

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


def _vic_epa_sites(lat: float, lng: float, radius_m: int = 2000) -> list[dict] | None:
    """Query VIC EPA Priority Sites Register via WFS."""
    deg = radius_m / 111_000
    bbox = f"{lng - deg},{lat - deg},{lng + deg},{lat + deg},EPSG:4326"
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
        m_per_deg = 111_320 * math.cos(math.radians(lat))
        for f in features:
            coords = f.get("geometry", {}).get("coordinates", [])
            if len(coords) >= 2:
                dist = math.sqrt(
                    ((coords[0] - lng) * m_per_deg) ** 2 +
                    ((coords[1] - lat) * 111320) ** 2
                )
                props = f.get("properties", {})
                results.append({
                    "name": props.get("address", "Unknown"),
                    "issue": props.get("issue", ""),
                    "distance_m": round(dist),
                    "lng": round(coords[0], 6),
                    "lat": round(coords[1], 6),
                    "source": "VIC EPA PSR",
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
    m_per_deg = 111_320 * math.cos(math.radians(lat))
    deg = radius_m / 111_000
    try:
        resp = requests.get(url, params={
            "geometry": f"{lng - deg},{lat - deg},{lng + deg},{lat + deg}",
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
            a = feat.get("attributes", {})
            flng = a.get("Longitude")
            flat = a.get("Latitude")
            if flng and flat:
                dist = math.sqrt(
                    ((flng - lng) * m_per_deg) ** 2 +
                    ((flat - lat) * 111320) ** 2
                )
                results.append({
                    "name": a.get("SiteName", "Unknown"),
                    "issue": a.get("ContaminationActivityType", ""),
                    "distance_m": round(dist),
                    "lng": round(flng, 6),
                    "lat": round(flat, 6),
                    "source": "NSW EPA CLR",
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
        m_per_deg = 111_320 * math.cos(math.radians(lat))

        def _inside(rings):
            # ray cast against the outer ring: inside the register polygon
            # means distance 0 (standing ON the contaminated site)
            ring = rings[0]
            n, hit = len(ring), False
            j = n - 1
            for i in range(n):
                xi, yi = ring[i][0], ring[i][1]
                xj, yj = ring[j][0], ring[j][1]
                if (yi > lat) != (yj > lat) and                         lng < (xj - xi) * (lat - yi) / (yj - yi + 1e-12) + xi:
                    hit = not hit
                j = i
            return hit

        results = []
        for feat in features:
            a = {str(k).lower(): v for k, v in feat.get("attributes", {}).items()}
            geom = feat.get("geometry", {})
            rings = geom.get("rings") or []
            flng = geom.get("x")
            flat_ = geom.get("y")
            if rings:
                if _inside(rings):
                    dist = 0.0
                else:
                    dist = min(
                        math.sqrt(((vx - lng) * m_per_deg) ** 2
                                  + ((vy - lat) * 111320) ** 2)
                        for vx, vy in rings[0])
                cx = sum(v[0] for v in rings[0]) / len(rings[0])
                cy = sum(v[1] for v in rings[0]) / len(rings[0])
                flng, flat_ = cx, cy
            elif flng is not None and flat_ is not None:
                dist = math.sqrt(((flng - lng) * m_per_deg) ** 2
                                 + ((flat_ - lat) * 111320) ** 2)
            else:
                dist = radius_m
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

_REMEDIATED_HINTS = ("remediated", "no longer", "removed from register", "former")


def _epa_to_score(sites: list[dict]) -> int:
    """Convert EPA sites to a score component.

    Severity-aware: a site whose classification says it has been REMEDIATED
    must not score like an active "remediation required" one (Killara was
    rated 30 off a remediated servo 301m away, same mechanism as genuinely
    active Botany, 2026-06-11 audit). Remediated-only neighbourhoods are
    capped at moderate concern.
    """
    if not sites:
        return 95

    def _active(st):
        issue = str(st.get("issue", "")).lower()
        return not any(h in issue for h in _REMEDIATED_HINTS)

    active_sites = [st for st in sites if _active(st)]
    if not active_sites:
        # only remediated/historical records nearby
        nearest_r = sites[0]["distance_m"]
        return 70 if nearest_r < 250 else 85

    sites = active_sites
    nearest = sites[0]["distance_m"]
    count = len(sites)

    if nearest < 100:
        return 10
    if nearest < 250:
        return 25
    if nearest < 500 and count > 2:
        return 30
    if nearest < 500:
        return 45
    if nearest < 1000 and count > 3:
        return 50
    if nearest < 1000:
        return 65
    if nearest < 2000:
        return 80

    return 90


def _industrial_to_score(ind: dict) -> int:
    """Convert industrial proximity to a score component."""
    count = ind["count_500m"]
    nearest = ind["nearest_m"]

    if count == 0:
        return 95

    if nearest is not None and nearest < 100 and count > 3:
        return 30
    if nearest is not None and nearest < 100:
        return 45
    if count > 5:
        return 40
    if count > 3:
        return 55
    if count > 1:
        return 70

    return 80


# ---------------------------------------------------------------------------
# Labelling
# ---------------------------------------------------------------------------

# Labels a reader takes as "this address was checked and it came back clean".
# They are the ones that must never be produced off an incomplete check.
_REASSURING_LABELS = {"Very Clean", "Clean"}

LABEL_CHECK_UNAVAILABLE = "Check Unavailable"
LABEL_INCOMPLETE = "Incomplete Check"
LABEL_REGISTER_NOT_CHECKED = "No Mapped Red Flag (Register Not Checked)"


def _score_band_label(score: int) -> str:
    if score >= 90:
        return "Very Clean"
    if score >= 70:
        return "Clean"
    if score >= 50:
        return "Low Risk"
    if score >= 30:
        return "Moderate Risk"
    if score >= 15:
        return "High Risk"
    return "Very High Risk"


def _contamination_label(score: int | None, epa_status: str, ind_failed: bool) -> str:
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
    if epa_status == "not_integrated":
        return LABEL_REGISTER_NOT_CHECKED
    return label


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

    Combines official EPA registers (VIC/NSW/WA) with industrial POI
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
        elif state == "WA":
            return _wa_epa_sites(lat, lng)
        return []

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_epa = pool.submit(_fetch_epa)
        f_ind = pool.submit(_industrial_proximity, lat, lng)

    epa_sites = f_epa.result()
    # None = the register query FAILED; [] = queried fine, nothing nearby.
    # Conflating them turned outages into "Very Clean" and cached the lie
    # for an hour (Keele St 10 -> 70 on a dropped connection, 2026-06-11).
    epa_failed = epa_sites is None
    if epa_failed:
        epa_sites = []
    epa_score = (_epa_to_score(epa_sites)
                 if not epa_failed and (epa_sites or state in ("VIC", "NSW", "WA"))
                 else None)

    industrial = f_ind.result()
    ind_failed = industrial.get("industrial_status") == "error"
    ind_score = None if ind_failed else _industrial_to_score(industrial)

    # --- Combine ---
    components = [s for s in (epa_score, ind_score) if s is not None]
    score = max(0, min(100, min(components))) if components else None

    epa_status = ("error" if epa_failed
                  else "ok" if state in ("VIC", "NSW", "WA")
                  else "not_integrated")
    label = _contamination_label(score, epa_status=epa_status, ind_failed=ind_failed)

    result: dict = {
        "score": score,
        "label": label,
        "disclaimer": "Estimate based on EPA registers and POI proximity. Not a substitute for site contamination assessment.",
        "state": state,
        "epa_sites_count": len(epa_sites),
        "epa_sites": epa_sites[:10],
        "industrial": industrial,
    }
    result["epa_status"] = epa_status
    result["industrial_status"] = industrial.get("industrial_status", "ok")

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
    # "Very Clean" for every later caller on the same grid cell. A
    # not_integrated state is a stable fact, not an outage, so it still caches.
    if not epa_failed and not ind_failed:
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
