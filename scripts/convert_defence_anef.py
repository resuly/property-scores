"""Convert Defence ANEF KML polygons to GeoJSON parquet for DuckDB queries."""

import json
import re
from xml.etree import ElementTree as ET

from property_scores.common.config import data_path


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
            coords_el = pm.find(".//kml:coordinates", ns)
            if coords_el is None:
                continue

            # Parse "lng,lat,alt lng,lat,alt ..."
            coords_text = coords_el.text.strip()
            ring = []
            for pt in coords_text.split():
                parts = pt.split(",")
                if len(parts) >= 2:
                    ring.append([float(parts[0]), float(parts[1])])
            if len(ring) < 3:
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

            features.append({
                "type": "Feature",
                "properties": {
                    "airfield": airfield,
                    "contour": contour,
                    "anef_min": anef_min,
                    "source": "defence",
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [ring],
                },
            })

    return {"type": "FeatureCollection", "features": features}


def main():
    geojson = parse_kml()
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
