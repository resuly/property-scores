"""Download SA DIT "Traffic Volume Estimates" and write data/aadt_sa.parquet.

Source: South Australia Dept for Infrastructure and Transport, data.sa.gov.au
"Traffic Volumes" — per-segment GeoJSON (zipped), no auth. LineString segments
across the SA road network with an estimated AADT (TESECN_VOLUME) and commercial
vehicle percent (CV_PERCENT).

Per-segment network coverage (like VIC). Consumed by aadt_near()'s glob.
"""

import io
import json
import os
import sys
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from property_scores.common.aadt_build import write_aadt_parquet, data_out, clamp_hv, coords_to_wkt  # noqa: E402

ZIP_URL = ("https://data.sa.gov.au/data/dataset/e5d6588a-f163-4f6a-bc57-25e95c87b5bd/"
           "resource/daf8098d-4ffb-4b07-b347-6b3c204add43/download/"
           "trafficvolumeestimates2024_geojson.zip")


def main():
    print("downloading SA traffic volume estimates...", flush=True)
    req = urllib.request.Request(ZIP_URL, headers={"User-Agent": "property-scores/sa-aadt"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        blob = resp.read()
    zf = zipfile.ZipFile(io.BytesIO(blob))
    name = next(n for n in zf.namelist() if n.lower().endswith((".geojson", ".json")))
    data = json.loads(zf.read(name).decode("utf-8", errors="replace"))

    rows = []
    for ft in data.get("features", []):
        a = ft.get("properties", {})
        geom = ft.get("geometry") or {}
        aadt = a.get("TESECN_VOLUME")
        if not aadt or aadt <= 0:
            continue
        wkt = coords_to_wkt(geom.get("type"), geom.get("coordinates"))
        if not wkt:
            continue
        rows.append({
            "aadt": int(aadt),
            "hv_pct": clamp_hv(a.get("CV_PERCENT")),
            "road_name": (str(a.get("ROAD_NO")).strip() or None) if a.get("ROAD_NO") else None,
            "wkt": wkt,
        })
    print(f"  {len(rows)} usable segments", flush=True)
    if not rows:
        print("ERROR: no SA rows", file=sys.stderr)
        return 2
    out = data_out("aadt_sa.parquet")
    n = write_aadt_parquet(rows, out)
    print(f"wrote {n} rows -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
