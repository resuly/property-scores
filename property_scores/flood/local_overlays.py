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


# --- reg-09: graded flood hazard (ARR/AIDR combined depth x velocity) -------
# Council flood studies classify the 1% AEP floodplain into H1..H6 by combined
# depth x velocity hazard, not a single binary "in the flood zone". H1 is
# shallow/slow nuisance water; H6 is life-threatening to people and all
# building types. Baking that class lets the score separate a 0.1 m ponding
# hit from a 2 m floodway hit instead of flagging both the same (the Skirving
# St over-report class). Dormant until the da_leads library is re-baked with a
# hazard class in props; existing extent-only sources are untouched.
_HAZARD_CLASS_KIND = {
    "H1": "moderate",   # generally safe for people, vehicles and buildings
    "H2": "flood",      # unsafe for small vehicles
    "H3": "flood",      # unsafe for vehicles, children and the elderly
    "H4": "floodway",   # unsafe for people and vehicles
    "H5": "floodway",   # unsafe for vehicles and people; buildings vulnerable
    "H6": "floodway",   # unsafe for all; all building types vulnerable
}
_HAZARD_CLASS_DESC = {
    "H1": "H1 - shallow/slow, generally safe",
    "H2": "H2 - unsafe for small vehicles",
    "H3": "H3 - unsafe for vehicles, children, elderly",
    "H4": "H4 - unsafe for people and vehicles",
    "H5": "H5 - dangerous; buildings vulnerable",
    "H6": "H6 - extreme; all buildings vulnerable",
}
# Coarse low/medium/high hazard maps (councils that publish 3 classes, not 6).
_COARSE_HAZARD = {"low": "H1", "medium": "H3", "med": "H3", "high": "H5"}


def _hazard_class(props: dict) -> str | None:
    """Normalise a baked hazard attribute to H1..H6, else None.

    Accepts the encodings councils actually publish: an explicit H-class
    ('H3'/'h3'), an ARR gridcode 1..6, or a coarse low/medium/high band.
    Reads a normalised `hazard_class` first (set at bake time), then falls
    back to raw source fields so the classifier is robust to bake naming."""
    raw = props.get("hazard_class")
    if raw is None:
        for key in ("gridcode", "HAZARD", "Hazard", "hazard", "OVL2_DESC",
                    "class", "category", "desc"):
            if props.get(key) not in (None, ""):
                raw = props[key]
                break
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if s.startswith("h") and s[1:2].isdigit():
        return f"H{s[1]}" if s[1] in "123456" else None
    if s in ("1", "2", "3", "4", "5", "6"):
        return f"H{s}"
    for word, hcls in _COARSE_HAZARD.items():
        if word in s:
            return hcls
    return None


def _classify(source: str, props: dict) -> tuple[str, str] | None:
    """(severity_kind, label) for a whitelisted source hit, else None."""
    # reg-09: graded hazard sources (suffix _flood_hazard) carry an H-class
    # that drives severity; a coarse-extent hit on the same council is
    # superseded by the graded read where present.
    if source.endswith("_flood_hazard"):
        hcls = _hazard_class(props)
        if hcls is None:
            return None  # hazard source but unclassifiable value: don't move the number
        return _HAZARD_CLASS_KIND[hcls], _HAZARD_CLASS_DESC[hcls]
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
    hazard: dict | None = None  # reg-09: worst graded H-class hit + provenance
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
        # capture the most severe graded hazard, with provenance for the report
        if source.endswith("_flood_hazard"):
            hcls = _hazard_class(props)
            if hcls is not None and (hazard is None or hcls > hazard["hazard_class"]):
                hazard = {
                    "hazard_class": hcls,
                    "description": _HAZARD_CLASS_DESC[hcls],
                    "source": props.get("study") or source,
                    "aep": props.get("aep") or "1% AEP",
                    "year": props.get("year"),
                    "licence": props.get("licence"),
                }
    result = {"worst": worst, "hit_zones": labels, "trust": trust}
    if hazard is not None:
        result["hazard"] = hazard
    return result


