"""ACT Register of contaminated sites joined to the official block cadastre.

The register is a CC BY 4.0 ACT Government SODA dataset. It has no geometry,
so a row is delivered only after its district/division/section/block identity
matches an active ACTGOV Block polygon containing the query point.

``None`` means either publisher could not be read or the subject block could
not be resolved. ``[]`` means both sources were checked and the subject block
has no register row. Failed refreshes may use a recent last-good in-process
register snapshot; snapshots older than the bounded grace period fail closed.
"""
from __future__ import annotations

import logging
import hashlib
import re
import time as _time

from property_scores.contamination.sources._common import (
    fetch_json,
    geojson_features_or_none,
)

logger = logging.getLogger(__name__)

REGISTER_URL = "https://www.data.act.gov.au/resource/ecgf-jdca.json"
BLOCK_QUERY_URL = (
    "https://services1.arcgis.com/E5n4f1VY84i0xSjy/arcgis/rest/services/"
    "ACTGOV_BLOCKS/FeatureServer/0/query"
)
REGISTER_CACHE_TTL_S = 24 * 3600
REGISTER_MAX_STALE_S = 7 * 24 * 3600
_MAX_REGISTER_ROWS = 5_000
_ACTIVE_LIFECYCLES = {"PROPOSED", "REGISTERED", "APPROVED", "OCCUPIED"}
_REGISTER_DISTRICTS = {
    "BELCONNEN", "CANBERRA CENTRAL", "GUNGAHLIN", "JERRABOMBERRA",
    "MAJURA", "MOLONGLO VALLEY", "PADDYS RIVER", "TUGGERANONG",
    "WESTON CREEK", "WODEN VALLEY",
}
_cache: tuple[list[dict], float] | None = None


def _clean(value) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split()).strip()


def _upper(value) -> str:
    return _clean(value).upper()


def _number(value) -> str:
    text = _clean(value)
    try:
        return str(int(float(text)))
    except (TypeError, ValueError):
        return text.upper()


def _current_or_notified(row: dict, field: str) -> str:
    current = _clean(row.get(f"{field}_current"))
    return current or _clean(row.get(f"{field}_notified"))


def _district_division(row: dict) -> tuple[str, str]:
    district = _upper(_current_or_notified(row, "district"))
    division = _upper(_current_or_notified(row, "division"))
    # One current publisher row has these two fields reversed (Former
    # Newcastle House: district=Fyshwick, division=Canberra Central). Correct
    # only the structurally provable orientation; unknown names remain as-is
    # and therefore fail to match rather than creating a false parcel hit.
    if district not in _REGISTER_DISTRICTS and division in _REGISTER_DISTRICTS:
        district, division = division, district
    return district, division


def _parse_register_rows(data, expected_count: int) -> list[dict] | None:
    if not isinstance(data, list) or len(data) != expected_count:
        return None
    rows = []
    seen = set()
    for raw in data:
        if not isinstance(raw, dict):
            return None
        district, division = _district_division(raw)
        section = _number(_current_or_notified(raw, "section"))
        block = _number(_current_or_notified(raw, "block"))
        notified = _clean(raw.get("notified_under_section")) or "Not specified"
        description = (_clean(raw.get("site_description"))
                       or "ACT contaminated sites register entry")
        site_id = _clean(raw.get("id"))
        if not site_id:
            identity = "|".join(
                (district, division, section, block, notified, description))
            site_id = "derived-" + hashlib.sha256(
                identity.encode("utf-8")).hexdigest()[:12]
        if (site_id in seen or not district or not section or not block):
            return None
        seen.add(site_id)
        rows.append({
            "site_id": site_id,
            "district": district,
            "division": division,
            "section": section,
            "block": block,
            "notified_under_section": notified,
            "site_description": description,
        })
    return rows


