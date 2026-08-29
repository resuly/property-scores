"""BAL Method 1 lookup data for the preliminary screening model.

The numeric distance limits are adapted from Geoscience Australia's Bushfire
Attack Level Toolbox ``utilities/bal_database.py`` at commit
``18c6cff4b37544805e78cf00ec376dbca2ff8cd0``. That software is Copyright
Commonwealth of Australia (Geoscience Australia) and licensed under Apache-2.0.

The GA implementation covers FDI 100, 80, 50 and 40. Keeping those four tables
removes the former 80->100 and 40->50 substitutions and gives every Australian
jurisdiction its published computational branch. This remains a preliminary
screening implementation based on the 2009 Method 1 model. It is not a current
AS 3959 conformity statement or a certified BAL assessment.

Each row contains the ascending distance limits for BAL-FZ, BAL-40, BAL-29 and
BAL-19. A classified-vegetation point inside the 100 m assessment window but
beyond the fourth limit is BAL-12.5; vegetation at or beyond 100 m is BAL-LOW.
For FDI other than 50, GA excludes grassland at 50 m or more. That rule is
applied by ``prescreen.py`` because it depends on distance as well as the table.
"""

GA_BAL_TOOLBOX_COMMIT = "18c6cff4b37544805e78cf00ec376dbca2ff8cd0"
GA_BAL_TOOLBOX_URL = (
    "https://github.com/GeoscienceAustralia/BAL/blob/"
    f"{GA_BAL_TOOLBOX_COMMIT}/utilities/bal_database.py"
)

BAL_LABELS = ["BAL-FZ", "BAL-40", "BAL-29", "BAL-19", "BAL-12.5"]
BAL_LOW = "BAL-LOW"

# Vegetation keys follow the GA source: A Forest, B Woodland, C Shrubland,
# D Scrub, E Mallee/Mulga, F Rainforest, G Grassland/Tussock moorland.
# fmt: off
_DISTANCE_LIMITS = {
    100: {
        "flat": {
            "A": (19, 25, 35, 48), "B": (12, 16, 24, 33),
            "C": (7, 9, 13, 19), "D": (10, 13, 19, 27),
            "E": (6, 8, 12, 17), "F": (8, 11, 16, 23),
            "G": (6, 9, 13, 19),
        },
        "d5": {
            "A": (24, 32, 43, 57), "B": (15, 21, 29, 41),
            "C": (7, 10, 15, 22), "D": (11, 15, 22, 31),
            "E": (7, 9, 13, 20), "F": (10, 14, 20, 29),
            "G": (7, 10, 15, 22),
        },
        "d10": {
            "A": (31, 39, 53, 69), "B": (20, 26, 37, 50),
            "C": (8, 11, 17, 25), "D": (12, 17, 24, 35),
            "E": (7, 10, 15, 23), "F": (13, 18, 26, 36),
            "G": (8, 11, 17, 25),
        },
        "d15": {
            "A": (39, 49, 64, 82), "B": (25, 33, 45, 60),
            "C": (9, 13, 19, 28), "D": (14, 19, 28, 39),
            "E": (8, 11, 18, 26), "F": (17, 23, 33, 45),
            "G": (9, 13, 20, 28),
        },
        "d20": {
            "A": (50, 61, 78, 98), "B": (32, 41, 56, 73),
            "C": (10, 15, 22, 31), "D": (15, 21, 31, 43),
            "E": (9, 13, 20, 29), "F": (22, 29, 42, 56),
            "G": (11, 15, 23, 32),
        },
    },
    80: {
        "flat": {
            "A": (16, 21, 31, 42), "B": (10, 14, 20, 29),
            "C": (7, 9, 13, 19), "D": (10, 13, 19, 27),
            "E": (6, 8, 12, 17), "F": (6, 9, 13, 19),
            "G": (6, 8, 12, 17),
        },
        "d5": {
            "A": (20, 27, 37, 50), "B": (13, 17, 25, 35),
            "C": (7, 10, 15, 22), "D": (11, 15, 22, 31),
            "E": (7, 9, 13, 20), "F": (8, 11, 17, 24),
            "G": (7, 9, 14, 20),
        },
        "d10": {
            "A": (26, 33, 46, 61), "B": (16, 22, 31, 43),
            "C": (8, 11, 17, 25), "D": (12, 17, 24, 35),
            "E": (7, 10, 15, 23), "F": (11, 15, 22, 31),
            "G": (8, 10, 16, 23),
        },
        "d15": {
            "A": (33, 42, 56, 73), "B": (21, 28, 39, 53),
            "C": (9, 13, 19, 28), "D": (14, 19, 28, 39),
            "E": (8, 11, 18, 26), "F": (14, 19, 28, 39),
            "G": (9, 12, 18, 26),
        },
        "d20": {
            "A": (42, 52, 68, 87), "B": (27, 35, 48, 64),
            "C": (10, 15, 22, 31), "D": (15, 21, 31, 43),
            "E": (9, 13, 20, 29), "F": (18, 25, 36, 48),
            "G": (10, 14, 21, 30),
        },
    },
    50: {
        "flat": {
            "A": (12, 16, 23, 32), "B": (7, 10, 15, 22),
            "C": (7, 9, 13, 19), "D": (10, 13, 19, 27),
            "E": (6, 8, 12, 17), "F": (5, 6, 9, 14),
            "G": (7, 9, 14, 20),
        },
        "d5": {
            "A": (14, 19, 27, 38), "B": (9, 12, 18, 26),
            "C": (7, 10, 15, 22), "D": (11, 15, 22, 31),
            "E": (7, 9, 13, 20), "F": (6, 8, 12, 17),
            "G": (8, 10, 16, 23),
        },
        "d10": {
            "A": (18, 24, 34, 46), "B": (11, 15, 23, 32),
            "C": (8, 11, 17, 25), "D": (12, 17, 24, 35),
            "E": (7, 10, 15, 23), "F": (7, 10, 15, 22),
            "G": (9, 12, 18, 26),
        },
        "d15": {
            "A": (22, 30, 41, 56), "B": (14, 19, 28, 40),
            "C": (9, 13, 19, 28), "D": (14, 19, 28, 39),
            "E": (8, 11, 18, 26), "F": (9, 13, 19, 28),
            "G": (10, 13, 20, 29),
        },
        "d20": {
            "A": (28, 37, 51, 67), "B": (18, 25, 36, 48),
            "C": (10, 15, 22, 31), "D": (15, 21, 31, 43),
            "E": (9, 13, 20, 29), "F": (12, 17, 25, 35),
            "G": (11, 15, 23, 33),
        },
    },
    40: {
        "flat": {
            "A": (10, 13, 20, 28), "B": (6, 9, 13, 19),
            "C": (7, 9, 13, 19), "D": (10, 13, 19, 27),
            "E": (6, 8, 12, 17), "F": (4, 5, 8, 12),
            "G": (4, 5, 8, 12),
        },
        "d5": {
            "A": (12, 16, 24, 34), "B": (8, 11, 16, 23),
            "C": (7, 10, 15, 22), "D": (11, 15, 22, 31),
            "E": (7, 9, 13, 20), "F": (5, 7, 10, 15),
            "G": (4, 6, 9, 14),
        },
        "d10": {
            "A": (15, 20, 29, 41), "B": (9, 13, 19, 28),
            "C": (8, 11, 17, 25), "D": (12, 17, 24, 35),
            "E": (7, 10, 15, 23), "F": (6, 8, 13, 19),
            "G": (5, 7, 11, 16),
        },
        "d15": {
            "A": (19, 25, 36, 49), "B": (12, 16, 24, 35),
            "C": (9, 13, 19, 28), "D": (14, 19, 28, 39),
            "E": (8, 11, 18, 26), "F": (8, 11, 16, 24),
            "G": (6, 8, 13, 19),
        },
        "d20": {
            "A": (24, 31, 44, 59), "B": (15, 21, 31, 42),
            "C": (10, 15, 22, 31), "D": (15, 21, 31, 43),
            "E": (9, 13, 20, 29), "F": (10, 14, 21, 30),
            "G": (7, 9, 15, 22),
        },
    },
}
# fmt: on

