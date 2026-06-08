"""Download QLD TMR "Road location and traffic data" and write data/aadt_qld.parquet.

Source: Queensland Dept of Transport and Main Roads, data.qld.gov.au — CSV of
state-declared road locations with Latitude/Longitude and AADT, no auth.

Point sites along the QLD state-declared network. Consumed by aadt_near() glob.
"""

import csv
import io
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from property_scores.common.aadt_build import write_aadt_parquet, data_out  # noqa: E402

CSV_URL = ("https://www.data.qld.gov.au/dataset/1c1d8224-0a5e-4a0c-ad6f-025096d206ba/"
           "resource/daab3617-077f-450a-a1c0-57c26d8ba47c/download/"
           "road-location-and-traffic-data_20260505.csv")


def _road_name(desc):
    if not desc:
        return None
    # Description looks like "1000_1|EAST COAST ROAD"
    parts = str(desc).split("|", 1)
    return (parts[1].strip() if len(parts) > 1 else parts[0].strip()) or None


def main():
    print("downloading QLD road/traffic CSV...", flush=True)
    req = urllib.request.Request(CSV_URL, headers={"User-Agent": "property-scores/qld-aadt"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        text = resp.read().decode("utf-8-sig", errors="replace")

    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        try:
            lat = float(r.get("Latitude") or "")
            lng = float(r.get("Longitude") or "")
            aadt = int(float(r.get("AADT") or 0))
        except ValueError:
            continue
        if aadt <= 0:
            continue
        rows.append({
            "aadt": aadt,
            "hv_pct": 0.05,
            "road_name": _road_name(r.get("Description")),
            "wkt": f"POINT ({lng} {lat})",
        })
    print(f"  {len(rows)} usable sites", flush=True)
    if not rows:
        print("ERROR: no QLD rows", file=sys.stderr)
        return 2
    out = data_out("aadt_qld.parquet")
    n = write_aadt_parquet(rows, out)
    print(f"wrote {n} rows -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
