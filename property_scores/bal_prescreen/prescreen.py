"""Indicative BAL pre-screen — AS 3959 Method 1 over open data.

Pipeline (all inputs automated from a coordinate):
  1. State + FDI region        -> tables.resolve_fdi  (AS 3959 Table 2.1)
  2. Classify vegetation       -> ESA WorldCover 10m nearest classified patch
  3. Distance to vegetation    -> geodesic distance to nearest >=1 ha woody patch
  4. Effective slope + sign    -> local DEM/5m-LiDAR slope + site-vs-veg elevation
  5. Look up indicative BAL    -> tables.lookup_bal, worst across veg types
  6. Confidence band           -> vary formation (Forest<->Woodland) and slope
  7. Official overlay check    -> reuse bushfire overlay as an independent cross-check

This is a PRE-SCREEN. A compliant BAL requires a site assessment by an
accredited assessor (measured distances/slopes and photographs per quadrant).
The value here is automating the three inputs a free calculator makes you
hand-enter, and being honest about the uncertainty that automation carries.
"""

import argparse
import json
import logging
import math

from property_scores.bal_prescreen import tables

logger = logging.getLogger(__name__)

# ESA WorldCover class -> (AS 3959 class letter, worst/best formation pair, label).
# WorldCover 10m cannot resolve canopy-cover %, so a "tree" pixel could be Forest
# (A, heaviest), Woodland (B) or Rainforest (F). We take the CONSERVATIVE class as
# the point estimate and expose the lighter plausible class for the low end of the
# confidence band. Grassland (G) is only assessed under FDI 50 (AS 3959 footnote:
# grassland is not considered in the BAL except in Tasmania/FDI-50 jurisdictions).
WC_TREE, WC_SHRUB, WC_GRASS = 10, 20, 30
_CLASSIFIED_WC = (WC_TREE, WC_SHRUB, WC_GRASS)

# point class (conservative), lighter class (band low end), display label
VEG_MAP = {
    WC_TREE:  ("A", "B", "Forest (tree cover)"),
    WC_SHRUB: ("C", "D", "Shrubland"),
    WC_GRASS: ("G", "G", "Grassland"),
}

MAX_VEG_M = 100          # AS 3959 cut-off: vegetation beyond 100 m -> BAL-LOW
SEARCH_RADIUS_M = 150    # window to scan for nearest classified vegetation
MIN_PATCH_PIXELS = 100   # ~1 ha of 10 m pixels: AS 3959 excludes <1 ha patches
SLOPE_BANDS = [(5, "d5"), (10, "d10"), (15, "d15"), (20, "d20")]

# BAL ordering for "take the worst" and for banding.
_BAL_ORDER = ["BAL-LOW", "BAL-12.5", "BAL-19", "BAL-29", "BAL-40", "BAL-FZ"]


def _bal_rank(label: str) -> int:
    return _BAL_ORDER.index(label) if label in _BAL_ORDER else 0


