"""AS 3959 Method 1 (simplified procedure) BAL lookup tables.

Source of the distance thresholds: the Victorian Government / CFA public guide
"Assessing a property's Bushfire Attack Level (BAL)" (reproduces the AS 3959-2009
Method 1 Tables 2.4.x for FDI 100 and FDI 50 verbatim). Fetched and transcribed
2026-07-20 from a public reproduction; cross-checked against the FDI-100 Forest
band quoted by multiple Victorian building guides. FDI table (2.1) source:
Geoscience Australia BAL Toolbox docs (open-source, GA 2017).

IMPORTANT — scope of these numbers:
  * These are the AS 3959-2009 tables. AS 3959-2018 revised some construction
    provisions but the Method 1 distance tables are materially the same; this is
    an INDICATIVE pre-screen, not a certified assessment, so 2009 tables are an
    honest basis. Flagged in output as method = "AS 3959-2009 Method 1 (indicative)".
  * Only FDI 100 and FDI 50 are tabulated in the public VIC guide (the two that
    apply in Victoria). FDI 80 (NSW general / SA / WA) and FDI 40 (QLD / NT) are
    NOT reproduced here; for those regions we substitute the nearest MORE
    conservative table and set fdi_substituted=True so callers can widen the
    confidence band. We never invent thresholds we could not cite.

Each row is the ascending distance boundaries (metres):
    (fz_max, bal40_max, bal29_max, bal19_max, bal125_max)
interpreted as:
    d <  fz_max      -> BAL-FZ
    d <  bal40_max   -> BAL-40
    d <  bal29_max   -> BAL-29
    d <  bal19_max   -> BAL-19
    d <  bal125_max  -> BAL-12.5     (bal125_max is always 100 = the AS 3959 cut-off)
    d >= bal125_max  -> BAL-LOW

Vegetation class letters follow AS 3959 Table 2.3:
    A Forest, B Woodland, C Shrubland, D Scrub, E Mallee/Mulga, F Rainforest,
    G Grassland/Tussock moorland (grassland is only assessed under FDI 50).

Slope band keys:
    "flat"  all upslopes and flat land (0 deg)
    "d5"    downslope >0 to 5 deg
    "d10"   downslope >5 to 10 deg
    "d15"   downslope >10 to 15 deg
    "d20"   downslope >15 to 20 deg
    (>20 deg downslope: Method 1 is not valid -> treated as BAL-FZ by the caller)
"""

# BAL labels in ascending severity, indexed by the boundary crossed.
BAL_LABELS = ["BAL-FZ", "BAL-40", "BAL-29", "BAL-19", "BAL-12.5"]
BAL_LOW = "BAL-LOW"

# fmt: off
TABLES = {
    100: {
        "flat": {
            "A": (19, 25, 35, 48, 100),
            "B": (12, 16, 24, 33, 100),
            "C": (10, 13, 19, 27, 100),
            "D": (7,  9,  13, 19, 100),
            "E": (6,  8,  12, 17, 100),
            "F": (8,  11, 16, 23, 100),
        },
        "d5": {
            "A": (24, 32, 43, 57, 100),
            "B": (15, 21, 29, 41, 100),
            "C": (11, 15, 22, 31, 100),
            "D": (7,  10, 15, 22, 100),
            "E": (7,  9,  13, 20, 100),
            "F": (10, 14, 20, 29, 100),
        },
        "d10": {
            "A": (31, 39, 53, 69, 100),
            "B": (20, 26, 37, 50, 100),
            "C": (12, 17, 24, 35, 100),
            "D": (8,  11, 17, 25, 100),
            "E": (7,  10, 15, 23, 100),
            "F": (13, 18, 26, 36, 100),
        },
        "d15": {
            "A": (39, 49, 64, 82, 100),
            "B": (25, 33, 45, 60, 100),
            "C": (14, 19, 28, 39, 100),
            "D": (9,  13, 19, 28, 100),
            "E": (8,  11, 18, 26, 100),
            "F": (17, 23, 33, 45, 100),
        },
        "d20": {
            "A": (50, 61, 78, 98, 100),
            "B": (32, 41, 56, 73, 100),
            "C": (15, 21, 31, 43, 100),
            "D": (10, 15, 22, 31, 100),
            "E": (9,  13, 20, 29, 100),
            "F": (22, 29, 42, 56, 100),
        },
    },
    50: {
        "flat": {
            "A": (12, 16, 23, 32, 100),
            "B": (7,  10, 15, 22, 100),
            "C": (10, 13, 19, 27, 100),
            "D": (7,  9,  13, 19, 100),
            "E": (6,  8,  12, 17, 100),
            "F": (5,  6,  9,  14, 100),
            "G": (7,  9,  14, 20, 100),
        },
        "d5": {
            "A": (14, 19, 27, 38, 100),
            "B": (9,  12, 18, 26, 100),
            "C": (11, 15, 22, 31, 100),
            "D": (7,  10, 15, 22, 100),
            "E": (7,  9,  13, 20, 100),
            "F": (6,  8,  12, 17, 100),
            "G": (8,  10, 16, 23, 100),
        },
        "d10": {
            "A": (18, 24, 34, 46, 100),
            "B": (11, 15, 23, 32, 100),
            "C": (12, 17, 24, 35, 100),
            "D": (8,  11, 17, 25, 100),
            "E": (7,  10, 15, 23, 100),
            "F": (7,  10, 15, 22, 100),
            "G": (9,  12, 18, 26, 100),
        },
        "d15": {
            "A": (22, 30, 41, 56, 100),
            "B": (14, 19, 28, 40, 100),
            "C": (14, 19, 28, 39, 100),
            "D": (9,  13, 19, 28, 100),
            "E": (8,  11, 18, 26, 100),
            "F": (9,  13, 19, 28, 100),
            "G": (10, 13, 20, 29, 100),
        },
        "d20": {
            "A": (28, 37, 51, 67, 100),
            "B": (18, 25, 36, 48, 100),
            "C": (15, 21, 31, 43, 100),
            "D": (10, 15, 22, 31, 100),
            "E": (9,  13, 20, 29, 100),
            "F": (12, 17, 25, 35, 100),
            "G": (11, 15, 23, 33, 100),
        },
    },
}
# fmt: on

