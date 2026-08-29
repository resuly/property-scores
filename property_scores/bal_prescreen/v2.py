"""Internal shadow candidate for a safer preliminary BAL screen.

This module is deliberately not wired to the public score API. It exists to
measure whether the engineering gaps in v1 can be closed before any decision
to expose a BAL beta. It uses the pinned GA 2009 Method 1 computation data,
not a current-AS-3959 conformity claim.

Differences from v1:
* requires a verified building point;
* treats adjacent mixed combustible land-cover classes as one patch for the
  one-hectare qualification test;
* assesses every qualifying vegetation class/component around the building;
* estimates effective slope from a profile wholly inside vegetation;
* fails closed when slope/coverage is insufficient or Method 1 is inapplicable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

from property_scores.bal_prescreen import tables
from property_scores.bal_prescreen.prescreen import (
    MAX_VEG_M,
    MIN_PATCH_AREA_M2,
    VEG_MAP,
    WC_GRASS,
    _CLASSIFIED_WC,
    _bal_rank,
    _grassland_excluded,
    _haversine_m,
    _slope_band,
)

V2_SCHEMA_VERSION = "preliminary-bal-shadow-v2"
SECTORS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
MIN_SLOPE_RUN_M = 10.0
SLOPE_SECTOR_HALF_WIDTH_DEG = 22.5
PATCH_SEARCH_RADIUS_M = 250
_IDENTITY_PROOF = object()


def _finite_coordinate(lat: float, lng: float) -> bool:
    return (isinstance(lat, (int, float)) and not isinstance(lat, bool)
            and isinstance(lng, (int, float)) and not isinstance(lng, bool)
            and math.isfinite(float(lat)) and math.isfinite(float(lng))
            and -90 <= float(lat) <= 90 and -180 <= float(lng) <= 180)


@dataclass(frozen=True, slots=True)
class VerifiedBuildingPoint:
    """Trusted internal subject evidence; never construct from request JSON."""

    lat: float
    lng: float
    authority: str
    reference: str
    evidence: dict
    _proof: object = field(repr=False)

    def __post_init__(self):
        if self._proof is not _IDENTITY_PROOF:
            raise ValueError("VerifiedBuildingPoint must come from a trusted resolver")

    def as_dict(self) -> dict:
        return {
            "kind": "building_point", "verified": True,
            "lat": self.lat, "lng": self.lng,
            "authority": self.authority, "reference": self.reference,
            "evidence": self.evidence,
        }


def building_point_from_overture(lat: float, lng: float) -> VerifiedBuildingPoint | None:
    """Issue subject evidence only for strict polygon containment, no 30 m fallback."""
    if not _finite_coordinate(lat, lng):
        return None
    from property_scores.common.overture import building_containing_point_m2, get_db
    area = building_containing_point_m2(get_db(), lat, lng)
    if area is None or not math.isfinite(float(area)) or area <= 0:
        return None
    return VerifiedBuildingPoint(
        float(lat), float(lng), "overture_building_containment",
        "Overture building polygon strictly contains G-NAF point",
        {"building_footprint_m2": round(float(area), 1)}, _IDENTITY_PROOF)


def building_point_from_professional_report(
        lat: float, lng: float, *, report_url: str,
        coordinate_evidence: str) -> VerifiedBuildingPoint | None:
    """Issue evidence for coordinates explicitly printed in a professional report."""
    if (not _finite_coordinate(lat, lng)
            or not isinstance(report_url, str) or not report_url.startswith("https://")
            or not str(coordinate_evidence or "").strip()):
        return None
    return VerifiedBuildingPoint(
        float(lat), float(lng), "published_professional_report",
        report_url, {"coordinate_evidence": coordinate_evidence}, _IDENTITY_PROOF)


def _bearing_deg(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Initial bearing in degrees, clockwise from north."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lng2 - lng1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _sector(bearing: float) -> str:
    return SECTORS[int((bearing + 22.5) // 45.0) % 8]


def _angular_difference(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def _identity_error(subject_identity: VerifiedBuildingPoint | None,
                    lat: float, lng: float) -> str | None:
    if not _finite_coordinate(lat, lng):
        return "finite calculation coordinates are required"
    if not isinstance(subject_identity, VerifiedBuildingPoint):
        return "trusted VerifiedBuildingPoint required"
    identity_lat = subject_identity.lat
    identity_lng = subject_identity.lng
    if not _finite_coordinate(identity_lat, identity_lng):
        return "finite building point coordinates are required"
    if _haversine_m(lat, lng, identity_lat, identity_lng) > 5.0:
        return "building point does not match calculation coordinates"
    return None


def _grid_geometry(grid: dict, lat: float) -> dict | None:
    classes = grid.get("classes")
    nrows, ncols = grid.get("nrows"), grid.get("ncols")
    bbox = grid.get("bbox")
    if (not isinstance(classes, list) or not isinstance(nrows, int)
            or not isinstance(ncols, int) or nrows <= 0 or ncols <= 0
            or not isinstance(bbox, list) or len(bbox) != 4):
        return None
    if len(classes) != nrows or any(not isinstance(row, list) or len(row) != ncols
                                    for row in classes):
        return None
    west, south, east, north = (float(v) for v in bbox)
    dlat = (north - south) / nrows
    dlng = (east - west) / ncols
    pixel_height_m = abs(dlat) * 111_320.0
    pixel_width_m = abs(dlng) * 111_320.0 * max(math.cos(math.radians(lat)), 0.1)
    pixel_area_m2 = pixel_height_m * pixel_width_m
    if pixel_area_m2 <= 0:
        return None
    return {
        "classes": classes, "nrows": nrows, "ncols": ncols,
        "west": west, "north": north, "dlat": dlat, "dlng": dlng,
        "pixel_area_m2": pixel_area_m2,
    }


def _pixel(geometry: dict, row: int, col: int, lat: float, lng: float) -> dict:
    plat = geometry["north"] - (row + 0.5) * geometry["dlat"]
    plng = geometry["west"] + (col + 0.5) * geometry["dlng"]
    bearing = _bearing_deg(lat, lng, plat, plng)
    return {
        "row": row, "col": col, "lat": plat, "lng": plng,
        "distance_m": _haversine_m(lat, lng, plat, plng),
        "bearing_deg": bearing, "sector": _sector(bearing),
        "wc_class": geometry["classes"][row][col],
    }


def scan_vegetation_observations(lat: float, lng: float,
                                 *, grid: dict | None = None) -> dict:
    """Return every class/component observation within the 100 m BAL window.

    Connectivity is across all combustible WorldCover classes, so adjacent
    tree and shrub pixels can jointly satisfy one hectare. Each vegetation
    class inside a qualifying component remains a separate BAL observation.
    A component clipped by the 150 m raster window is conservatively retained
    even when the visible part is under one hectare, and flagged as truncated.
    """
    if grid is None:
        from property_scores.bushfire.score import landcover_grid
        grid = landcover_grid(lat, lng, radius_m=PATCH_SEARCH_RADIUS_M)
    if not grid:
        return {"status": "unavailable", "reason": "WorldCover unavailable",
                "observations": []}
    geometry = _grid_geometry(grid, lat)
    if geometry is None:
        return {"status": "unavailable", "reason": "invalid land-cover grid",
                "observations": []}

    classes = geometry["classes"]
    nrows, ncols = geometry["nrows"], geometry["ncols"]
    visited: set[tuple[int, int]] = set()
    observations = []
    component_summaries = []
    component_id = 0

    for start_r in range(nrows):
        for start_c in range(ncols):
            start = (start_r, start_c)
            if start in visited or classes[start_r][start_c] not in _CLASSIFIED_WC:
                continue
            component_id += 1
            stack = [start]
            visited.add(start)
            cells: list[tuple[int, int]] = []
            touches_edge = False
            while stack:
                row, col = stack.pop()
                cells.append((row, col))
                if row in (0, nrows - 1) or col in (0, ncols - 1):
                    touches_edge = True
                for dr in (-1, 0, 1):
                    for dc in (-1, 0, 1):
                        if dr == 0 and dc == 0:
                            continue
                        nr, nc = row + dr, col + dc
                        neighbour = (nr, nc)
                        if not (0 <= nr < nrows and 0 <= nc < ncols):
                            continue
                        if neighbour in visited or classes[nr][nc] not in _CLASSIFIED_WC:
                            continue
                        visited.add(neighbour)
                        stack.append(neighbour)

            area_m2 = len(cells) * geometry["pixel_area_m2"]
            qualifies = area_m2 >= MIN_PATCH_AREA_M2 or touches_edge
            component_summaries.append({
                "component_id": component_id,
                "pixel_count": len(cells),
                "observed_area_m2": round(area_m2, 1),
                "touches_grid_edge": touches_edge,
                "qualification": ("observed_ge_1ha" if area_m2 >= MIN_PATCH_AREA_M2
                                  else "edge_continuation_assumed" if touches_edge
                                  else "under_1ha"),
            })
            if not qualifies:
                continue

            pixels = [_pixel(geometry, row, col, lat, lng) for row, col in cells]
            by_class: dict[int, list[dict]] = {}
            for pixel in pixels:
                by_class.setdefault(pixel["wc_class"], []).append(pixel)
            for wc_class, class_pixels in by_class.items():
                inside = [p for p in class_pixels if p["distance_m"] <= MAX_VEG_M]
                if not inside:
                    continue
                by_sector: dict[str, list[dict]] = {}
                for pixel in inside:
                    by_sector.setdefault(pixel["sector"], []).append(pixel)
                for sector, sector_pixels in by_sector.items():
                    nearest = min(sector_pixels, key=lambda p: p["distance_m"])
                    observations.append({
                        "component_id": component_id,
                        "wc_class": wc_class,
                        "distance_m": round(nearest["distance_m"], 1),
                        "bearing_deg": round(nearest["bearing_deg"], 1),
                        "sector": sector,
                        "veg_lat": nearest["lat"], "veg_lng": nearest["lng"],
                        "component_area_m2": round(area_m2, 1),
                        "component_edge_truncated": touches_edge,
                        # Area qualification is mixed-class; slope is not. An
                        # observation's effective slope must stay under its
                        # own vegetation class.
                        "_component_pixels": class_pixels,
                        "_near_cell": (nearest["row"], nearest["col"]),
                    })

    observations.sort(key=lambda item: (item["distance_m"], item["component_id"],
                                        item["wc_class"], item["sector"]))
    return {
        "status": "ok",
        "pixel_area_m2": round(geometry["pixel_area_m2"], 1),
        "components": component_summaries,
        "observations": observations,
    }


def _bresenham_cells(start: tuple[int, int], end: tuple[int, int]) -> list[tuple[int, int]]:
    """Integer raster cells on a straight line, including both endpoints."""
    row0, col0 = start
    row1, col1 = end
    cells = []
    dcol = abs(col1 - col0)
    drow = -abs(row1 - row0)
    step_col = 1 if col0 < col1 else -1
    step_row = 1 if row0 < row1 else -1
    error = dcol + drow
    while True:
        cells.append((row0, col0))
        if row0 == row1 and col0 == col1:
            return cells
        twice = 2 * error
        if twice >= drow:
            error += drow
            col0 += step_col
        if twice <= dcol:
            error += dcol
            row0 += step_row


def effective_slope_under_vegetation(
        lat: float, lng: float, observation: dict,
        *, elevation_fn: Callable[[float, float], float | None] | None = None) -> dict:
    """Estimate slope along a radial profile wholly inside the vegetation.

    The nearest classified pixel is the near end. The far end is the most
    distant pixel in the same mixed component and 45-degree sector. Both ends
    are vegetation pixels; the building-to-vegetation gap is not used as slope.
    """
    if elevation_fn is None:
        from property_scores.common import terrain
        elevation_fn = terrain.elevation
    pixels = observation.get("_component_pixels") or []
    near_cell = observation.get("_near_cell")
    try:
        bearing = float(observation["bearing_deg"])
        near_distance = float(observation["distance_m"])
    except (KeyError, TypeError, ValueError):
        return {"status": "unavailable", "reason": "invalid observation geometry",
                "band": None}
    if (not math.isfinite(bearing) or not math.isfinite(near_distance)
            or not isinstance(near_cell, tuple) or len(near_cell) != 2):
        return {"status": "unavailable", "reason": "invalid observation geometry",
                "band": None}
    class_cells = {(pixel.get("row"), pixel.get("col")) for pixel in pixels}
    candidates = [
        pixel for pixel in pixels
        if isinstance(pixel.get("distance_m"), (int, float))
        and math.isfinite(float(pixel["distance_m"]))
        and pixel["distance_m"] >= near_distance + MIN_SLOPE_RUN_M
        and isinstance(pixel.get("bearing_deg"), (int, float))
        and math.isfinite(float(pixel["bearing_deg"]))
        and _angular_difference(pixel["bearing_deg"], bearing)
        <= SLOPE_SECTOR_HALF_WIDTH_DEG
        and isinstance(pixel.get("row"), int) and isinstance(pixel.get("col"), int)
        and all(cell in class_cells for cell in _bresenham_cells(
            near_cell, (pixel["row"], pixel["col"])))
    ]
    if not candidates:
        return {"status": "unavailable", "reason": "insufficient vegetation run",
                "band": None}
    near_elevation = elevation_fn(observation["veg_lat"], observation["veg_lng"])
    if (near_elevation is None or not isinstance(near_elevation, (int, float))
            or not math.isfinite(float(near_elevation))):
        return {"status": "unavailable", "reason": "terrain elevation unavailable",
                "band": None}

    # Every continuous ray candidate is evaluated. The most dangerous valid
    # downslope governs; choosing only a centreline pixel can miss a steeper
    # branch inside the same sector and distance band.
    profiles = []
    for candidate in candidates:
        run_m = _haversine_m(observation["veg_lat"], observation["veg_lng"],
                             candidate["lat"], candidate["lng"])
        if not math.isfinite(run_m) or run_m < MIN_SLOPE_RUN_M:
            continue
        far_elevation = elevation_fn(candidate["lat"], candidate["lng"])
        if (far_elevation is None or not isinstance(far_elevation, (int, float))
                or not math.isfinite(float(far_elevation))):
            return {"status": "unavailable", "reason": "terrain profile incomplete",
                    "band": None}
        rise_m = float(near_elevation) - float(far_elevation)
        signed_deg = math.degrees(math.atan(rise_m / run_m))
        if not math.isfinite(signed_deg):
            return {"status": "unavailable", "reason": "invalid terrain slope",
                    "band": None}
        profiles.append({
            "signed_deg": signed_deg,
            "run_m": run_m, "far_elevation_m": float(far_elevation),
        })
    if not profiles:
        return {"status": "unavailable", "reason": "vegetation run under 10 m",
                "band": None}
    governing = max(profiles, key=lambda profile: profile["signed_deg"])
    signed_deg = governing["signed_deg"]
    run_m = governing["run_m"]
    far_elevation = governing["far_elevation_m"]
    # Sub-half-degree differences are below a defensible signal once DEM
    # vertical uncertainty and raster resampling are considered.
    if signed_deg <= 0.5:
        return {
            "status": "ok", "direction": "flat/upslope", "deg": round(signed_deg, 1),
            "band": "flat", "run_m": round(run_m, 1),
            "near_elevation_m": round(float(near_elevation), 1),
            "far_elevation_m": round(float(far_elevation), 1),
            "profile_points_sampled": len(profiles),
        }
    if signed_deg > 20:
        return {
            "status": "method1_inapplicable", "direction": "downslope",
            "deg": round(signed_deg, 1), "band": None, "run_m": round(run_m, 1),
            "reason": "effective downslope exceeds 20 degrees",
            "profile_points_sampled": len(profiles),
        }
    return {
        "status": "ok", "direction": "downslope", "deg": round(signed_deg, 1),
        "band": _slope_band(signed_deg), "run_m": round(run_m, 1),
        "near_elevation_m": round(float(near_elevation), 1),
        "far_elevation_m": round(float(far_elevation), 1),
        "profile_points_sampled": len(profiles),
    }


def _public_observation(observation: dict) -> dict:
    return {key: value for key, value in observation.items()
            if not key.startswith("_")}


def preliminary_bal_v2(
        lat: float, lng: float, *, subject_identity: VerifiedBuildingPoint | None,
        state: str | None = None, overlay=None, grid: dict | None = None,
        elevation_fn: Callable[[float, float], float | None] | None = None) -> dict:
    """Calculate the internal v2 shadow result or fail closed with no BAL."""
    identity_error = _identity_error(subject_identity, lat, lng)
    if identity_error:
        return {
            "schema_version": V2_SCHEMA_VERSION,
            "status": "identity_required", "preliminary_bal": None,
            "reason": identity_error,
            "subject_identity": (subject_identity.as_dict()
                                 if isinstance(subject_identity, VerifiedBuildingPoint)
                                 else None),
        }

    from property_scores.bushfire.score import _detect_state, _overlay_check
    if state is None:
        state = _detect_state(lat, lng)
    if not state:
        return {"schema_version": V2_SCHEMA_VERSION, "status": "outside_coverage",
                "preliminary_bal": None, "reason": "outside Australia"}

    if elevation_fn is None:
        from property_scores.common import terrain
        elevation_fn = terrain.elevation
    site_elevation = elevation_fn(lat, lng)
    fdi, fdi_basis = tables.resolve_fdi(state, site_elevation)
    fdi_table, fdi_used, fdi_substituted = tables.table_for_fdi(fdi)
    scan = scan_vegetation_observations(lat, lng, grid=grid)

    if overlay is None:
        overlay = _overlay_check(state, lat, lng)
    worst_severity, hits, category, overlay_ok, overlay_basis = overlay
    overlay_clear = worst_severity is None and overlay_ok and overlay_basis is not None
    overlay_status = "in_zone" if hits else "outside" if overlay_clear else "unavailable"
    official = {"status": overlay_status, "zones": hits, "category": category,
                "basis": overlay_basis}

    if scan["status"] != "ok":
        return {
            "schema_version": V2_SCHEMA_VERSION,
            "status": "data_unavailable", "preliminary_bal": None,
            "reason": scan["reason"], "state": state,
            "subject_identity": subject_identity.as_dict(), "official_overlay": official,
        }

    observations = scan["observations"]
    if not observations:
        status = "professional_assessment_required" if overlay_status == "in_zone" else "ok"
        return {
            "schema_version": V2_SCHEMA_VERSION, "status": status,
            "preliminary_bal": None if status != "ok" else "BAL-LOW",
            "bal_range": None if status != "ok" else ["BAL-LOW", "BAL-LOW"],
            "reason": ("official overlay hit but no qualifying vegetation resolved"
                       if status != "ok" else "no qualifying vegetation within 100 m"),
            "state": state, "fdi": fdi, "fdi_basis": fdi_basis,
            "subject_identity": subject_identity.as_dict(), "official_overlay": official,
            "directions_assessed": [], "observations": [],
        }

    assessed = []
    contributing = []
    blockers = []
    for observation in observations:
        public = _public_observation(observation)
        wc_class = observation["wc_class"]
        distance = observation["distance_m"]
        if wc_class == WC_GRASS and _grassland_excluded(fdi_used, distance):
            public["slope"] = {"status": "not_required", "band": None}
            public["excluded"] = "grassland outside 50 m for this FDI"
            public["point_bal"] = "BAL-LOW"
            public["lighter_bal"] = "BAL-LOW"
            assessed.append(public)
            continue

        slope = effective_slope_under_vegetation(
            lat, lng, observation, elevation_fn=elevation_fn)
        public["slope"] = slope
        if slope["status"] != "ok":
            blockers.append({
                "component_id": observation["component_id"],
                "wc_class": observation["wc_class"],
                "sector": observation["sector"],
                "reason": slope.get("reason", slope["status"]),
            })
            assessed.append(public)
            continue

        point_class, light_class, label = VEG_MAP[wc_class]
        point_bal = tables.lookup_bal(fdi_table, slope["band"], point_class, distance)
        light_bal = tables.lookup_bal(fdi_table, slope["band"], light_class, distance)
        public.update({
            "vegetation_label": label,
            "point_class": point_class, "lighter_class": light_class,
        })
        public["point_bal"] = point_bal
        public["lighter_bal"] = light_bal
        assessed.append(public)
        contributing.append(public)

    if blockers:
        return {
            "schema_version": V2_SCHEMA_VERSION,
            "status": "professional_assessment_required",
            "preliminary_bal": None,
            "reason": "one or more qualifying vegetation observations lack a valid Method 1 slope",
            "blockers": blockers, "state": state, "fdi": fdi,
            "fdi_basis": fdi_basis, "subject_identity": subject_identity.as_dict(),
            "official_overlay": official,
            "directions_assessed": sorted({item["sector"] for item in assessed}),
            "observations": assessed,
        }

    if not contributing:
        if overlay_status == "in_zone":
            return {
                "schema_version": V2_SCHEMA_VERSION,
                "status": "professional_assessment_required",
                "preliminary_bal": None,
                "reason": "official overlay hit but no contributing vegetation was resolved",
                "state": state, "fdi": fdi, "fdi_basis": fdi_basis,
                "subject_identity": subject_identity.as_dict(),
                "official_overlay": official,
                "directions_assessed": sorted({item["sector"] for item in assessed}),
                "observations": assessed,
            }
        return {
            "schema_version": V2_SCHEMA_VERSION, "status": "ok",
            "preliminary_bal": "BAL-LOW", "bal_range": ["BAL-LOW", "BAL-LOW"],
            "confidence": "low" if overlay_status == "unavailable" else "moderate",
            "reason": "all observed vegetation was excluded by the method",
            "state": state, "fdi": fdi, "fdi_basis": fdi_basis,
            "subject_identity": subject_identity.as_dict(),
            "official_overlay": official,
            "directions_assessed": sorted({item["sector"] for item in assessed}),
            "observations": assessed,
        }

    limiting = max(contributing, key=lambda item: _bal_rank(item["point_bal"]))
    point_bal = limiting["point_bal"]
    low_bal = max((item["lighter_bal"] for item in contributing), key=_bal_rank)
    edge_uncertain = any(item["component_edge_truncated"] for item in contributing)
    formation_uncertain = any(item.get("point_class") != item.get("lighter_class")
                              for item in contributing)
    overlay_disagreement = (overlay_status == "outside"
                            and _bal_rank(point_bal) >= _bal_rank("BAL-29"))
    # A planning overlay is a broad designation, not a classification of each
    # WorldCover component. Satellite cover alone cannot tell whether a park,
    # maintained garden or narrow strip is excludable low-threat vegetation.
    low_threat_unresolved = point_bal != "BAL-LOW"
    if low_threat_unresolved:
        # WorldCover describes cover, not whether managed/excludable vegetation
        # meets AS low-threat rules. The mild end must allow that the observed
        # cover is excludable even when the wider property is inside a BPA.
        low_bal = "BAL-LOW"
    confidence = ("low" if edge_uncertain or overlay_disagreement
                  or low_threat_unresolved else "moderate")

    return {
        "schema_version": V2_SCHEMA_VERSION, "status": "ok",
        "preliminary_bal": point_bal, "bal_range": [low_bal, point_bal],
        "confidence": confidence,
        "state": state, "fdi": fdi, "fdi_basis": fdi_basis,
        "fdi_substituted": fdi_substituted,
        "subject_identity": subject_identity.as_dict(),
        "directions_assessed": sorted({item["sector"] for item in assessed}),
        "limiting_observation": limiting,
        "observations": assessed,
        "official_overlay": official,
        "uncertainty": {
            "formation": formation_uncertain,
            "edge_truncated_component": edge_uncertain,
            "official_model_disagreement": overlay_disagreement,
            "low_threat_status": ("unresolved_from_worldcover"
                                  if low_threat_unresolved else "not_triggered"),
        },
        "method": "GA 2009 Method 1 engineering shadow; not current AS 3959 conformity",
        "regulatory_use": "not_permitted",
        "formal_assessment_required": True,
    }
