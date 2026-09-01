"""Indicative BAL pre-screen, AS 3959 Method 1 over open data.

Pipeline (all inputs automated from a coordinate):
  1. State + FDI region        -> tables.resolve_fdi  (AS 3959 Table 2.1)
  2. Classify vegetation       -> ESA WorldCover 10m qualifying patches, all classes
  3. Distance to vegetation    -> geodesic distance per class to its nearest >=1 ha patch
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
# (A, heaviest), Woodland (B) or Rainforest (F). We take the conservative class as
# the point estimate and expose the lighter plausible class for the low end of the
# confidence band. Grassland distance treatment follows the pinned GA implementation.
WC_TREE, WC_SHRUB, WC_GRASS = 10, 20, 30
_CLASSIFIED_WC = (WC_TREE, WC_SHRUB, WC_GRASS)

# point class (conservative), lighter class (band low end), display label
VEG_MAP = {
    WC_TREE:  ("A", "B", "Forest (tree cover)"),
    # In the pinned GA tables D (Scrub) has longer distance thresholds than
    # C (Shrubland), so D is the conservative point and C the lighter bound.
    WC_SHRUB: ("D", "C", "Shrub/scrub cover"),
    WC_GRASS: ("G", "G", "Grassland"),
}

MAX_VEG_M = 100          # AS 3959 cut-off: vegetation beyond 100 m -> BAL-LOW
SEARCH_RADIUS_M = 150    # window to scan for nearest classified vegetation
MIN_PATCH_AREA_M2 = 10_000  # one hectare
SLOPE_BANDS = [(5, "d5"), (10, "d10"), (15, "d15"), (20, "d20")]
_METHOD = "GA BAL Toolbox 2009 Method 1 adaptation (preliminary screen)"
_METHOD_SOURCE = {
    "name": "Geoscience Australia Bushfire Attack Level Toolbox",
    "licence": "Apache-2.0",
    "commit": tables.GA_BAL_TOOLBOX_COMMIT,
    "url": tables.GA_BAL_TOOLBOX_URL,
}

# BAL ordering for "take the worst" and for banding.
_BAL_ORDER = ["BAL-LOW", "BAL-12.5", "BAL-19", "BAL-29", "BAL-40", "BAL-FZ"]


def _bal_rank(label: str) -> int:
    return _BAL_ORDER.index(label) if label in _BAL_ORDER else 0


def _grassland_excluded(fdi_used: int, distance_m: float) -> bool:
    """Mirror the pinned GA rule: non-FDI-50 grass is excluded from 50 m."""
    return fdi_used != 50 and distance_m >= 50


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
    """Classified-vegetation patches around the point, via ESA WorldCover 10m.

    Returns {distance_m, wc_class, in_vegetation, patch_pixels, veg_lat, veg_lng,
    pixels} or None if WorldCover is unavailable. distance_m/wc_class describe
    the nearest qualifying vegetation pixel; pixels lists EVERY qualifying
    vegetation pixel within the 100 m window so the caller can evaluate each
    one (class, distance and slope direction) and keep the worst BAL, instead
    of letting the nearest pixel shadow a worse one elsewhere. distance_m is
    None when no qualifying classified vegetation exists within the window.

    Patch qualification (>=1 ha) uses the connected area of ADJACENT CLASSIFIED
    VEGETATION of any type: adjacent tree + shrub areas of 0.6 ha each form one
    1.2 ha fuel patch, per the intent of the AS 3959 minimum-area exclusion.
    Each pixel still carries its own class for the risk lookup. The area basis
    is the visible portion inside the analysis window; a patch truncated by the
    window edge is assessed on its visible connected pixels only.
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
    pixel_height_m = abs(dlat) * 111_320.0
    pixel_width_m = abs(dlng) * 111_320.0 * max(
        math.cos(math.radians(lat)), 0.1)
    pixel_area_m2 = pixel_height_m * pixel_width_m
    if pixel_area_m2 <= 0:
        return None
    min_patch_pixels = max(1, math.ceil(MIN_PATCH_AREA_M2 / pixel_area_m2))

    # Count all pixels for transparent diagnostics, but qualify vegetation by
    # its actual connected component. The former window-wide count treated 100
    # isolated tree pixels as one hectare of vegetation.
    counts = {WC_TREE: 0, WC_SHRUB: 0, WC_GRASS: 0}
    for r in range(nrows):
        row = classes[r]
        for c in range(ncols):
            v = row[c]
            if v in counts:
                counts[v] += 1

    # Connected components over ALL classified vegetation, not per class:
    # adjacent mixed-type vegetation (e.g. tree next to shrub) burns as one
    # fuel patch, so the >=1 ha qualification uses the combined area.
    component_size: dict[tuple[int, int], int] = {}
    visited: set[tuple[int, int]] = set()
    for start_r in range(nrows):
        for start_c in range(ncols):
            start = (start_r, start_c)
            if classes[start_r][start_c] not in _CLASSIFIED_WC or start in visited:
                continue
            stack = [start]
            visited.add(start)
            component = []
            while stack:
                r, c = stack.pop()
                component.append((r, c))
                for dr in (-1, 0, 1):
                    for dc in (-1, 0, 1):
                        if dr == 0 and dc == 0:
                            continue
                        nr, nc = r + dr, c + dc
                        neighbour = (nr, nc)
                        if not (0 <= nr < nrows and 0 <= nc < ncols):
                            continue
                        if neighbour in visited or classes[nr][nc] not in _CLASSIFIED_WC:
                            continue
                        visited.add(neighbour)
                        stack.append(neighbour)
            size = len(component)
            for cell in component:
                component_size[cell] = size

    nearest = None  # (distance, wc_class, plat, plng, patch_size)
    # every qualifying vegetation pixel within the 100 m assessment window
    pixels: list[dict] = []
    center_r, center_c = nrows // 2, ncols // 2
    center_qualifies = (
        classes[center_r][center_c] in _CLASSIFIED_WC
        and component_size.get((center_r, center_c), 0) >= min_patch_pixels)
    for r in range(nrows):
        plat = north - (r + 0.5) * dlat
        row = classes[r]
        for c in range(ncols):
            v = row[c]
            if v not in _CLASSIFIED_WC:
                continue
            patch_size = component_size.get((r, c), 0)
            if patch_size < min_patch_pixels:
                continue
            plng = west + (c + 0.5) * dlng
            # the site itself sitting in a qualifying patch is distance 0
            if r == center_r and c == center_c and center_qualifies:
                d = 0.0
            else:
                d = _haversine_m(lat, lng, plat, plng)
            if d > MAX_VEG_M:
                continue
            if nearest is None or d < nearest[0]:
                nearest = (d, v, plat, plng, patch_size)
            pixels.append({"wc_class": v, "distance_m": round(d, 1),
                           "veg_lat": plat, "veg_lng": plng})

    in_veg = classes[center_r][center_c] in _CLASSIFIED_WC
    if nearest is None:
        return {"distance_m": None, "wc_class": None, "in_vegetation": in_veg,
                "patch_pixels": counts, "min_patch_pixels": min_patch_pixels,
                "pixel_area_m2": round(pixel_area_m2, 1),
                "veg_lat": None, "veg_lng": None}
    d, v, plat, plng, patch_size = nearest
    return {"distance_m": round(d, 1), "wc_class": v, "in_vegetation": in_veg,
            "patch_pixels": counts, "nearest_patch_pixels": patch_size,
            "min_patch_pixels": min_patch_pixels,
            "pixel_area_m2": round(pixel_area_m2, 1),
            "veg_lat": plat, "veg_lng": plng,
            "pixels": pixels}


