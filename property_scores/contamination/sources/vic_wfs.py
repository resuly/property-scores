"""VIC adapters over the single DEECA/EPA GeoServer WFS endpoint.

Three layers, three different meanings:

* ``sands_mcdougall_public`` - Sands & McDougall trade directories 1896-1974,
  ~500k geocoded points. A HISTORICAL LAND USE SIGNAL, never a contamination
  finding. EPA Victoria's own wording: the listings are "not definitive
  information about possible or actual contamination and must not be relied
  on or held out as such".
* ``vlr_point`` / ``vlr_polygon`` - Victorian Landfill Register, including
  closed legacy landfills.
* ``gqruz_polygon`` - Groundwater Quality Restricted Use Zones. An official
  restriction area, i.e. contamination that has reached groundwater; the
  migration amplifier in the scoring model.

Two measured upstream traps, both 2026-08-27:

1. WFS 2.0 with a ``urn:ogc:def:crs:EPSG::4326`` bbox uses **lat,lon** axis
   order, not lon,lat. Getting it backwards silently returns nothing, which
   would read as a clean register.
2. Sands lists the same shopfront once per directory year, so a 2km Melbourne
   CBD bbox matches 97,019 raw rows. Queries MUST be bbox-filtered (never
   pull the layer whole) and results MUST be de-duplicated by
   (address, business_type), otherwise one old service station is counted
   five to eight times.

Fail-closed (2026-08-10 audit): ``None`` = the layer could not be read
(network error, non-2xx, HTTP 200 carrying a GeoServer ``{"exceptions": ...}``
body, or an unrecognised feature shape). ``[]`` = read fine, nothing nearby.

Attribution (CC BY 4.0) required on any surface exposing these records; see
the licence audit in limon-ops docs/contamination-data-sources-tracker.md.
"""

import logging
from concurrent.futures import ThreadPoolExecutor

from property_scores.contamination.sources._common import (
    _distance_m,
    _search_envelope,
    child_context,
    fetch_json,
    geojson_features_or_none,
    point_coords,
    polygon_distance_m,
    polygon_rings,
)

logger = logging.getLogger(__name__)

WFS_URL = "https://opendata.maps.vic.gov.au/geoserver/wfs"

LAYER_SANDS = "open-data-platform:sands_mcdougall_public"
LAYER_VLR_POINT = "open-data-platform:vlr_point"
LAYER_VLR_POLYGON = "open-data-platform:vlr_polygon"
LAYER_GQRUZ_POLYGON = "open-data-platform:gqruz_polygon"

# Page size for the paged Sands reader, and the total feature ceiling for a
# single sands_near() call. Melbourne CBD is the national worst case: a 200m
# radius already matches 11,917 raw rows and 500m matches 63,983 (measured
# 2026-08-27). If a call would exceed the ceiling we return None rather than a
# silently truncated neighbourhood, because a partially read register has not
# been read - same discipline as a non-2xx.
_SANDS_PAGE = 2000
_SANDS_SORT_BY = "vdpid"
_SANDS_MAX_FEATURES = 16000

# The VLR and GQRUZ layers are sparse (single/double digit hits per query), so
# one request each is enough. Saturating this cap means the layer changed
# character and is escalated to a failure by the same rule as above.
_DEFAULT_COUNT = 500

# VLR and GQRUZ write missing values as this literal string rather than null.
_MISSING = "not available"


