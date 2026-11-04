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
import time
from threading import Lock

from property_scores.common import model_version
from property_scores.flood.shards import ShardReader

log = logging.getLogger(__name__)

FEATURES_DB = os.environ.get("FEATURES_DB", "/data/features/features.duckdb")
FEATURES_MANIFEST = os.environ.get("FEATURES_MANIFEST", "/data/features/manifest.json")

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
_RESTRICTED_GRADED_HAZARD_SOURCES = {
    "nsw_hazard_flood_newcastle",
    "nsw_newcastle_flood_hazard",
}

def _is_graded_hazard_source(source: str) -> bool:
    # Only the generic contract is accepted here. Newcastle's production ID
    # is intentionally excluded: its source terms do not permit commercial
    # redistribution, so it must not influence the commercial score payload.
    return (source not in _RESTRICTED_GRADED_HAZARD_SOURCES
            and source.endswith("_flood_hazard"))


def _hazard_class(props: dict) -> str | None:
    """Normalise a baked hazard attribute to H1..H6, else None.

    Accepts the encodings councils actually publish: an explicit H-class
    ('H3'/'h3'), an ARR gridcode 1..6, or a coarse low/medium/high band.
    Reads a normalised `hazard_class` first (set at bake time), then falls
    back to raw source fields so the classifier is robust to bake naming."""
    raw = props.get("hazard_class")
    if raw is None:
        # 'severity' is the da_leads bake canonical key the Newcastle Hazard
        # field (1..6) maps onto via states.yaml `fields: {severity: Hazard}`.
        for key in ("severity", "gridcode", "HAZARD", "Hazard", "hazard",
                    "OVL2_DESC", "class", "category", "desc"):
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


# --- score model v2: council / statutory layers with verified semantics ------
# Gated on model_version.at_least("v2"): v1 (Foundit's pinned model) never
# sees these. Every entry is (a) on the da_leads hazard geometry allowlist
# (web/property_intel_routes._HAZARD_GEOMETRY_SOURCES, item-level CC BY from
# the 2026-08-04/05 rights audit) AND (b) checked against the publisher's own
# record for what the polygon means. Mapping follows the existing scale:
# a 1% AEP extent or flood planning area is "flood" (same as NSW EPI "Flood
# Planning Area" and VIC LSIO); overland-flow planning areas and statutory
# inundation areas that publish no severity are "moderate" (same as the
# remote VIC SBO entry). Matching is on the exact baked category string, so a
# re-bake that renames a category drops out of the score instead of being
# guessed at.
#
# Deliberately NOT here (still display-only), with the reason:
#   nsw_hazard_flood_ses          content is the Hornsby FRMSP depth extent
#                                 (SES dataset licence "3rd"), not the Northern
#                                 Lakes study the audit cleared; rights finding
#   qld_hazard_flood_gold_coast   "Flood assessment required": a code trigger
#                                 with no published extent definition
#   qld_hazard_flood_logan_city   "Flood assessment area": TLPI trigger area
#   qld_hazard_flood_redland_city CC BY-SA 4.0; a score derived from it is a
#                                 share-alike question for Bo, not a mapping
#   qld_hazard_flood_floodplain   SPP floodplain assessment overlay: whole
#                                 sub-basins (Phase 1/2/3), not an extent
#   qld QFMP basin extents        not yet rights-audited
#   nsw_hazard_flood_georges_river 1% AEP extent, but the baked multipolygon
#                                 is self-intersecting: point-in-polygon on it
#                                 disagrees with ST_MakeValid on 14 of 42 hits
#                                 (2026-11-05 G-NAF sample). Repair at ingest
#                                 first, then add it here as "flood".
_V2_KRG_MAINSTREAM = {
    "Lovers Jump Creek - Mainstream Flow", "Blackbutt Creek - Mainstream Flow",
    "Middle Harbour South - Mainstream Flow",
    "Middle Harbour North - Mainstream Flow",
}
_V2_KRG_OVERLAND = {
    "Lovers Jump Creek - Overland FPA", "Blackbutt Creek - Overland FPA",
    "Middle Harbour South - Overland FPA", "Middle Harbour North - Overland FPA",
}
# source -> {baked category: (kind, label)}
_V2_COUNCIL_LAYERS: dict[str, dict[str, tuple[str, str]]] = {
    # Ku-ring-gai Hydrology service: council FAQ defines the FPA as the 1% AEP
    # flood plus 500 mm freeboard (mainstream) / 300 mm (overland flow).
    "nsw_hazard_flood_ku_ring_gai": {
        **{c: ("flood", f"Ku-ring-gai mainstream flood planning area "
                        f"({c.split(' - ')[0]})") for c in _V2_KRG_MAINSTREAM},
        **{c: ("moderate", f"Ku-ring-gai overland flow flood planning area "
                           f"({c.split(' - ')[0]})") for c in _V2_KRG_OVERLAND},
    },
    # SES record hawkesbury-frmsp2025-flood-planning-area (cc-by): the
    # harvester labels it "1% AEP Flood Extent"; the dataset is the FRMSP 2025
    # Flood Planning Area.
    "nsw_hazard_flood_hawkesbury": {
        "1% AEP Flood Extent": ("flood", "Hawkesbury flood planning area "
                                         "(FRMSP 2025, 1% AEP)"),
    },
    # SES record design_flood_extents (cc-by), file 1AEP_Extent.
    "nsw_hazard_flood_campbelltown": {
        "1% AEP flood extent": ("flood", "Campbelltown 1% AEP flood extent"),
    },
    # SCC Planning Scheme 2014 Flood Hazard Overlay "Flooding and Inundation
    # Area": modelled DFE / storm-tide inundation, and the publisher says it
    # "does not specify a level of hazard" -> moderate, never flood.
    "qld_hazard_flood_sunshine_coast": {
        "Land Subject to Flood Hazard Overlay": (
            "moderate", "Sunshine Coast flooding and inundation area"),
    },
    # VicPlan layer 16 Special Building Overlay (overland flow), the same
    # "moderate" the remote ENDPOINTS entry has always used. Dormant until the
    # library carries it; until then check() asks VicPlan (see
    # _vic_sbo_supplement).
    "vic_hazard_flood_sbo": {
        "*": ("moderate", "SBO - Special Building Overlay"),
    },
}


