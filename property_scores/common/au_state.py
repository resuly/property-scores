"""Australian state detection and ArcGIS REST query helpers.

`detect_state(lat, lng)` resolves the state/territory of a coordinate. There is no
local admin/boundary polygon (Overture admin divisions were not downloaded; only
buildings/pois/roads/water exist), so we encode the actual interstate borders.

This matters because almost every state pair is divided by a meridian or parallel,
and a naive overlapping-bbox classifier with first-match-wins misroutes a large
strip of one state into its neighbour. The previous version sent all of southern
inland NSW (Wagga, Albury, Yass, Deniliquin) into VIC, the QLD side of the 29S
border (Coolangatta, Goondiwindi) into NSW, and SA points just west of 141E into VIC.

Border geometry encoded here (all the straight ones are exact; the two river borders
are piecewise polylines traced from Overture water `Murray River` /
`Macintyre`-`Dumaresq`-`Barwon River` vertices):

  WA | SA, WA | NT, SA | NT (west)   meridian 129 E
  SA | NT                            parallel  26 S  (north of 26 = NT)
  NT | QLD                           meridian 138 E
  SA | NSW, SA | VIC                 meridian 141 E
  QLD | NSW (west of ~149 E)         parallel  29 S
  QLD | NSW (east of ~149 E)         Macintyre/Dumaresq river polyline
  NSW | VIC                          Murray River polyline, then a near-meridian
                                     segment (~148 E) down to the coast

A point is assigned by testing these borders in an order that resolves every
overlap; interior points fall through to a coarse bbox only as a final safety net.
"""

import requests

# --- river-border polylines: list of (lng, border_lat); state membership decided
#     by whether the point's lat is north (larger, less negative) or south of the
#     interpolated border_lat at that lng. Traced from Overture water vertices. ---

# NSW | VIC. NSW is NORTH of the Murray; VIC is SOUTH. Traced from the Overture
# `Murray River` southern-bank vertices (min-lat per 0.1 deg lng bin). The river
# runs from the SA corner (~141 E, -34.06) curving south to ~-36.13 near Tocumwal
# (144.8 E), back up to ~-35.83 (145.1 E), then down past Albury (147 E, -36.11).
# Near the source (~147.9 E) the border LEAVES the river and runs as a roughly
# straight line south-east to Cape Howe on the coast (149.97 E, -37.50).
_NSW_VIC_MURRAY = [
    (140.97, -34.06),
    (141.20, -34.09),
    (141.40, -34.17),
    (141.60, -34.20),
    (141.80, -34.13),
    (142.00, -34.12),
    (142.20, -34.20),   # main channel at Mildura; ignore the deep horseshoe bend
    (142.40, -34.45),
    (142.60, -34.79),
    (142.80, -34.63),
    (143.00, -34.70),
    (143.20, -34.76),
    (143.40, -35.20),
    (143.60, -35.40),
    (143.80, -35.46),
    (144.00, -35.56),
    (144.20, -35.73),
    (144.40, -35.94),
    (144.60, -36.08),
    (144.80, -36.13),
    (145.00, -36.08),
    (145.10, -35.84),
    (145.40, -35.87),
    (145.60, -35.90),
    (145.80, -35.99),
    (146.00, -36.01),
    (146.20, -36.05),
    (146.40, -36.05),
    (146.60, -36.01),
    (146.80, -36.09),
    (147.00, -36.11),
    (147.20, -36.05),
    (147.60, -35.96),   # main channel near Jingellic/Walwa; south-bank towns -> VIC
    (147.80, -35.94),
    (147.90, -36.00),
    (148.00, -36.40),   # border leaves the river near Tom Groggin, heads SE
    (148.20, -36.72),
    (148.50, -36.95),
    (149.00, -37.25),
    (149.50, -37.42),
    (149.97, -37.50),   # Cape Howe on the coast
]

# QLD | NSW east of ~149 E. QLD is NORTH of the river line; NSW is SOUTH.
# West of 149 E this border is the 29 S parallel (handled separately).
_QLD_NSW_RIVERS = [
    (148.95, -29.00),
    (149.25, -28.76),
    (149.75, -28.62),
    (150.25, -28.56),
    (150.75, -28.66),
    (151.00, -28.95),
    (151.25, -29.10),
    (151.50, -29.20),
    (152.00, -28.70),
    (152.50, -28.40),
    (153.00, -28.25),
    (153.55, -28.17),   # Point Danger / Coolangatta on the coast
]


def _interp_lat(polyline: list[tuple[float, float]], lng: float) -> float:
    """Linearly interpolate the border latitude at a given longitude."""
    if lng <= polyline[0][0]:
        return polyline[0][1]
    if lng >= polyline[-1][0]:
        return polyline[-1][1]
    for (x0, y0), (x1, y1) in zip(polyline, polyline[1:]):
        if x0 <= lng <= x1:
            t = (lng - x0) / (x1 - x0) if x1 != x0 else 0.0
            return y0 + t * (y1 - y0)
    return polyline[-1][1]