def fetch_all_rows() -> list[dict] | None:
    count_data = fetch_json(REGISTER_URL, {"$select": "count(*)"})
    try:
        expected = int(count_data[0]["count"])
    except (TypeError, ValueError, KeyError, IndexError):
        return None
    if expected < 1 or expected > _MAX_REGISTER_ROWS:
        return None
    data = fetch_json(REGISTER_URL, {
        "$limit": _MAX_REGISTER_ROWS,
        "$order": "id",
    })
    return _parse_register_rows(data, expected)


def all_rows(force_refresh: bool = False) -> list[dict] | None:
    global _cache
    now = _time.time()
    if not force_refresh and _cache is not None:
        rows, ts = _cache
        if now - ts < REGISTER_CACHE_TTL_S:
            return rows
    rows = fetch_all_rows()
    if rows is None:
        if _cache is not None:
            cached_rows, cached_at = _cache
            age_s = now - cached_at
            if age_s <= REGISTER_MAX_STALE_S:
                logger.warning(
                    "ACT contaminated-sites refresh failed; serving recent "
                    "last-good cache age_s=%.0f", age_s)
                return cached_rows
            logger.error(
                "ACT contaminated-sites refresh failed; last-good cache "
                "expired age_s=%.0f max_stale_s=%s",
                age_s, REGISTER_MAX_STALE_S)
        return None
    _cache = (rows, now)
    return rows


def clear_cache() -> None:
    global _cache
    _cache = None


def _subject_blocks(lat: float, lng: float) -> list[dict] | None:
    data = fetch_json(BLOCK_QUERY_URL, {
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": (
            "OBJECTID,DISTRICT_NAME,DIVISION_NAME,SECTION_NUMBER,"
            "BLOCK_NUMBER,CURRENT_LIFECYCLE_STAGE"
        ),
        "returnGeometry": "false",
        "f": "json",
    })
    features = geojson_features_or_none(data)
    if features is None:
        return None
    blocks = []
    for feature in features:
        attrs = feature.get("attributes") if isinstance(feature, dict) else None
        if not isinstance(attrs, dict):
            return None
        lifecycle = _upper(attrs.get("CURRENT_LIFECYCLE_STAGE"))
        if lifecycle not in _ACTIVE_LIFECYCLES:
            continue
        district = _upper(attrs.get("DISTRICT_NAME"))
        division = _upper(attrs.get("DIVISION_NAME"))
        section = _number(attrs.get("SECTION_NUMBER"))
        block = _number(attrs.get("BLOCK_NUMBER"))
        if not district or not section or not block:
            return None
        blocks.append({
            "district": district,
            "division": division,
            "section": section,
            "block": block,
        })
    return blocks or None


def _block_tokens(value: str) -> set[str]:
    return {
        _number(token)
        for token in re.split(r"[+,;/&]", value)
        if _number(token)
    }


def _matches(register_row: dict, block: dict) -> bool:
    if register_row["district"] != block["district"]:
        return False
    if register_row["section"] != block["section"]:
        return False
    register_division = register_row["division"]
    if register_division not in {"", "-"} and register_division != block["division"]:
        return False
    return block["block"] in _block_tokens(register_row["block"])


def sites_at(lat: float, lng: float) -> list[dict] | None:
    """Official register rows attached to the ACT block at ``lat,lng``."""
    blocks = _subject_blocks(lat, lng)
    if blocks is None:
        return None
    rows = all_rows()
    if rows is None:
        return None
    hits = []
    for row in rows:
        if not any(_matches(row, block) for block in blocks):
            continue
        hits.append({
            "site_id": row["site_id"],
            "name": row["site_description"],
            "issue": (
                f"ACT Register of contaminated sites, notified under "
                f"section {row['notified_under_section']}"
            ),
            "activity_type": "ACT contaminated sites register",
            "management_class": row["notified_under_section"],
            "distance_m": 0,
            "geom": "polygon",
            "source": "ACT EPA Register of contaminated sites",
        })
    return sorted(hits, key=lambda row: row["site_id"])
