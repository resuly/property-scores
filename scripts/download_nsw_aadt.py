"""Download TfNSW Road Traffic Counts and write data/aadt_nsw.parquet.

Source: Transport for NSW open data "Roads Traffic Volume Counts" — two public
CSVs, no auth:
  - station reference (station_key + wgs84 lat/lng + road_name)
  - yearly summary (AADT = traffic_count where period='ALL DAYS')

AADT row picked: period='ALL DAYS', classification='ALL VEHICLES',
direction='PRESCRIBED AND COUNTER' (two-way all-vehicle), latest year per
station; falls back to classification 'UNCLASSIFIED' two-way total.

NSW publishes counts at STATIONS (sparse), not per road segment, so coverage is
thinner than VIC's network layer. Point geometry. Consumed by aadt_near() glob.
"""

import csv
import io
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from property_scores.common.aadt_build import write_aadt_parquet, data_out  # noqa: E402

BASE = ("https://opendata.transport.nsw.gov.au/data/dataset/"
        "ef2b0bd2-db1e-48f3-9ea1-2bb9e6bc6504/resource/")
STATION_CSV = BASE + "c65ad7b4-0257-4cc6-953e-5299ac8d27ba/download/road_traffic_counts_station_reference.csv"
YEARLY_CSV = BASE + "cba9a012-c305-414e-b848-f0e3aad18d97/download/road_traffic_counts_yearly_summary.csv"


def _fetch_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "property-scores/nsw-aadt"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return resp.read().decode("utf-8", errors="replace")


def main():
    print("downloading station reference...", flush=True)
    stations = {}
    for row in csv.DictReader(io.StringIO(_fetch_text(STATION_CSV))):
        key = row.get("station_key")
        try:
            lat = float(row.get("wgs84_latitude") or "")
            lng = float(row.get("wgs84_longitude") or "")
        except ValueError:
            continue
        if key:
            stations[key] = (lng, lat, (row.get("road_name") or "").strip() or None)
    print(f"  {len(stations)} stations with coords", flush=True)

    print("downloading yearly summary (47MB)...", flush=True)
    # pick best AADT per station: prefer ALL VEHICLES two-way, else UNCLASSIFIED two-way; latest year
    best = {}  # station_key -> (priority, year, aadt)
    n = 0
    for row in csv.DictReader(io.StringIO(_fetch_text(YEARLY_CSV))):
        n += 1
        if (row.get("period") or "").upper() != "ALL DAYS":
            continue
        direction = (row.get("traffic_direction_name") or "").upper()
        if direction != "PRESCRIBED AND COUNTER":
            continue
        cls = (row.get("classification_type") or "").upper()
        if cls == "ALL VEHICLES":
            prio = 2
        elif cls == "UNCLASSIFIED":
            prio = 1
        else:
            continue
        key = row.get("station_key")
        if key not in stations:
            continue
        try:
            aadt = int(float(row.get("traffic_count") or 0))
            year = int(row.get("year") or 0)
        except ValueError:
            continue
        if aadt <= 0:
            continue
        cur = best.get(key)
        if cur and (cur[0] > prio or (cur[0] == prio and cur[1] >= year)):
            continue
        best[key] = (prio, year, aadt)
    print(f"  scanned {n} rows, {len(best)} stations with AADT", flush=True)

    rows = []
    for key, (_, _, aadt) in best.items():
        lng, lat, road = stations[key]
        rows.append({"aadt": aadt, "hv_pct": 0.05, "road_name": road,
                     "wkt": f"POINT ({lng} {lat})"})
    if not rows:
        print("ERROR: no NSW rows", file=sys.stderr)
        return 2
    out = data_out("aadt_nsw.parquet")
    written = write_aadt_parquet(rows, out)
    print(f"wrote {written} rows -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