def _slope_magnitude(lat, lng, *, slope_deg=None) -> tuple[float | None, bool]:
    """Local terrain slope magnitude in degrees, and whether it was measured.

    slope_deg may be injected by a caller that has already measured the local
    terrain slope (e.g. bushfire_score), to avoid a redundant DEM read.
    """
    if slope_deg is not None:
        return slope_deg, True
    from property_scores.bushfire.score import _terrain_slope
    slope = _terrain_slope(lat, lng)
    if slope is None:
        return None, False
    return slope["mean_slope_deg"], True


def _pixel_slope(mag, measured, e_site, e_veg) -> dict:
    """Effective slope for ONE vegetation pixel: magnitude + direction.

    Downslope (site above the vegetation, land falling away toward the veg) is
    the more dangerous case in AS 3959. Direction comes from the elevation
    difference between the site and THIS pixel, so vegetation on opposite
    sides of the site gets its own direction instead of inheriting the nearest
    pixel's. Returns {band, deg, direction, basis, measured}.
    """
    if not measured:
        return {"band": "flat", "deg": None, "direction": "unknown",
                "basis": "slope not measurable (outside DEM coverage), "
                         "assumed flat (0 deg)", "measured": False}

    direction = "flat/upslope"
    basis = f"terrain slope {mag} deg; direction not resolved -> flat/upslope (conservative-neutral)"
    if e_site is not None and e_veg is not None:
        drop = e_site - e_veg
        if drop > 2:  # site meaningfully above the vegetation -> downslope
            direction = "downslope"
            basis = (f"site {round(e_site)} m is {round(drop)} m above the "
                     f"vegetation at {round(e_veg)} m -> downslope; slope {mag} deg")
        else:
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