def _clean(value) -> str | None:
    """Trim, and fold upstream's "Not available" sentinel down to ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == _MISSING:
        return None
    return text


def _bbox_param(lat: float, lng: float, radius_m: int) -> str:
    """WFS 2.0 bbox in urn CRS axis order: min lat, min lon, max lat, max lon.

    The lat/lon swap here is the trap; see the module docstring.
    """
    west, south, east, north = _search_envelope(lat, lng, radius_m)
    return f"{south},{west},{north},{east},urn:ogc:def:crs:EPSG::4326"


def _get_feature(type_names: str, lat: float, lng: float, radius_m: int,
                 count: int, start_index: int = 0, sort_by: str | None = None):
    """One GetFeature call, bbox-filtered. Returns the decoded body or None."""
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": type_names,
        "bbox": _bbox_param(lat, lng, radius_m),
        "outputFormat": "application/json",
        "count": count,
    }
    if start_index:
        params["startIndex"] = start_index
    if sort_by:
        # GeoServer refuses a paged read on a view with no primary key
        # ("Cannot do natural order without a primary key"), and an unsorted
        # page window would not be stable anyway. Measured 2026-08-27.
        params["sortBy"] = sort_by
    return fetch_json(WFS_URL, params)


def _wfs_features(type_names: str, lat: float, lng: float, radius_m: int,
                  count: int) -> list | None:
    """Single-page GetFeature on a sparse layer. ``None`` on any failure.

    Saturating ``count`` is treated as a failure, not as a truncated answer:
    these layers hold a handful of features per query, so a full page means we
    no longer know what we did not see.
    """
    data = _get_feature(type_names, lat, lng, radius_m, count)
    features = geojson_features_or_none(data)
    if features is None:
        return None
    if len(features) >= count:
        logger.warning("%s saturated the %d feature cap near %.4f,%.4f; "
                       "refusing to answer from a partial read",
                       type_names, count, lat, lng)
        return None
    return features


def _sands_features(lat: float, lng: float, radius_m: int) -> list | None:
    """Every Sands row inside the bbox, paged. ``None`` on failure or overflow.

    Overflow is a failure on purpose: the caller asked about an area whose raw
    row count exceeds what we are willing to pull, so the honest answer is
    "not read", not a slice of arbitrary years and streets.
    """
    first = _get_feature(lat=lat, lng=lng, radius_m=radius_m,
                         type_names=LAYER_SANDS, count=_SANDS_PAGE,
                         sort_by=_SANDS_SORT_BY)
    features = geojson_features_or_none(first)
    if features is None:
        return None
    matched = first.get("numberMatched") if isinstance(first, dict) else None
    try:
        matched = int(matched)
    except (TypeError, ValueError):
        matched = None
    if matched is not None and matched > _SANDS_MAX_FEATURES:
        logger.warning("Sands bbox near %.4f,%.4f matches %d rows, over the "
                       "%d ceiling; use a smaller radius",
                       lat, lng, matched, _SANDS_MAX_FEATURES)
        return None

    collected = list(features)
    while len(collected) < (matched if matched is not None else 0):
        if len(collected) >= _SANDS_MAX_FEATURES:
            logger.warning("Sands paging hit the %d feature ceiling near "
                           "%.4f,%.4f", _SANDS_MAX_FEATURES, lat, lng)
            return None
        page = _get_feature(lat=lat, lng=lng, radius_m=radius_m,
                            type_names=LAYER_SANDS, count=_SANDS_PAGE,
                            start_index=len(collected),
                            sort_by=_SANDS_SORT_BY)
        page_features = geojson_features_or_none(page)
        if page_features is None:
            return None
        if not page_features:
            break  # upstream disagrees with its own numberMatched; stop here
        collected.extend(page_features)
    return collected


def _adopted_address(props: dict) -> str | None:
    """Geocoder-normalised street address, or ``None`` if upstream has none.

    Two reasons to prefer the ``adopted_*`` fields over the raw ``address``
    column. First, the raw column is the directory line as printed and in many
    years it carries the trader's personal name inside it ("Lester, G., 60
    Tooronga-Rd, Haw. E.", measured 2026-08-27), which must not travel with
    the record. Second, the raw column spells the same shopfront differently
    from year to year ("727 Dandenong Rd Malv" / "727 Dandenong Road,
    Malvern"), which defeats de-duplication precisely where it matters.
    """
    street = _clean(props.get("adopted_street_name"))
    if not street:
        return None
    parts = [
        _clean(props.get("adopted_street_no")),
        street,
        _clean(props.get("adopted_street_type")),
    ]
    line = " ".join(part for part in parts if part)
    locality = _clean(props.get("adopted_locality"))
    return f"{line}, {locality}" if locality else line


def _address_key(address: str | None) -> str:
    """Normalise an address for de-duplication: case and whitespace only.

    Deliberately conservative. Aggressive normalisation (expanding St/Street,
    stripping unit numbers) would merge genuinely different shopfronts.
    """
    if not address:
        return ""
    return " ".join(str(address).split()).casefold()


def sands_near(lat: float, lng: float, radius_m: int = 200) -> list[dict] | None:
    """Historical business listings near a point, de-duplicated.

    Returns ``[{business_type, anzsic_subdivision, anzsic_subdivision_title,
    address, directories, distance_m, lat, lng}]`` sorted nearest first, where
    ``directories`` is the sorted list of directory years the same
    (address, business_type) pair appeared in. ``None`` if the layer could not
    be read; ``[]`` if it was read and nothing is nearby.

    ``business_name`` is intentionally NOT returned, and ``address`` is the
    geocoder-normalised ``adopted_*`` address rather than the raw directory
    line. In this era a sole trader's business name is the person's name, and
    the raw ``address`` column often embeds that name too; natural-person
    details do not cross into any delivery surface (privacy red line,
    2026-08-27 audit). The pollution-activity mapping works from
    business_type / ANZSIC anyway.

    Default radius is deliberately tight: this layer is dense enough that a
    2km Melbourne CBD bbox matches ~97k raw rows, 500m matches 63,983 and
    200m matches 11,917 (measured 2026-08-27). A call whose bbox exceeds the
    internal feature ceiling returns ``None`` rather than a truncated slice.
    """
    if radius_m <= 0:
        raise ValueError("radius_m must be positive")
    features = _sands_features(lat, lng, radius_m)
    if features is None:
        return None

    grouped: dict[tuple[str, str], dict] = {}
    for feat in features:
        coords = point_coords(feat)
        if coords is None:
            return None
        flat, flng = coords
        props = feat.get("properties")
        if not isinstance(props, dict):
            return None
        dist = _distance_m(lat, lng, flat, flng)
        if dist > radius_m:
            continue  # bbox is a prefilter; the circle is authoritative
        business_type = _clean(props.get("business_type")) or "Unknown"
        address = _adopted_address(props)
        # Fall back to the raw directory line for the KEY only when the
        # geocoder left no normalised address; it never reaches the output.
        key = (_address_key(address or props.get("address")),
               business_type.casefold())
        year = props.get("directory")
        try:
            year = int(year) if year is not None else None
        except (TypeError, ValueError):
            year = None

        existing = grouped.get(key)
        if existing is None:
            grouped[key] = {
                "business_type": business_type,
                "anzsic_subdivision": props.get("anzsic_subdivision"),
                "anzsic_subdivision_title": _clean(
                    props.get("anzsic_subdivision_title")),
                "address": address,
                "directories": [year] if year is not None else [],
                "distance_m": round(dist),
                "lat": round(flat, 6),
                "lng": round(flng, 6),
            }
            continue
        if year is not None and year not in existing["directories"]:
            existing["directories"].append(year)
        if round(dist) < existing["distance_m"]:
            # Same listing geocoded slightly differently across years: keep
            # the closest fix so the proximity band is not pessimistic.
            existing["distance_m"] = round(dist)
            existing["lat"] = round(flat, 6)
            existing["lng"] = round(flng, 6)

    results = list(grouped.values())
    for item in results:
        item["directories"].sort()
    return sorted(results, key=lambda s: (s["distance_m"], s["business_type"]))


def _vlr_record(props: dict, distance_m: float, geom: str) -> dict:
    return {
        "name": _clean(props.get("landfill_name")) or "Unnamed landfill",
        "register_number": props.get("landfill_register_number"),
        "address": _clean(props.get("address")),
        "suburb": _clean(props.get("suburb")),
        "operating_status": _clean(props.get("operating_status")),
        "waste_type_accepted": _clean(props.get("waste_type_accepted")),
        "estimated_year_of_closure": _clean(
            props.get("estimated_year_of_closure")),
        "distance_m": round(distance_m),
        "geom": geom,
    }


def landfills_near(lat: float, lng: float, radius_m: int = 2000) -> list[dict] | None:
    """Victorian Landfill Register entries near a point, nearest first.

    Queries the point and polygon layers concurrently and merges them,
    de-duplicated by ``landfill_register_number`` with the polygon geometry
    preferred (a polygon gives a real distance to the tip face; the point is
    a centroid).
    Includes closed legacy landfills, which is the whole reason this layer is
    interesting. ``None`` on failure, ``[]`` when nothing is nearby.
    """
    if radius_m <= 0:
        raise ValueError("radius_m must be positive")

    # The two layers are independent reads, so run them side by side: serially
    # they cost 2 x (10s timeout + retry) = up to 40s on their own, which is
    # most of the 25s /scores batch deadline by itself (latency review,
    # 2026-08-27). One combined request was measured first and does not work:
    # GeoServer reads a comma-separated ``typeNames`` as a JOIN and answers
    # "Extracted invalid join sub-filter" to the bbox, so two requests it is.
    #
    # A local pool of 2, used and closed inside this call. The signals already
    # run inside a ThreadPoolExecutor, and this deliberately does not add a
    # third nesting level. ``child_context`` carries the caller's fetch budget
    # into both workers; without it they would run unbudgeted.
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_poly = pool.submit(child_context().run, _wfs_features,
                             LAYER_VLR_POLYGON, lat, lng, radius_m, _DEFAULT_COUNT)
        f_point = pool.submit(child_context().run, _wfs_features,
                              LAYER_VLR_POINT, lat, lng, radius_m, _DEFAULT_COUNT)
        poly_feats = f_poly.result()
        point_feats = f_point.result()
    if poly_feats is None or point_feats is None:
        return None

    by_number: dict = {}
    order: list = []

    for feat in poly_feats:
        if not isinstance(feat, dict):
            return None
        props = feat.get("properties")
        rings = polygon_rings(feat.get("geometry"))
        if not isinstance(props, dict) or rings is None:
            return None
        dist = polygon_distance_m(lat, lng, rings)
        if dist > radius_m:
            continue
        key = props.get("landfill_register_number")
        by_number[key] = _vlr_record(props, dist, "polygon")
        order.append(key)

    for feat in point_feats:
        coords = point_coords(feat)
        props = feat.get("properties") if isinstance(feat, dict) else None
        if coords is None or not isinstance(props, dict):
            return None
        flat, flng = coords
        dist = _distance_m(lat, lng, flat, flng)
        if dist > radius_m:
            continue
        key = props.get("landfill_register_number")
        if key in by_number:
            continue  # polygon geometry already covers this register entry
        by_number[key] = _vlr_record(props, dist, "point")
        order.append(key)

    return sorted(by_number.values(), key=lambda s: s["distance_m"])


def gqruz_near(lat: float, lng: float, radius_m: int = 500) -> list[dict] | None:
    """Groundwater Quality Restricted Use Zones near or containing a point.

    Returns ``[{reference_number, address, suburb, site_history,
    restricted_uses, status, aquifer_formation, groundwater_flow_direction,
    map_link, inside, distance_m}]`` sorted nearest first, where
    ``restricted_uses`` is the upstream semicolon-separated list split into
    items and ``inside`` is True when the point falls within the zone.
    ``distance_m`` is 0 for an inside hit.

    ``None`` on failure, ``[]`` when the layer holds nothing nearby. Only the
    polygon layer is queried: the point layer is the same records' centroids.

    Caveat measured 2026-08-27: upstream stores ``restricted_uses`` in a
    fixed-width column and clips it around 200 characters, so the final entry
    of a long list can arrive cut off mid-word. That is the publisher's data,
    not a parsing bug, and it means the list must not be presented as
    exhaustive; the linked ``map_link`` PDF is the authoritative statement.
    """
    if radius_m <= 0:
        raise ValueError("radius_m must be positive")
    features = _wfs_features(LAYER_GQRUZ_POLYGON, lat, lng, radius_m, _DEFAULT_COUNT)
    if features is None:
        return None

    results = []
    for feat in features:
        if not isinstance(feat, dict):
            return None
        props = feat.get("properties")
        rings = polygon_rings(feat.get("geometry"))
        if not isinstance(props, dict) or rings is None:
            return None
        dist = polygon_distance_m(lat, lng, rings)
        if dist > radius_m:
            continue
        raw_uses = _clean(props.get("restricted_uses")) or ""
        results.append({
            "reference_number": _clean(props.get("reference_number")),
            "address": _clean(props.get("address")),
            "suburb": _clean(props.get("suburb")),
            "site_history": _clean(props.get("site_history")),
            "restricted_uses": [u.strip() for u in raw_uses.split(";") if u.strip()],
            "status": _clean(props.get("status")),
            "aquifer_formation": _clean(props.get("aquifer_formation")),
            "groundwater_flow_direction": _clean(
                props.get("groundwater_flow_direction")),
            "map_link": _clean(props.get("map_link")),
            "inside": dist == 0.0,
            "distance_m": round(dist),
        })
    return sorted(results, key=lambda z: z["distance_m"])