def _haversine_m(lat1, lng1, lat2, lng2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _slope_band(slope_deg: float) -> str:
    """Map a downslope magnitude to an AS 3959 band key. >20 deg handled by caller."""
    for ceil, key in SLOPE_BANDS:
        if slope_deg <= ceil:
            return key
    return "d20"


def _nearest_vegetation(lat: float, lng: float) -> dict | None:
    """Nearest classified-vegetation patch to the point, via ESA WorldCover 10m.

    Returns {distance_m, wc_class, in_vegetation, patch_pixels, veg_lat, veg_lng}
    for the nearest woody/shrub (or grass) pixel that belongs to a >=1 ha patch,
    or None if WorldCover is unavailable. distance_m is None when no qualifying
    classified vegetation exists within the search window.
    """
    from property_scores.bushfire.score import landcover_grid

    grid = landcover_grid(lat, lng, radius_m=SEARCH_RADIUS_M)
    if not grid:
        return None

    classes = grid["classes"]
    nrows, ncols = grid["nrows"], grid["ncols"]
    west, south, east, north = grid["bbox"]
    # pixel centre coordinates
    dlat = (north - south) / nrows
    dlng = (east - west) / ncols

    # count classified pixels per class for the >=1 ha patch test (window-wide
    # proxy for contiguity — honest approximation, flagged in output)
    counts = {WC_TREE: 0, WC_SHRUB: 0, WC_GRASS: 0}
    for r in range(nrows):
        row = classes[r]
        for c in range(ncols):
            v = row[c]
            if v in counts:
                counts[v] += 1

    nearest = None  # (distance, wc_class, plat, plng)
    center_r, center_c = nrows // 2, ncols // 2
    for r in range(nrows):
        plat = north - (r + 0.5) * dlat
        row = classes[r]
        for c in range(ncols):
            v = row[c]
            if v not in _CLASSIFIED_WC:
                continue
            if counts[v] < MIN_PATCH_PIXELS:
                continue  # patch smaller than ~1 ha -> excluded per AS 3959
            plng = west + (c + 0.5) * dlng
            d = _haversine_m(lat, lng, plat, plng)
            if d > MAX_VEG_M:
                continue
            if nearest is None or d < nearest[0]:
                nearest = (d, v, plat, plng)

    in_veg = classes[center_r][center_c] in _CLASSIFIED_WC
    if nearest is None:
        return {"distance_m": None, "wc_class": None, "in_vegetation": in_veg,
                "patch_pixels": counts, "veg_lat": None, "veg_lng": None}
    d, v, plat, plng = nearest
    # if the site itself sits in a qualifying patch, effective distance is ~0
    if in_veg and classes[center_r][center_c] == v:
        d = 0.0
    return {"distance_m": round(d, 1), "wc_class": v, "in_vegetation": in_veg,
            "patch_pixels": counts, "veg_lat": plat, "veg_lng": plng}


def _effective_slope(lat, lng, veg_lat, veg_lng) -> dict:
    """Effective slope magnitude + direction (upslope/flat vs downslope).

    Downslope (site above the vegetation, land falling away toward the veg) is the
    more dangerous case in AS 3959. We approximate direction from the elevation
    difference between the site and the nearest-vegetation pixel, and magnitude
    from the local terrain slope. Returns {band, deg, direction, basis, measured}.
    """
    from property_scores.bushfire.score import _terrain_slope
    from property_scores.common import terrain

    slope = _terrain_slope(lat, lng)
    if slope is None:
        return {"band": "flat", "deg": None, "direction": "unknown",
                "basis": "slope not measurable (outside DEM coverage) — "
                         "assumed flat (0 deg)", "measured": False}
    mag = slope["mean_slope_deg"]

    direction = "flat/upslope"
    basis = f"terrain slope {mag} deg; direction not resolved -> flat/upslope (conservative-neutral)"
    if veg_lat is not None:
        e_site = terrain.elevation(lat, lng)
        e_veg = terrain.elevation(veg_lat, veg_lng)
        if e_site is not None and e_veg is not None:
            drop = e_site - e_veg
            if drop > 2:  # site meaningfully above the vegetation -> downslope
                direction = "downslope"
                basis = (f"site {round(e_site)} m is {round(drop)} m above nearest "
                         f"vegetation {round(e_veg)} m -> downslope; slope {mag} deg")
            else:
                direction = "flat/upslope"
                basis = (f"site {round(e_site)} m vs vegetation {round(e_veg)} m "
                         f"(drop {round(drop)} m) -> flat/upslope; slope {mag} deg")

    if direction == "downslope":
        if mag > 20:
            return {"band": ">20", "deg": mag, "direction": direction,
                    "basis": basis + " (>20 deg: Method 1 invalid -> BAL-FZ)",
                    "measured": True}
        return {"band": _slope_band(mag), "deg": mag, "direction": direction,
                "basis": basis, "measured": True}
    return {"band": "flat", "deg": mag, "direction": direction,
            "basis": basis, "measured": True}


def bal_prescreen(lat: float, lng: float) -> dict:
    """Indicative BAL pre-screen for an Australian coordinate (AS 3959 Method 1)."""
    from property_scores.bushfire.score import _detect_state, _overlay_check
    from property_scores.common import terrain

    state = _detect_state(lat, lng)
    if not state:
        return {"indicative_bal": None, "reason": "Outside Australia coverage",
                "lat": lat, "lng": lng}

    elev = terrain.elevation(lat, lng)
    fdi, fdi_basis = tables.resolve_fdi(state, elev)
    fdi_table, fdi_used, fdi_sub = tables.table_for_fdi(fdi)

    veg = _nearest_vegetation(lat, lng)
    # official state overlay (independent cross-check)
    worst_sev, hits, worst_cat, overlay_ok, overlay_basis = _overlay_check(state, lat, lng)
    overlay_clear = worst_sev is None and overlay_ok and overlay_basis is not None
    overlay_status = ("in_zone" if hits else "outside" if overlay_clear else "unavailable")

    assumptions = [
        "Indicative pre-screen only — NOT a certified BAL. A compliant assessment "
        "requires an accredited assessor measuring distance/slope and classifying "
        "vegetation on site per quadrant.",
        f"FDI: {fdi_basis}.",
    ]
    if fdi_sub:
        assumptions.append(
            f"FDI {fdi} is not tabulated in the public Method 1 tables; substituted "
            f"the nearest more-conservative table (FDI {fdi_used}) — widen confidence.")

    # --- No classified vegetation within 100 m -> BAL-LOW -------------------
    if veg is None:
        return {
            "indicative_bal": "BAL-LOW",
            "bal_range": ["BAL-LOW", "BAL-LOW"],
            "confidence": "low",
            "state": state, "fdi": fdi, "fdi_basis": fdi_basis,
            "inputs": {"vegetation": "ESA WorldCover unavailable at this location"},
            "official_overlay": {"status": overlay_status, "zones": hits},
            "method": "AS 3959-2009 Method 1 (indicative)",
            "assumptions": assumptions + ["WorldCover mosaic missing — vegetation "
                                          "input could not be automated here."],
            "disclaimer": _DISCLAIMER, "lat": lat, "lng": lng,
        }

    if veg["distance_m"] is None:
        # no >=1 ha classified vegetation within 100 m
        conf = "high" if overlay_clear else "moderate"
        note = ("No classified vegetation (>=1 ha, within 100 m) detected in "
                "WorldCover 10m.")
        if overlay_status == "in_zone":
            conf = "low"
            note += (" NOTE: official overlay flags this lot as bushfire-prone — "
                     "vegetation may be finer than 10 m resolution or just beyond "
                     "the window; a site check is warranted.")
        return {
            "indicative_bal": "BAL-LOW",
            "bal_range": ["BAL-LOW", "BAL-12.5" if overlay_status == "in_zone" else "BAL-LOW"],
            "confidence": conf,
            "state": state, "fdi": fdi, "fdi_basis": fdi_basis,
            "inputs": {"vegetation": note, "patch_pixels": veg["patch_pixels"]},
            "official_overlay": {"status": overlay_status, "zones": hits},
            "method": "AS 3959-2009 Method 1 (indicative)",
            "assumptions": assumptions,
            "disclaimer": _DISCLAIMER, "lat": lat, "lng": lng,
        }

    # --- Classified vegetation within 100 m --------------------------------
    dist = veg["distance_m"]
    wc = veg["wc_class"]
    point_class, light_class, veg_label = VEG_MAP[wc]

    slope = _effective_slope(lat, lng, veg["veg_lat"], veg["veg_lng"])

    # grassland only assessed under FDI 50 (AS 3959)
    grass_ignored = wc == WC_GRASS and fdi_used == 100
    if grass_ignored:
        assumptions.append(
            "Nearest vegetation is grassland; under FDI 100 grassland is not "
            "assessed for BAL (AS 3959) -> BAL-LOW on the grassland input.")

    def _bal_for(band, veg_class):
        if band == ">20":
            return "BAL-FZ"
        return tables.lookup_bal(fdi_table, band, veg_class, dist)

    if grass_ignored:
        point_bal = "BAL-LOW"
    else:
        point_bal = _bal_for(slope["band"], point_class)

    # Confidence band: vary formation (point vs lighter class) and slope
    # (measured band vs flat as the mild end; one-steeper as the harsh end).
    band_candidates = []
    if grass_ignored:
        band_candidates = ["BAL-LOW"]
    else:
        harsh_band = slope["band"]
        # a steeper downslope is the plausible-worse case for slope uncertainty
        steeper = {"flat": "d5", "d5": "d10", "d10": "d15", "d15": "d20", "d20": ">20"}
        harsh_band = steeper.get(slope["band"], slope["band"]) if slope["measured"] else slope["band"]
        band_candidates = [
            _bal_for("flat", light_class),      # mild: lighter formation, flat
            point_bal,                          # point estimate
            _bal_for(harsh_band, point_class),  # harsh: heavier formation, steeper
        ]
    lo = min(band_candidates, key=_bal_rank)
    hi = max(band_candidates, key=_bal_rank)

    # Confidence: driven by formation ambiguity + slope measurement + overlay agreement
    conf = "moderate"
    if not slope["measured"]:
        conf = "low"
    if _bal_rank(hi) - _bal_rank(lo) >= 3:
        conf = "low"
    # overlay disagreement lowers confidence
    if overlay_status == "outside" and _bal_rank(point_bal) >= _bal_rank("BAL-29"):
        conf = "low"
        assumptions.append("Official overlay says OUTSIDE mapped bushfire-prone land "
                           "yet WorldCover fuel is close — treat with caution.")

    result = {
        "indicative_bal": point_bal,
        "bal_range": [lo, hi],
        "confidence": conf,
        "state": state, "fdi": fdi, "fdi_basis": fdi_basis,
        "inputs": {
            "vegetation": {
                "esa_worldcover": veg_label,
                "as3959_class": f"{point_class} ({'Forest' if point_class=='A' else point_class})",
                "distance_m": dist,
                "in_vegetation": veg["in_vegetation"],
                "patch_pixels": veg["patch_pixels"],
                "formation_uncertainty": (
                    "WorldCover 10m cannot resolve canopy-cover %; assumed heaviest "
                    f"plausible class ({point_class}); lighter plausible = {light_class}. "
                    "This is the dominant confidence driver."),
            },
            "slope": slope,
            "distance_cutoff_m": MAX_VEG_M,
            "min_patch_ha": round(MIN_PATCH_PIXELS / 100, 1),
        },
        "official_overlay": {"status": overlay_status, "zones": hits,
                             "category": worst_cat, "basis": overlay_basis},
        "method": "AS 3959-2009 Method 1 (indicative, simplified procedure)",
        "assumptions": assumptions,
        "disclaimer": _DISCLAIMER,
        "lat": lat, "lng": lng,
    }
    return result


_DISCLAIMER = (
    "Indicative BAL pre-screen from open data (AS 3959 Method 1). This is NOT a "
    "certified Bushfire Attack Level assessment and must not be used for a building "
    "permit. A compliant BAL requires a site assessment by an accredited bushfire "
    "hazard assessor. Vegetation is classified from 10 m satellite land cover, which "
    "cannot resolve canopy density or fine on-ground detail; distances and slope are "
    "modelled, not surveyed.")


def _fmt(result: dict) -> str:
    if result.get("indicative_bal") is None:
        return f"{result.get('reason')}"
    lo, hi = result["bal_range"]
    lines = [
        f"Indicative BAL: {result['indicative_bal']}  (range {lo}..{hi}, "
        f"confidence: {result['confidence']})",
        f"State: {result['state']}   FDI: {result['fdi']} — {result['fdi_basis']}",
    ]
    veg = result["inputs"].get("vegetation")
    if isinstance(veg, dict):
        lines.append(f"Vegetation: {veg['esa_worldcover']} (AS3959 {veg['as3959_class']}), "
                     f"nearest {veg['distance_m']} m")
        lines.append(f"  {veg['formation_uncertainty']}")
    else:
        lines.append(f"Vegetation: {veg}")
    sl = result["inputs"].get("slope")
    if isinstance(sl, dict):
        lines.append(f"Slope: {sl.get('deg')} deg [{sl.get('direction')}] band={sl.get('band')}")
        lines.append(f"  {sl.get('basis')}")
    ov = result["official_overlay"]
    lines.append(f"Official overlay: {ov['status']}" + (f" {ov['zones']}" if ov.get("zones") else ""))
    return "\n".join(lines)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Indicative BAL pre-screen (AS 3959 Method 1)")
    p.add_argument("--lat", type=float, required=True)
    p.add_argument("--lng", type=float, required=True)
    p.add_argument("--json", action="store_true", help="emit full JSON")
    args = p.parse_args()
    res = bal_prescreen(args.lat, args.lng)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(_fmt(res))