def bal_prescreen(lat: float, lng: float, *, state=None, elevation=None,
                  slope_deg=None, overlay=None) -> dict:
    """Indicative BAL pre-screen for an Australian coordinate (AS 3959 Method 1).

    Optional injected inputs let a caller that has already computed them (e.g.
    bushfire_score) avoid redundant fetches:
      state      , AU state code (skips border detection)
      elevation  , metres (skips a DEM read; used for the alpine-FDI override)
      slope_deg  , local terrain slope magnitude (skips a DEM slope read)
      overlay    , the _overlay_check tuple
                    (worst_sev, hits, worst_cat, overlay_ok, overlay_basis)
    """
    from property_scores.bushfire.score import _detect_state, _overlay_check
    from property_scores.common import terrain

    if state is None:
        state = _detect_state(lat, lng)
    if not state:
        return {"indicative_bal": None, "reason": "Outside Australia coverage",
                "lat": lat, "lng": lng}

    elev = elevation if elevation is not None else terrain.elevation(lat, lng)
    fdi, fdi_basis = tables.resolve_fdi(state, elev)
    fdi_table, fdi_used, fdi_sub = tables.table_for_fdi(fdi)

    veg = _nearest_vegetation(lat, lng)
    # official state overlay (independent cross-check)
    if overlay is not None:
        worst_sev, hits, worst_cat, overlay_ok, overlay_basis = overlay
    else:
        worst_sev, hits, worst_cat, overlay_ok, overlay_basis = _overlay_check(state, lat, lng)
    overlay_clear = worst_sev is None and overlay_ok and overlay_basis is not None
    overlay_status = ("in_zone" if hits else "outside" if overlay_clear else "unavailable")

    assumptions = [
        "Indicative pre-screen only, NOT a certified BAL. A compliant assessment "
        "requires an accredited assessor measuring distance/slope and classifying "
        "vegetation on site per quadrant.",
        f"FDI: {fdi_basis}.",
    ]
    if fdi_sub:
        assumptions.append(
            f"FDI {fdi} is outside the four Australian branches in the pinned GA "
            f"implementation; substituted FDI {fdi_used}, widen confidence.")

    # --- No classified vegetation within 100 m -> BAL-LOW -------------------
    if veg is None:
        return {
            "indicative_bal": "BAL-LOW",
            "bal_range": ["BAL-LOW", "BAL-LOW"],
            "confidence": "low",
            "state": state, "fdi": fdi, "fdi_basis": fdi_basis,
            "inputs": {"vegetation": "ESA WorldCover unavailable at this location"},
            "official_overlay": {"status": overlay_status, "zones": hits},
            "method": _METHOD, "method_source": _METHOD_SOURCE,
            "assumptions": assumptions + ["WorldCover mosaic missing, vegetation "
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
            note += (" NOTE: official overlay flags this lot as bushfire-prone, "
                     "vegetation may be finer than 10 m resolution or just beyond "
                     "the window; a site check is warranted.")
        return {
            "indicative_bal": "BAL-LOW",
            "bal_range": ["BAL-LOW", "BAL-12.5" if overlay_status == "in_zone" else "BAL-LOW"],
            "confidence": conf,
            "state": state, "fdi": fdi, "fdi_basis": fdi_basis,
            "inputs": {"vegetation": note, "patch_pixels": veg["patch_pixels"]},
            "official_overlay": {"status": overlay_status, "zones": hits},
            "method": _METHOD, "method_source": _METHOD_SOURCE,
            "assumptions": assumptions,
            "disclaimer": _DISCLAIMER, "lat": lat, "lng": lng,
        }

    # --- Classified vegetation within 100 m --------------------------------
    # Worst case across EVERY qualifying vegetation pixel: class, distance and
    # slope direction are all evaluated per pixel and the severest outcome
    # wins. Reducing to the nearest pixel per class let a nearer upslope
    # forest shadow a farther downslope forest whose Method 1 band is worse,
    # just as reducing to the nearest patch let grass shadow forest.
    pixels = veg.get("pixels") or [
        {"wc_class": veg["wc_class"], "distance_m": veg["distance_m"],
         "veg_lat": veg["veg_lat"], "veg_lng": veg["veg_lng"]}]

    def _bal_for(band, veg_class, distance_m):
        if band == ">20":
            return "BAL-FZ"
        return tables.lookup_bal(fdi_table, band, veg_class, distance_m)

    # a steeper downslope is the plausible-worse case for slope uncertainty
    steeper = {"flat": "d5", "d5": "d10", "d10": "d15", "d15": "d20", "d20": ">20"}

    mag, measured = _slope_magnitude(lat, lng, slope_deg=slope_deg)
    e_site = None
    if measured and any(p["veg_lat"] is not None for p in pixels):
        e_site = terrain.elevation(lat, lng)

    def _cand_key(cand):
        # worst point BAL wins; break ties by the harsher band, then proximity
        return (_bal_rank(cand["point_bal"]),
                _bal_rank(max(cand["band_candidates"], key=_bal_rank)),
                -cand["dist"])

    worst = None      # severest candidate overall
    by_class = {}     # severest candidate per vegetation class (for reporting)
    hi = "BAL-LOW"    # harsh end of the confidence band across ALL candidates
    for pix in pixels:
        c_dist = pix["distance_m"]
        wc_cls = pix["wc_class"]
        c_point, c_light, c_label = VEG_MAP[wc_cls]
        e_veg = None
        if e_site is not None and pix["veg_lat"] is not None:
            e_veg = terrain.elevation(pix["veg_lat"], pix["veg_lng"])
        c_slope = _pixel_slope(mag, measured, e_site, e_veg)
        # GA's Method 1 implementation excludes grassland at >=50 m for every
        # FDI except 50. It still assesses closer grassland; the former
        # implementation incorrectly discarded all grassland in those
        # jurisdictions.
        c_grass_excluded = wc_cls == WC_GRASS and _grassland_excluded(fdi_used, c_dist)
        if c_grass_excluded:
            c_point_bal = "BAL-LOW"
            # Confidence band: vary formation (point vs lighter class) and
            # slope (flat as the mild end; one-steeper as the harsh end).
            c_band_candidates = ["BAL-LOW"]
        else:
            c_point_bal = _bal_for(c_slope["band"], c_point, c_dist)
            harsh_band = (steeper.get(c_slope["band"], c_slope["band"])
                          if c_slope["measured"] else c_slope["band"])
            c_band_candidates = [
                _bal_for("flat", c_light, c_dist),        # mild: lighter formation, flat
                c_point_bal,                              # point estimate
                _bal_for(harsh_band, c_point, c_dist),    # harsh: heavier formation, steeper
            ]
        cand = {
            "wc": wc_cls, "dist": c_dist, "point_class": c_point,
            "light_class": c_light, "label": c_label, "slope": c_slope,
            "grass_excluded": c_grass_excluded, "point_bal": c_point_bal,
            "band_candidates": c_band_candidates,
        }
        if worst is None or _cand_key(cand) > _cand_key(worst):
            worst = cand
        prev = by_class.get(wc_cls)
        if prev is None or _cand_key(cand) > _cand_key(prev):
            by_class[wc_cls] = cand
        c_hi = max(c_band_candidates, key=_bal_rank)
        if _bal_rank(c_hi) > _bal_rank(hi):
            hi = c_hi

    dist = worst["dist"]
    point_class, light_class, veg_label = (
        worst["point_class"], worst["light_class"], worst["label"])
    slope = worst["slope"]
    point_bal = worst["point_bal"]
    lo = min(worst["band_candidates"], key=_bal_rank)

    class_summaries = sorted(by_class.values(), key=lambda c: c["dist"])
    for cand in class_summaries:
        if cand["grass_excluded"]:
            assumptions.append(
                f"Grassland at {cand['dist']} m; the GA Method 1 implementation "
                f"excludes grassland from 50 m for FDI {fdi_used}.")
    if len(class_summaries) > 1:
        assumptions.append(
            "Multiple vegetation classes qualify within 100 m; the screen keeps "
            "the worst BAL across all of them ("
            + ", ".join(f"{c['point_class']} at {c['dist']} m -> {c['point_bal']}"
                        for c in class_summaries)
            + ").")

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
                           "yet WorldCover fuel is close, treat with caution.")

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
                "nearest_patch_pixels": veg.get("nearest_patch_pixels"),
                "assessed_classes": [
                    {"as3959_class": c["point_class"], "label": c["label"],
                     "distance_m": c["dist"], "bal": c["point_bal"]}
                    for c in class_summaries],
                "min_patch_pixels": veg.get("min_patch_pixels"),
                "pixel_area_m2": veg.get("pixel_area_m2"),
                "formation_uncertainty": (
                    "WorldCover 10m cannot resolve canopy-cover %; assumed heaviest "
                    f"plausible class ({point_class}); lighter plausible = {light_class}. "
                    "This is the dominant confidence driver."),
            },
            "slope": slope,
            "distance_cutoff_m": MAX_VEG_M,
            "min_patch_ha": 1.0,
        },
        "official_overlay": {"status": overlay_status, "zones": hits,
                             "category": worst_cat, "basis": overlay_basis},
        "method": _METHOD,
        "method_source": _METHOD_SOURCE,
        "assumptions": assumptions,
        "disclaimer": _DISCLAIMER,
        "lat": lat, "lng": lng,
    }
    return result


_DISCLAIMER = (
    "Preliminary BAL screen from open data using a pinned Geoscience Australia "
    "2009 Method 1 implementation. This is NOT a "
    "certified Bushfire Attack Level assessment and must not be used for a building "
    "permit or treated as current AS 3959 conformity. A compliant BAL requires a "
    "site assessment by an accredited bushfire "
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
        f"State: {result['state']}   FDI: {result['fdi']}, {result['fdi_basis']}",
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
