#!/usr/bin/env python3
"""Build au_rail_stops.parquet from GTFS feeds (train + tram stops only).

Downloads GTFS zips from each state's transit authority, extracts stops.txt,
filters to rail/tram stops (route_type 0, 1, 2), and writes a single parquet.

Output columns: stop_id, stop_name, lat, lng, route_type, state
"""
import csv
import io
import sys
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen
import base64, os

SOURCES = {
    "vic": "http://data.ptv.vic.gov.au/downloads/gtfs.zip",
    "nsw": "https://opendata.transport.nsw.gov.au/data/dataset/d1f68d4f-b778-44df-9823-cf2fa922e47f/resource/67974f14-01bf-47b7-bfa5-c7f2f8a950ca/download/full_greater_sydney_gtfs_static_0.zip",
    "qld": "https://gtfsrt.api.translink.com.au/GTFS/SEQ_GTFS.zip",
    "sa": "https://gtfs.adelaidemetro.com.au/v1/static/latest/google_transit.zip",
    "wa": "http://www.transperth.wa.gov.au/TimetablePDFs/GoogleTransit/Production/google_transit.zip",
    "tas": "https://www.transport.tas.gov.au/__data/assets/file/0011/557615/tas_gtfs.zip",
}

def _dict_reader(f):
    """DictReader with stripped header names (Transperth ships ' stop_id')."""
    rdr = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
    rdr.fieldnames = [fn.strip() for fn in rdr.fieldnames or []]
    return rdr


RAIL_TYPES = {0, 1, 2}  # tram, metro, train


def _read_stops(zf, state):
    """Extract stops from a GTFS zip, optionally nested (VIC has inner zips)."""
    stops = []
    # Check for stops.txt directly
    names = zf.namelist()
    stop_files = [n for n in names if n.endswith("stops.txt")]
    inner_zips = [n for n in names if n.endswith(".zip")]

    for sf in stop_files:
        with zf.open(sf) as f:
            for row in _dict_reader(f):
                try:
                    lat = float(row.get("stop_lat", 0))
                    lng = float(row.get("stop_lon", 0))
                except (ValueError, TypeError):
                    continue
                if lat == 0 or lng == 0:
                    continue
                name = row.get("stop_name", "").strip()
                sid = row.get("stop_id", "").strip()
                loc_type = row.get("location_type", "0")
                if loc_type not in ("", "0", "1"):
                    continue
                stops.append({
                    "stop_id": f"{state}:{sid}",
                    "stop_name": name,
                    "lat": lat,
                    "lng": lng,
                    "state": state,
                })

    for iz in inner_zips:
        try:
            with zf.open(iz) as inner_f:
                inner_data = io.BytesIO(inner_f.read())
                with zipfile.ZipFile(inner_data) as inner_zf:
                    stops.extend(_read_stops(inner_zf, state))
        except Exception as e:
            print(f"  skip inner zip {iz}: {e}", file=sys.stderr)
    return stops


def _filter_rail_stops(stops, zf, state):
    """Filter stops to only those served by rail/tram routes."""
    names = zf.namelist()
    routes_files = [n for n in names if n.endswith("routes.txt")]
    trips_files = [n for n in names if n.endswith("trips.txt")]
    stop_times_files = [n for n in names if n.endswith("stop_times.txt")]

    rail_route_ids = set()
    for rf in routes_files:
        with zf.open(rf) as f:
            for row in _dict_reader(f):
                rt = int(row.get("route_type", -1))
                if rt in RAIL_TYPES:
                    rail_route_ids.add(row["route_id"])

    if not rail_route_ids:
        return []

    rail_trip_ids = set()
    for tf in trips_files:
        with zf.open(tf) as f:
            for row in _dict_reader(f):
                if row.get("route_id") in rail_route_ids:
                    rail_trip_ids.add(row["trip_id"])

    rail_stop_ids = set()
    for stf in stop_times_files:
        with zf.open(stf) as f:
            for row in _dict_reader(f):
                if row.get("trip_id") in rail_trip_ids:
                    rail_stop_ids.add(row["stop_id"])

    stop_id_set = {f"{state}:{sid}" for sid in rail_stop_ids}
    return [s for s in stops if s["stop_id"] in stop_id_set]


def main():
    out_path = Path(__file__).resolve().parents[1] / "data" / "au_rail_stops.parquet"
    all_stops = []

    for state, url in SOURCES.items():
        print(f"Downloading {state}...", file=sys.stderr)
        try:
            req = Request(url, headers={"User-Agent": "property-scores/1.0"})
            resp = urlopen(req, timeout=60)
            data = io.BytesIO(resp.read())
            with zipfile.ZipFile(data) as zf:
                stops = _read_stops(zf, state)
                print(f"  {len(stops)} total stops", file=sys.stderr)
                if state == "vic":
                    # VIC has nested zips, harder to filter by route type
                    # Keep stops with "Station" or "Stop" in name as heuristic
                    rail = [s for s in stops if "station" in s["stop_name"].lower()
                            or "railway" in s["stop_name"].lower()]
                    print(f"  {len(rail)} rail/station stops (name filter)", file=sys.stderr)
                else:
                    rail = _filter_rail_stops(stops, zf, state)
                    print(f"  {len(rail)} rail stops (GTFS route_type filter)", file=sys.stderr)
                    if not rail:
                        rail = [s for s in stops if "station" in s["stop_name"].lower()]
                        print(f"  fallback: {len(rail)} by name filter", file=sys.stderr)
                all_stops.extend(rail)
        except Exception as e:
            print(f"  FAILED: {e}", file=sys.stderr)

    # Deduplicate by rounding lat/lng to 4 decimals
    seen = set()
    deduped = []
    for s in all_stops:
        key = (round(s["lat"], 4), round(s["lng"], 4))
        if key not in seen:
            seen.add(key)
            deduped.append(s)

    print(f"\nTotal: {len(all_stops)} raw, {len(deduped)} after dedup", file=sys.stderr)
    if not deduped:
        sys.exit("no stops extracted, refusing to overwrite the parquet (2026-06-11 guard)")

    import duckdb, pyarrow as pa
    db = duckdb.connect()
    tbl = pa.table({
        "stop_id": [s["stop_id"] for s in deduped],
        "stop_name": [s["stop_name"] for s in deduped],
        "lat": [s["lat"] for s in deduped],
        "lng": [s["lng"] for s in deduped],
        "state": [s["state"] for s in deduped],
    })
    db.execute(f"COPY tbl TO '{out_path}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    print(f"Written to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