# Coarse fallback boxes (only used if no border rule fires — e.g. WA/NT/QLD north,
# TAS, ACT, or an offshore point). Ordered smallest-enclosed-first.
_STATE_BOXES = [
    ("ACT", 148.76, -35.93, 149.40, -35.12),
    ("TAS", 143.50, -43.70, 148.55, -39.40),
    ("VIC", 140.96, -39.20, 150.05, -33.95),
    ("NSW", 140.99, -37.60, 153.70, -28.10),
    ("QLD", 137.90, -29.20, 153.60, -9.90),
    ("SA",  129.00, -38.10, 141.00, -25.95),
    ("NT",  129.00, -26.00, 138.05, -10.90),
    ("WA",  112.90, -35.20, 129.00, -13.50),
]


def detect_state(lat: float, lng: float) -> str | None:
    """Resolve the AU state/territory of a coordinate using real border geometry.

    Returns one of NSW/VIC/QLD/SA/WA/NT/TAS/ACT, or None if outside the mainland
    + Tasmania bounds. Points exactly on a border resolve deterministically to one
    side (the comparisons use strict/relaxed inequalities chosen to keep towns on
    their correct side).
    """
    # Reject clearly out-of-range coordinates.
    if not (-44.0 <= lat <= -9.0 and 112.0 <= lng <= 154.5):
        return None

    # --- Tasmania: isolated island, no shared land border. ---
    if 143.50 <= lng <= 148.55 and -43.70 <= lat <= -39.40:
        return "TAS"

    # --- ACT: enclave inside NSW; must be tested before the NSW/VIC logic. ---
    if 148.76 <= lng <= 149.40 and -35.93 <= lat <= -35.12:
        return "ACT"

    # --- Western third: meridian 129 E splits WA from SA/NT. ---
    if lng < 129.0:
        # West of 129 E is WA (its only land borders are the 129 E meridian).
        return "WA"

    # --- The 129 E .. 141 E column is SA (south of 26 S) or NT (north of 26 S). ---
    if 129.0 <= lng < 138.0:
        if lat >= -26.0:          # north of 26 S
            return "NT"
        # south of 26 S: SA, unless east enough to be QLD's far SW (QLD west edge
        # is 138 E, so nothing here is QLD). It's SA all the way to 141 E.
        return "SA"

    # --- 138 E .. 141 E: NT ends at 138 E. This column is QLD (north of 26 S) or
    #     SA (south of 26 S). SA's whole eastern edge is 141 E; QLD's SW corner is
    #     Poeppel Corner at 138 E / 26 S. The 29 S QLD/NSW line lives EAST of 141 E
    #     (Cameron Corner), so it must NOT be applied in this column. ---
    if 138.0 <= lng < 141.0:
        return "QLD" if lat > -26.0 else "SA"

    # --- East of 141 E: QLD / NSW / VIC. SA does not reach east of 141 E. ---
    # QLD vs NSW.
    if lng < 148.95:
        # West of the river system: plain 29 S parallel.
        if lat > -29.0:
            return "QLD"
    else:
        # East of ~149 E: Macintyre/Dumaresq river polyline.
        if lat > _interp_lat(_QLD_NSW_RIVERS, lng):
            return "QLD"

    # --- Remaining: NSW vs VIC, divided by the Murray (then near-meridian). ---
    border = _interp_lat(_NSW_VIC_MURRAY, lng)
    if lat >= border:             # north of the Murray -> NSW
        return "NSW"
    # South of the Murray. Could be VIC, or (for lng east of ~148 E and far south)
    # still VIC. East Gippsland/Cape Howe corner is VIC up to ~149.97 E.
    if lng <= 150.05 and lat >= -39.20:
        return "VIC"

    # --- Final safety net: coarse bbox (offshore / edge cases). ---
    for state, x1, y1, x2, y2 in _STATE_BOXES:
        if x1 <= lng <= x2 and y1 <= lat <= y2:
            return state
    return None


def arcgis_point_query(endpoint: str, lat: float, lng: float,
                       *, out_fields: str = "*", timeout: int = 10) -> dict | None:
    """Query an ArcGIS MapServer/FeatureServer layer for features intersecting a point."""
    params = {
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "outSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": out_fields,
        "returnCountOnly": "false",
        "f": "json",
    }
    try:
        resp = requests.get(f"{endpoint}/query", params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            return None
        return data
    except (requests.RequestException, ValueError):
        return None


def arcgis_point_count(endpoint: str, lat: float, lng: float,
                       *, timeout: int = 8) -> int:
    """Fast count-only query — returns number of features at a point."""
    params = {
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "returnCountOnly": "true",
        "f": "json",
    }
    try:
        resp = requests.get(f"{endpoint}/query", params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        return data.get("count", 0)
    except (requests.RequestException, ValueError):
        return -1
