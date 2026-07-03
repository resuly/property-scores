"""Local flood overlay source: the features.duckdb layer library baked by the
da_leads tiles pipeline.

Why: the flood score used to query live state ArcGIS services while the
customer-visible hazards block was served from this library — two sources
that could (and did) contradict each other in one API response (Rocklea:
official 1% AEP polygon under hazards.flood, official_layer "none" in the
score). Reading the SAME library makes disagreement structurally impossible
and drops the flaky live-service dependency for covered states.

Whitelist discipline: only sources whose severity semantics were verified
feed the score. Council "Flood Assessment Area" / development-constraint
trigger layers stay visible in the hazards block but do not move the number.

Trust classes: 'full' states (statewide statutory overlays) may vouch a
clean no-hit; 'hit_only' states have partial library coverage, so a miss
says nothing and the satellite/terrain evidence carries the estimate.
"""
from __future__ import annotations

import json
import logging
import os
from threading import Lock

log = logging.getLogger(__name__)

FEATURES_DB = os.environ.get("FEATURES_DB", "/data/features/features.duckdb")

_SEVERITY_RANK = {"floodway": 0, "flood": 1, "moderate": 2}

# state -> trust class. Absent state = library cannot answer, fall back to
# the remote endpoints.
TRUST: dict[str, str] = {
    "vic": "full",       # statewide VicPlan LSIO/FO/RFO
    "act": "full",       # statewide 1% AEP extent (same dataset as remote)
    "nsw": "hit_only",   # SEPP precinct extents + council mosaic: partial
    "qld": "hit_only",   # Brisbane FAM + council mosaic: partial
    "wa":  "hit_only",   # DoT floodplain dataset: mapped floodplains only
}

# NSW statewide SEPP layer: category -> severity. PMF is far beyond the 1%
# AEP standard, so it must not read as a hard flood hit.
_NSW_CATEGORY_KIND = {
    "Flood Planning Area": "flood",
    "1 in 100 AEP Flood Extent": "flood",
    "Probable Maximum Flood Line": "moderate",
    "Flood Prone and Major Creeks Land": "moderate",
}


def _classify(source: str, props: dict) -> tuple[str, str] | None:
    """(severity_kind, label) for a whitelisted source hit, else None."""
    if source in ("vic_hazard_flood", "vic_hazard_flood_lsio"):
        return "flood", props.get("category") or "LSIO - Land Subject to Inundation Overlay"
    if source == "vic_hazard_flood_fo":
        return "floodway", props.get("category") or "FO/RFO - Floodway Overlay"
    if source == "act_hazard_flood":
        return "flood", "1% AEP Flood Extent (ACT)"
    if source == "qld_hazard_flood_brisbane_fam":
        return "flood", props.get("category") or "Brisbane 1% AEP flood (FAM)"
    if source == "wa_hazard_flood":
        code = (props.get("code") or "").strip()
        kind = "floodway" if code.lower() == "floodway" else "flood"
        label = f"WA floodplain: {code or 'mapped extent'}"
        return kind, label
    if source == "nsw_hazard_flood":
        cat = (props.get("category") or "").strip()
        kind = _NSW_CATEGORY_KIND.get(cat, "moderate")
        return kind, cat or "NSW SEPP flood extent"
    return None


_conn = None
_conn_ino: int | None = None
_conn_lock = Lock()


def _get_conn():
    """Read-only DuckDB handle, reopened when the bake swaps the file (the
    atomic rename changes the inode; a held handle would pin the old file)."""
    global _conn, _conn_ino
    try:
        ino = os.stat(FEATURES_DB).st_ino
    except OSError:
        return None
    with _conn_lock:
        if _conn is not None and ino == _conn_ino:
            return _conn
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None
        try:
            import duckdb

            c = duckdb.connect(FEATURES_DB, read_only=True)
            c.execute("LOAD spatial")
            _conn, _conn_ino = c, ino
            return _conn
        except Exception:
            log.warning("local flood overlay library unavailable", exc_info=True)
            return None


def check(state: str, lat: float, lng: float) -> dict | None:
    """Query the library for whitelisted flood overlay hits at a point.

    Returns {"worst": kind|None, "hit_zones": [labels], "trust": class}
    or None when the library cannot answer for this state (caller falls
    back to the remote endpoints)."""
    st = (state or "").lower()
    trust = TRUST.get(st)
    if trust is None:
        return None
    conn = _get_conn()
    if conn is None:
        return None
    try:
        rows = conn.execute(
            "SELECT source, props FROM features "
            "WHERE state = ? AND category = 'flood' "
            "AND ST_Contains(geom, ST_Point(?, ?))",
            [st, float(lng), float(lat)],
        ).fetchall()
    except Exception:
        log.warning("local flood overlay query failed", exc_info=True)
        return None

    worst: str | None = None
    labels: list[str] = []
    for source, props_raw in rows:
        try:
            props = json.loads(props_raw) if isinstance(props_raw, str) else (props_raw or {})
        except (json.JSONDecodeError, TypeError):
            props = {}
        classified = _classify(source, props)
        if classified is None:
            continue  # visible in the hazards block, not score-bearing
        kind, label = classified
        if label not in labels:
            labels.append(label)
        if worst is None or _SEVERITY_RANK[kind] < _SEVERITY_RANK[worst]:
            worst = kind
    return {"worst": worst, "hit_zones": labels, "trust": trust}


# ---------------------------------------------------------------------------
# Bushfire: same library, same discipline. Only VIC's BMO is baked so far
# (5,225 statewide statutory polygons, the same VicPlan dataset the remote
# endpoint serves), so VIC switches to the library and every other state
# keeps its remote path untouched. Extend BUSHFIRE_TRUST as NSW BFPL /
# SA / WA / TAS layers get baked.
# ---------------------------------------------------------------------------

BUSHFIRE_TRUST: dict[str, str] = {
    "vic": "full",   # BMO is statewide statutory: a clean miss is meaningful
}


def check_bushfire(state: str, lat: float, lng: float) -> dict | None:
    """Whitelisted bushfire overlay hits from the local library.

    Returns {"worst": severity|None, "hit_zones": [labels],
    "category": str|None, "trust": class} or None when the library cannot
    answer for this state (caller falls back to the remote endpoints)."""
    st = (state or "").lower()
    trust = BUSHFIRE_TRUST.get(st)
    if trust is None:
        return None
    conn = _get_conn()
    if conn is None:
        return None
    try:
        rows = conn.execute(
            "SELECT source, props FROM features "
            "WHERE state = ? AND category = 'bushfire' "
            "AND ST_Contains(geom, ST_Point(?, ?))",
            [st, float(lng), float(lat)],
        ).fetchall()
    except Exception:
        log.warning("local bushfire overlay query failed", exc_info=True)
        return None

    worst: str | None = None
    labels: list[str] = []
    category: str | None = None
    for source, props_raw in rows:
        if source != "vic_hazard_bushfire":
            continue  # non-whitelisted: visible in the layers block only
        try:
            props = json.loads(props_raw) if isinstance(props_raw, str) else (props_raw or {})
        except (json.JSONDecodeError, TypeError):
            props = {}
        label = "Bushfire Management Overlay (BMO)"
        if label not in labels:
            labels.append(label)
        worst = "high"                       # BMO maps to the remote's severity
        category = props.get("code") or "BMO"
    return {"worst": worst, "hit_zones": labels, "category": category, "trust": trust}