def _classify_v2(source: str, props: dict) -> tuple[str, str] | None:
    layer = _V2_COUNCIL_LAYERS.get(source)
    if layer is None:
        return None
    cat = str(props.get("category") or "").strip()
    return layer.get(cat) or layer.get("*")


def _classify(source: str, props: dict) -> tuple[str, str] | None:
    """(severity_kind, label) for a whitelisted source hit, else None."""
    if model_version.at_least("v2"):
        v2 = _classify_v2(source, props)
        if v2 is not None:
            return v2
    # reg-09: graded hazard sources (suffix _flood_hazard) carry an H-class
    # that drives severity; a coarse-extent hit on the same council is
    # superseded by the graded read where present.
    if _is_graded_hazard_source(source):
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
    """A per-call CURSOR off one shared read-only DuckDB handle.

    The base handle is reopened when the bake swaps the file (the atomic rename
    changes the inode; a held handle would pin the old file).

    The cursor matters. A single DuckDB connection is not safe to drive from
    several threads at once, and this module is called from exactly that: the
    /scores batch path scores flood and bushfire in parallel, and both land
    here. Sharing the raw handle produced

        _duckdb.InternalException: Attempted to dereference unique_ptr
        that is NULL!

    which check() catches and turns into "the library cannot answer", so the
    flood score silently fell back off the official overlay. Measured against
    production on 2026-07-25 at 100 Sherwood Road Rocklea, a flood-plain
    address: serial calls returned 35 "High Risk" with the overlay hit every
    time, while at concurrency 4 two of eight and at concurrency 8 four of
    sixteen came back 60 "Moderate Risk" with official_layer
    checked_partial_coverage and no zones. Wrong, in the reassuring direction,
    on the score that matters most, and then cached for 90 days downstream.

    Separate cursors off one database instance are the supported multi-thread
    pattern, and every query here is read-only, so they cannot collide on
    catalog state. This mirrors common/overture.get_db(), which already
    documents the same reasoning; this module never picked it up.
    """
    global _conn, _conn_ino
    try:
        ino = os.stat(FEATURES_DB).st_ino
    except OSError:
        return None
    with _conn_lock:
        if _conn is None or ino != _conn_ino:
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
            except Exception:
                log.warning("local flood overlay library unavailable", exc_info=True)
                return None
        try:
            return _conn.cursor()
        except Exception:
            log.warning("local flood overlay cursor unavailable", exc_info=True)
            return None


_reader_lock = Lock()
_reader: ShardReader | None = None


def _get_reader() -> ShardReader | None:
    global _reader
    if not os.path.exists(FEATURES_MANIFEST):
        return None
    with _reader_lock:
        if _reader is None or _reader.manifest_path != FEATURES_MANIFEST:
            if _reader is not None:
                try:
                    _reader.close()
                except Exception:
                    pass
            _reader = ShardReader(FEATURES_MANIFEST)
        return _reader


def check(state: str, lat: float, lng: float) -> dict | None:
    """Query the library for whitelisted flood overlay hits at a point.

    Returns {"worst": kind|None, "hit_zones": [labels], "trust": class}
    or None when the library cannot answer for this state (caller falls
    back to the remote endpoints)."""
    st = (state or "").lower()
    trust = TRUST.get(st)
    if trust is None:
        return None
    reader = _get_reader()
    if reader is not None:
        try:
            rows = reader.query(
                "source, props",
                "state = ? AND category = 'flood' AND ST_Contains(geom, ST_Point(?, ?))",
                [st, float(lng), float(lat)],
                categories=["flood"],
                states=[st],
            )
        except Exception:
            log.warning("sharded flood overlay query failed", exc_info=True)
            return None
    else:
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
        if _is_graded_hazard_source(source):
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
    if st == "vic" and model_version.at_least("v2"):
        _vic_sbo_supplement(result, reader, rows, lat, lng)
    return result