TABLES = {
    fdi: {
        slope: {veg: (*limits, 100) for veg, limits in rows.items()}
        for slope, rows in slope_tables.items()
    }
    for fdi, slope_tables in _DISTANCE_LIMITS.items()
}

FDI_BY_STATE = {
    "VIC": (100, "AS 3959-2009 Table 2.1 Victoria (general)"),
    "NSW": (100, "AS 3959-2009 Table 2.1 NSW, conservative coastal region; "
                   "general/inland NSW is FDI 80 and requires region identity"),
    "ACT": (100, "AS 3959-2009 Table 2.1 ACT"),
    "SA":  (80, "AS 3959-2009 Table 2.1 South Australia"),
    "WA":  (80, "AS 3959-2009 Table 2.1 Western Australia"),
    "TAS": (50, "AS 3959-2009 Table 2.1 Tasmania"),
    "QLD": (40, "AS 3959-2009 Table 2.1 Queensland"),
    "NT":  (40, "AS 3959-2009 Table 2.1 Northern Territory"),
}

ALPINE_ELEV_M = 1200


def resolve_fdi(state: str, elevation_m: float | None) -> tuple[int, str]:
    """Return the screening FDI and its explicit jurisdiction assumption."""
    fdi, basis = FDI_BY_STATE.get(
        state, (100, "unknown state, preliminary-screen default FDI 100"))
    if elevation_m is not None and elevation_m >= ALPINE_ELEV_M and state in ("VIC", "NSW"):
        return 50, (f"AS 3959-2009 Table 2.1 {state} alpine screening assumption "
                    f"(elevation {round(elevation_m)} m >= {ALPINE_ELEV_M} m)")
    return fdi, basis


def table_for_fdi(fdi: int) -> tuple[dict, int, bool]:
    """Return (table, FDI used, substituted); known Australian FDIs are exact."""
    if fdi in TABLES:
        return TABLES[fdi], fdi, False
    if fdi > 80:
        return TABLES[100], 100, True
    if fdi > 50:
        return TABLES[80], 80, True
    if fdi > 40:
        return TABLES[50], 50, True
    return TABLES[40], 40, True


def lookup_bal(fdi_table: dict, slope_band: str, veg_class: str,
               distance_m: float) -> str:
    """Map one classified vegetation observation to a preliminary BAL band."""
    row = fdi_table.get(slope_band, {}).get(veg_class)
    if row is None or distance_m >= 100:
        return BAL_LOW
    for i, boundary in enumerate(row):
        if distance_m < boundary:
            return BAL_LABELS[i]
    return BAL_LOW
