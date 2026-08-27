"""Geoscience Australia Waste Management Facilities adapter (landfills only).

WHAT THIS LAYER IS, AND IS NOT: it is a register of CURRENTLY OPERATING waste
infrastructure - 6,421 of its 6,453 records are OPERATIONAL and only 32 are
CLOSED (measured 2026-08-27). It is therefore a weak "an operating landfill is
nearby" signal, not a historical landfill layer. Legacy tips, which are the
ones that matter most for contamination, are almost entirely absent; in VIC
that job belongs to the Victorian Landfill Register instead. Treat a hit as
national fallback coverage, not as evidence of historical filling.

Filtering is mandatory. Unfiltered, 1,620 of the records are supermarket soft
plastics drop-off bins and 1,493 are transfer stations. Only 1,154 are
landfills, spread across ``LANDFILL – PUTRESCIBLE`` / ``LANDFILL – INERT`` /
``LANDFILL – NOT CLASSIFIED`` / ``LANDFILL``. The separator inside those values
is an EN DASH, not a hyphen, so the WHERE clause uses ``LIKE '%LANDFILL%'``
rather than exact matches.

Fail-closed (2026-08-10 audit): ``None`` = the service could not be queried
(network error, non-2xx, HTTP 200 carrying an ArcGIS ``{"error": ...}`` body,
or an unrecognised payload). ``[]`` = queried fine, no landfill within the
radius.

Attribution (CC BY 4.0): ``Waste Management Facilities (c) Commonwealth of
Australia (Geoscience Australia) 2017, licensed under CC BY 4.0.``
"""

import logging

from property_scores.contamination.sources._common import (
    _distance_m,
    fetch_json,
    geojson_features_or_none,
)

logger = logging.getLogger(__name__)

QUERY_URL = (
    "https://services.ga.gov.au/gis/rest/services/Waste_Management_Facilities"
    "/MapServer/0/query"
)

# En dash inside the upstream values ("LANDFILL – PUTRESCIBLE"), so LIKE, never
# an equality list. Verified 2026-08-27: this WHERE returns 1,154 of 6,453.
LANDFILL_WHERE = "facility_infrastructure_type LIKE '%LANDFILL%'"

OUT_FIELDS = (
    "facility_name,facility_owner,facility_infrastructure_type,"
    "operational_status,address,suburb,state,postcode"
)
_MAX_RECORDS = 200


def landfills_near(lat: float, lng: float, radius_m: int = 2000) -> list[dict] | None:
    """Operating landfills within ``radius_m`` of a point, nearest first.

    Returns ``[{name, type, status, owner, address, suburb, state,
    distance_m, lat, lng}]``, or ``None`` if the service could not be queried.
    ``[]`` means the query succeeded and no landfill is nearby; the two are
    not interchangeable.
    """
    if radius_m <= 0:
        raise ValueError("radius_m must be positive")
    data = fetch_json(QUERY_URL, {
        "where": LANDFILL_WHERE,
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "outSR": "4326",
        "distance": radius_m,
        "units": "esriSRUnit_Meter",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": OUT_FIELDS,
        "returnGeometry": "true",
        "resultRecordCount": _MAX_RECORDS,
        "f": "json",
    })
    features = geojson_features_or_none(data)
    if features is None:
        return None

    results = []
    for feat in features:
        if not isinstance(feat, dict):
            return None
        attrs = feat.get("attributes")
        geom = feat.get("geometry")
        if not isinstance(attrs, dict) or not isinstance(geom, dict):
            return None
        flng, flat = geom.get("x"), geom.get("y")
        if flng is None or flat is None:
            return None
        try:
            flng, flat = float(flng), float(flat)
        except (TypeError, ValueError):
            return None
        if not (-90.0 <= flat <= 90.0) or not (-180.0 <= flng <= 180.0):
            return None
        infra = str(attrs.get("facility_infrastructure_type") or "").strip()
        if "LANDFILL" not in infra.upper():
            # Belt and braces: if the server-side WHERE were ever dropped or
            # silently ignored, this keeps soft-plastics bins out of the score.
            continue
        dist = _distance_m(lat, lng, flat, flng)
        if dist > radius_m:
            continue
        results.append({
            "name": str(attrs.get("facility_name") or "").strip() or "Unknown",
            "type": infra,
            "status": str(attrs.get("operational_status") or "").strip(),
            "owner": str(attrs.get("facility_owner") or "").strip() or None,
            "address": str(attrs.get("address") or "").strip() or None,
            "suburb": str(attrs.get("suburb") or "").strip() or None,
            "state": str(attrs.get("state") or "").strip() or None,
            "distance_m": round(dist),
            "lat": round(flat, 6),
            "lng": round(flng, 6),
        })
    return sorted(results, key=lambda s: s["distance_m"])
