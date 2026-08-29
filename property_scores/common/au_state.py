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

# ACT is not a rectangle: Queanbeyan cuts into its eastern edge.  The old
# 148.76..149.40 bbox routed Queanbeyan NSW into ACT.  This is the mainland ACT
# exterior derived from the ABS ASGS 2021 SAL polygons (GDA2020, CC BY 4.0),
# simplified to ~500 m while retaining 99.7% of area.  It is only a routing
# boundary; no ABS fields or geometry are returned to customers.
_ACT_BOUNDARY = [
    (149.090715, -35.765604), (149.101510, -35.803640),
    (149.093577, -35.824099), (149.095693, -35.845624),
    (149.064289, -35.874934), (149.049550, -35.919946),
    (149.012400, -35.899611), (148.999423, -35.902664),
    (148.997668, -35.896414), (148.975662, -35.892163),
    (148.961605, -35.896708), (148.932902, -35.873576),
    (148.909451, -35.853061), (148.907069, -35.829566),
    (148.886684, -35.810051), (148.897783, -35.794646),
    (148.894868, -35.771865), (148.903325, -35.757924),
    (148.894104, -35.751326), (148.886711, -35.719226),
    (148.877720, -35.714949), (148.872491, -35.721415),
    (148.857529, -35.761445), (148.822500, -35.720958),
    (148.796288, -35.709339), (148.788448, -35.697794),
    (148.798454, -35.666583), (148.767504, -35.647400),
    (148.783448, -35.628497), (148.768439, -35.603293),
    (148.788779, -35.588327), (148.772945, -35.567785),
    (148.777954, -35.558507), (148.762796, -35.495486),
    (148.774387, -35.486132), (148.766747, -35.467168),
    (148.775991, -35.454874), (148.774846, -35.441822),
    (148.788922, -35.426482), (148.785800, -35.408898),
    (148.795982, -35.406624), (148.795693, -35.392990),
    (148.808665, -35.382442), (148.793552, -35.339109),
    (148.810139, -35.307437), (149.120953, -35.124403),
    (149.138992, -35.127970), (149.149618, -35.138393),
    (149.146732, -35.144540), (149.164284, -35.141892),
    (149.167584, -35.159739), (149.185612, -35.161109),
    (149.183646, -35.175656), (149.197041, -35.185355),
    (149.189548, -35.203311), (149.208523, -35.211499),
    (149.204992, -35.229087), (149.213790, -35.219593),
    (149.238703, -35.222215), (149.246799, -35.229137),
    (149.234379, -35.242779), (149.273184, -35.259166),
    (149.271813, -35.273472), (149.315306, -35.276218),
    (149.322482, -35.286727), (149.341504, -35.286690),
    (149.361671, -35.309012), (149.394791, -35.303176),
    (149.399293, -35.319077), (149.355571, -35.350701),
    (149.336419, -35.339714), (149.254750, -35.330108),
    (149.207167, -35.345286), (149.146693, -35.414833),
    (149.139075, -35.432520), (149.155022, -35.436663),
    (149.135078, -35.454825), (149.151261, -35.507189),
    (149.131459, -35.553590), (149.142687, -35.592776),
    (149.084620, -35.580802), (149.078153, -35.586404),
    (149.087568, -35.639640), (149.097485, -35.647282),
    (149.095304, -35.679267), (149.109423, -35.696573),
    (149.099355, -35.714646), (149.104104, -35.724969),
    (149.090715, -35.765604),
]


def _point_in_polygon(lng: float, lat: float,
                      polygon: list[tuple[float, float]]) -> bool:
    """Dependency-free ray casting for the small ACT routing polygon."""
    inside = False
    j = len(polygon) - 1
    for i, (xi, yi) in enumerate(polygon):
        xj, yj = polygon[j]
        crosses = ((yi > lat) != (yj > lat))
        if crosses:
            edge_x = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lng < edge_x:
                inside = not inside
        j = i
    return inside

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

    # --- ACT: enclave inside NSW; must be tested before NSW/VIC.  Its eastern
    # edge wraps around Queanbeyan, so a rectangle is not a valid boundary. ---
    if (148.76 <= lng <= 149.40 and -35.93 <= lat <= -35.12
            and _point_in_polygon(lng, lat, _ACT_BOUNDARY)):
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