# ---------------------------------------------------------------------------
# Bushfire: same library, same discipline. Six states baked as of
# 2026-07-06 (VIC BMO, QLD QFES BPA 2.56M, NSW BFPL, WA OBRM, SA P&D Code
# overlays, TAS Bushfire-prone Areas Code) — all full-precision direct
# downloads of the same datasets the remote endpoints serve, so severity
# classification below mirrors bushfire.score._check_layer exactly and
# only the basis changes. ACT/NT stay remote/modelled.
# ---------------------------------------------------------------------------

BUSHFIRE_TRUST: dict[str, str] = {
    "vic": "full",   # BMO statewide statutory: a clean miss is meaningful
    "qld": "full",   # QFES BPA statewide (July 2017 official release)
    "nsw": "full",   # Bushfire Prone Land statewide statutory (CC BY 4.0)
    "wa":  "full",   # OBRM Map of Bush Fire Prone Areas statewide
    "sa":  "full",   # Planning & Design Code hazard overlays, all six classes
    "tas": "full",   # Bushfire-prone Areas Code overlay (LIST layer 14)
}

# Bushfire severities rank on the score module's scale — NOT the flood
# _SEVERITY_RANK above, which ranks flood overlay kinds.
_BUSHFIRE_SEVERITY_RANK = {"extreme": 0, "high": 1, "moderate": 2, "low": 3}

# Mirrors bushfire.score.NSW_CATEGORY_MAP — duplicated (not imported) to
# avoid a load-time import cycle with bushfire.score; these are stable
# statutory categories.
_NSW_BUSHFIRE_CATEGORY = {
    "Vegetation Category 1": "extreme",
    "Vegetation Category 2": "high",
    "Vegetation Category 3": "moderate",
    "Vegetation Buffer": "low",
}

# Official severity ordering per the SA Planning & Design Code: High Risk
# outranks Urban Interface (same ordering as the remote ENDPOINTS list).
_SA_BUSHFIRE_CATEGORY = {
    "High Risk": "high",
    "Urban Interface": "moderate",
    "Medium Risk": "moderate",
    "General Risk": "low",
    "Regional": "low",
    "Outback": "low",
}


def _classify_bushfire(source: str, props: dict) -> tuple[str, str] | None:
    """(severity, label) for a whitelisted bushfire source hit, else None.

    Severity semantics mirror bushfire.score._check_layer per state so a
    local_library basis scores identically to the remote state services."""
    if source == "vic_hazard_bushfire":
        return "high", "Bushfire Management Overlay (BMO)"
    if source == "qld_hazard_bushfire":
        cls = str(props.get("desc") or props.get("category") or "")
        low = cls.lower()
        if "very high" in low:
            sev = "high"
        elif "high" in low or "medium" in low:
            sev = "moderate"
        else:
            sev = "low"
        return sev, cls or "Bushfire Prone Area (QLD QFD 2017)"
    if source == "nsw_hazard_bushfire":
        cat = str(props.get("category") or "")
        return (_NSW_BUSHFIRE_CATEGORY.get(cat, "high"),
                cat or "Bushfire Prone Land")
    if source == "wa_hazard_bushfire":
        return "moderate", "Bush Fire Prone Area (OBRM-023)"
    if source == "sa_hazard_bushfire":
        cat = str(props.get("category") or "")
        label = f"Hazards (Bushfire - {cat})" if cat else "Bushfire hazard overlay"
        return _SA_BUSHFIRE_CATEGORY.get(cat, "moderate"), label
    if source == "tas_hazard_bushfire":
        return "moderate", str(props.get("desc") or "Bushfire-prone areas")
    return None


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
        try:
            props = json.loads(props_raw) if isinstance(props_raw, str) else (props_raw or {})
        except (json.JSONDecodeError, TypeError):
            props = {}
        classified = _classify_bushfire(source, props)
        if classified is None:
            continue  # non-whitelisted: visible in the layers block only
        sev, label = classified
        if label not in labels:
            labels.append(label)
        if worst is None or _BUSHFIRE_SEVERITY_RANK[sev] < _BUSHFIRE_SEVERITY_RANK[worst]:
            worst = sev
            # VIC keeps its historical category (zone code); other states
            # surface the classified label, same as the remote detail.
            if source == "vic_hazard_bushfire":
                category = props.get("code") or "BMO"
            else:
                category = label
    return {"worst": worst, "hit_zones": labels, "category": category, "trust": trust}
