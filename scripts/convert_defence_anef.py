"""Convert Defence ANEF KML polygons to GeoJSON parquet for DuckDB queries."""

import json
import re
import statistics
from xml.etree import ElementTree as ET

from property_scores.common.config import data_path

# Upstream ANEF-40 polygons are attributed to the wrong base. Measured against
# the source KML (2026-07-26): the placemark "RAAF Base Townsville - Contour
# Range 40+" carries a polygon at (-27.632, 152.710), which is Amberley and
# 1,104 km from Townsville's other contours; "RAAF Base Amberley - Contour Range
# 40+" carries one at (-32.750, 151.913), which is Williamtown and 573 km from
# Amberley's. The labels are shifted by one base in Defence's own data -- the
# geometry is right, the name attached to it is not.
#
# This matters: a property inside Amberley's ANEF was reported as "RAAF Base
# Townsville - ANEF 40+", both the wrong airfield and an inflated band (Amberley
# tops out at 35), and ANEF 40+ reads as "only commercial/industrial
# recommended" on a planning report.
#
# Each stray polygon sits within 7 km of exactly one base and hundreds of km
# from the one it is labelled with, so reattribution by geometry is unambiguous.
# Anything that is NOT unambiguous raises rather than shipping a guess.
_OUTLIER_KM = 20.0        # a contour this far from its base's others is misfiled
_REATTRIBUTE_MAX_KM = 15.0  # ...and must land this close to another base

# Upstream misspellings. These render verbatim into the customer-facing
# zone_desc ("RAAF Base Williamown - ANEF 40+"), so they are corrected at
# ingest rather than left to look like our typo.
_NAME_FIXES = {"RAAF Base Williamown": "RAAF Base Williamtown"}


def _centroid(geometry: dict) -> tuple[float, float]:
    """Mean vertex position (lat, lng). Crude, but only used to tell apart
    airfields hundreds of km apart."""
    polys = ([geometry["coordinates"]] if geometry["type"] == "Polygon"
             else geometry["coordinates"])
    pts = [pt for poly in polys for ring in poly for pt in ring]
    return (sum(p[1] for p in pts) / len(pts), sum(p[0] for p in pts) / len(pts))


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    import math
    return math.hypot((a[0] - b[0]) * 111.0,
                      (a[1] - b[1]) * 111.0 * math.cos(math.radians(a[0])))


def fix_misattributed(features: list) -> list:
    """Reassign contours whose geometry belongs to a different airfield.

    Uses each airfield's MEDIAN centroid as its anchor, so a couple of misfiled
    polygons cannot drag the reference. Raises if a stray polygon has no clear
    home -- a new upstream error should stop the build, not ship silently.
    """
    for f in features:
        fixed = _NAME_FIXES.get(f["properties"]["airfield"])
        if fixed:
            f["properties"]["airfield"] = fixed

    cents = {i: _centroid(f["geometry"]) for i, f in enumerate(features)}
    groups: dict[str, list[int]] = {}
    for i, f in enumerate(features):
        groups.setdefault(f["properties"]["airfield"], []).append(i)
    anchor = {a: (statistics.median(cents[i][0] for i in idx),
                  statistics.median(cents[i][1] for i in idx))
              for a, idx in groups.items()}

    for i, f in enumerate(features):
        label = f["properties"]["airfield"]
        if _km(cents[i], anchor[label]) <= _OUTLIER_KM:
            continue
        best = min((a for a in anchor if a != label),
                   key=lambda a: _km(cents[i], anchor[a]))
        d = _km(cents[i], anchor[best])
        if d > _REATTRIBUTE_MAX_KM:
            raise SystemExit(
                f"ANEF polygon #{i} labelled {label!r} sits "
                f"{_km(cents[i], anchor[label]):.0f} km from that base and is not "
                f"within {_REATTRIBUTE_MAX_KM:.0f} km of any other "
                f"(nearest {best!r} at {d:.0f} km). Upstream data changed -- "
                f"resolve by hand before shipping.")
        print(f"  reattributed contour {f['properties']['contour']!r} "
              f"from {label!r} to {best!r} ({d:.0f} km away, was "
              f"{_km(cents[i], anchor[label]):.0f} km from {label!r})")
        f["properties"]["airfield"] = best
        f["properties"]["airfield_source_label"] = label
    return features


def parse_kml():
    kml_path = data_path("defence_anef.kml")
    with open(kml_path) as f:
        content = f.read()

    content = content.replace(
        'xmlns:atom="http://www.w3.org/2005/Atom"',
        'xmlns:atom="http://www.w3.org/2005/Atom" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
    )

    root = ET.fromstring(content)
    ns = {"kml": "http://www.opengis.net/kml/2.2"}

    features = []
    for folder in root.findall(".//kml:Folder", ns):
        fname = folder.findtext("kml:name", "", ns)
        if "Polygon" not in fname:
            continue

        for pm in folder.findall(".//kml:Placemark", ns):
            name = pm.findtext("kml:name", "", ns)

            # A Placemark may be a single <Polygon> or a <MultiGeometry> of many
            # <Polygon>s (e.g. RAAF Base Darwin's contour bands are 2-3 disjoint
            # polygons each). The old code grabbed only the FIRST <coordinates>
            # block, so multi-polygon airfields collapsed to a tiny sliver
            # fragment. Collect every Polygon's outer ring instead.
            polygons = []
            for poly_el in pm.findall(".//kml:Polygon", ns):
                outer = poly_el.find(".//kml:outerBoundaryIs//kml:coordinates", ns)
                if outer is None or not outer.text:
                    continue
                ring = []
                for pt in outer.text.strip().split():
                    parts = pt.split(",")
                    if len(parts) >= 2:
                        ring.append([float(parts[0]), float(parts[1])])
                if len(ring) >= 3:
                    polygons.append([ring])  # [outer]; holes ignored (nested
                    # higher-ANEF bands cover them, and best_anef takes the max)
            if not polygons:
                continue

            # Extract airfield name and contour range
            match = re.match(r"(.+?) - Contour Range (.+)", name)
            if match:
                airfield = match.group(1).strip()
                contour = match.group(2).strip()
            else:
                airfield = name
                contour = "unknown"

            # Parse ANEF min from contour (e.g. "20-25" → 20, "35+" → 35)
            anef_match = re.search(r"(\d+)", contour)
            anef_min = int(anef_match.group(1)) if anef_match else 20

            if len(polygons) == 1:
                geometry = {"type": "Polygon", "coordinates": polygons[0]}
            else:
                geometry = {"type": "MultiPolygon", "coordinates": polygons}

            features.append({
                "type": "Feature",
                "properties": {
                    "airfield": airfield,
                    "contour": contour,
                    "anef_min": anef_min,
                    "source": "defence",
                },
                "geometry": geometry,
            })

    return {"type": "FeatureCollection", "features": features}


def main():
    geojson = parse_kml()
    print("Checking airfield attribution against geometry:")
    geojson["features"] = fix_misattributed(geojson["features"])
    out_path = data_path("defence_anef.geojson")
    with open(out_path, "w") as f:
        json.dump(geojson, f)
    print(f"Converted {len(geojson['features'])} polygons to {out_path}")

    airfields = set(f["properties"]["airfield"] for f in geojson["features"])
    print(f"Airfields ({len(airfields)}):")
    for a in sorted(airfields):
        contours = [f["properties"]["contour"] for f in geojson["features"]
                     if f["properties"]["airfield"] == a]
        print(f"  {a}: {', '.join(contours)}")


if __name__ == "__main__":
    main()