# ---------------------------------------------------------------------------
# FDI by jurisdiction — AS 3959 Table 2.1 (via GA BAL Toolbox docs).
# Returns (fdi_value, human_basis). Region-within-state precision (e.g. NSW
# listed coastal = 100 vs general = 80) cannot be resolved from a coordinate
# alone here, so we take the CONSERVATIVE (higher) value for populated states
# and flag the assumption. Alpine (FDI 50) is approximated by elevation.
# ---------------------------------------------------------------------------
FDI_BY_STATE = {
    "VIC": (100, "Table 2.1 Victoria (general)"),
    "NSW": (100, "Table 2.1 NSW — conservative: listed coastal regions = 100 "
                 "(general/inland NSW = 80; region not resolved from coordinate)"),
    "ACT": (100, "Table 2.1 ACT"),
    "SA":  (80,  "Table 2.1 South Australia"),
    "WA":  (80,  "Table 2.1 Western Australia"),
    "TAS": (50,  "Table 2.1 Tasmania"),
    "QLD": (40,  "Table 2.1 Queensland"),
    "NT":  (40,  "Table 2.1 Northern Territory"),
}

ALPINE_ELEV_M = 1200  # rough alpine threshold; VIC/NSW alpine areas use FDI 50


def resolve_fdi(state: str, elevation_m: float | None) -> tuple[int, str]:
    """Return (fdi, basis) for a state, with an elevation-based alpine override."""
    fdi, basis = FDI_BY_STATE.get(state, (100, "unknown state — default FDI 100"))
    if elevation_m is not None and elevation_m >= ALPINE_ELEV_M and state in ("VIC", "NSW"):
        return 50, f"Table 2.1 {state} alpine (elevation {round(elevation_m)} m >= {ALPINE_ELEV_M} m)"
    return fdi, basis


def table_for_fdi(fdi: int) -> tuple[dict, int, bool]:
    """Return (table, table_fdi_used, substituted).

    Only FDI 100 and 50 are tabulated. Substitute the nearest MORE conservative
    table for 80 (->100) and 40 (->50); flag it so the caller widens confidence.
    """
    if fdi in TABLES:
        return TABLES[fdi], fdi, False
    if fdi >= 75:          # 80 -> 100 (conservative)
        return TABLES[100], 100, True
    return TABLES[50], 50, True   # 40 -> 50 (conservative)


def lookup_bal(fdi_table: dict, slope_band: str, veg_class: str,
               distance_m: float) -> str:
    """Map (slope band, veg class, distance) -> BAL label via the AS 3959 row."""
    row = fdi_table.get(slope_band, {}).get(veg_class)
    if row is None:
        return BAL_LOW
    for i, boundary in enumerate(row):
        if distance_m < boundary:
            return BAL_LABELS[i]
    return BAL_LOW