_VIC_SBO_SOURCE = "vic_hazard_flood_sbo"
_SBO_WARNING = "Could not reach VicPlan SBO (layer 16)"
_SBO_SKIPPED = "VicPlan SBO (layer 16) not checked: recent failures, retrying shortly"
# (connect, read) seconds. A VIC lookup must not hang on VicPlan: the remote
# path's 10 s TIMEOUT is too long for a call made on every VIC address.
_SBO_TIMEOUT = (3.0, 3.5)
# Process-local circuit breaker: after this many consecutive failures, skip the
# call for _SBO_OPEN_S seconds (the answer is then reported as incomplete, not
# clean). Bounds the cost of a VicPlan outage to one short window per worker.
_SBO_FAILS_TO_OPEN = 3
_SBO_OPEN_S = 300.0
_sbo_lock = Lock()
_sbo_state = {"fails": 0, "open_until": 0.0}


def _sbo_breaker_open(now: float) -> bool:
    with _sbo_lock:
        return now < _sbo_state["open_until"]


def _sbo_record(ok: bool, now: float) -> None:
    with _sbo_lock:
        if ok:
            _sbo_state["fails"] = 0
            return
        _sbo_state["fails"] += 1
        if _sbo_state["fails"] >= _SBO_FAILS_TO_OPEN:
            _sbo_state["open_until"] = now + _SBO_OPEN_S
            _sbo_state["fails"] = 0


def _vic_sbo_supplement(result: dict, reader, rows, lat: float, lng: float) -> None:
    """v2: close the SBO gap in the VIC library, in place.

    VIC is a 'full' trust state, so a library miss is scored as a statewide
    statutory clean check (90). But the library has never held the Special
    Building Overlay (da_leads build.py registers LSIO and FO only), so every
    SBO address was "checked clean" and landed at 70-80 Low (2026-11-05
    validation: 16 of 36 SBO addresses Low). The remote path this replaced
    queried SBO as 'moderate'.

    If the library carries the SBO source, _classify already scored it and
    nothing is done here. Otherwise VicPlan layer 16 is asked directly.

    Asked even when the library already hit LSIO/FO: SBO cannot raise the
    severity there, but it is one more mapped zone and the multi-zone penalty
    (-3 per extra zone, same as the pre-2026-07-03 remote path) moves the
    score, e.g. LSIO alone 40 Moderate vs LSIO + SBO 37 High. Skipping would
    change the conclusion, and would make the answer depend on whether SBO
    happens to be baked.

    If VicPlan cannot answer (or the breaker is open), a warning is recorded so
    the consumer does not pin the result, and a miss is no longer a full
    statewide check: trust drops to 'hit_only' rather than vouching a clean 90.
    """
    if any(src == _VIC_SBO_SOURCE for src, _ in rows):
        return
    if reader is not None:
        try:
            if _VIC_SBO_SOURCE in ((reader.manifest() or {}).get("sources") or {}):
                return  # library holds SBO and the point is not in it
        except Exception:
            log.warning("feature manifest unreadable for SBO check", exc_info=True)
    from property_scores.flood.score import VIC_PLAN_BASE, _layer_has_features

    now = time.monotonic()
    if _sbo_breaker_open(now):
        hit, warning = None, _SBO_SKIPPED
    else:
        hit = _layer_has_features(f"{VIC_PLAN_BASE}/16", lat, lng,
                                  timeout=_SBO_TIMEOUT)
        _sbo_record(hit is not None, now)
        warning = _SBO_WARNING
    if hit is None:
        result.setdefault("warnings", []).append(warning)
        if result["worst"] is None:
            result["trust"] = "hit_only"
        return
    if hit:
        kind, label = _V2_COUNCIL_LAYERS[_VIC_SBO_SOURCE]["*"]
        if label not in result["hit_zones"]:
            result["hit_zones"].append(label)
        if result["worst"] is None or _SEVERITY_RANK[kind] < _SEVERITY_RANK[result["worst"]]:
            result["worst"] = kind


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
    reader = _get_reader()
    if reader is not None:
        try:
            rows = reader.query(
                "source, props",
                "state = ? AND category = 'bushfire' AND ST_Contains(geom, ST_Point(?, ?))",
                [st, float(lng), float(lat)],
                categories=["bushfire"],
                states=[st],
            )
        except Exception:
            log.warning("sharded bushfire overlay query failed", exc_info=True)
            return None
    else:
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
