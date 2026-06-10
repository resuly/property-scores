#!/usr/bin/env python3
"""Build au_bus_stops.parquet from GTFS feeds (bus + tram stops).

Overture places have essentially no Australian bus stops (verified 2026-06-10:
zero stops within 1500 m of Turramurra station's bus interchange), so the
walkability tram_bus scenario reads official GTFS stops instead.

Per state: routes.txt (route_type 3 / 700-799 = bus, 0 / 900 = tram)
-> trips.txt -> stop_times.txt -> stops.txt, recursing into nested zips
(VIC ships one inner zip per mode). Downloads cache to /tmp/gtfs_<state>.zip.

Output columns: stop_id, stop_name, lat, lng, mode (bus|tram), state
Usage: .venv/bin/python scripts/build_bus_stops.py [--states nsw,vic]
"""
import argparse
import csv
import io
import sys
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

SOURCES = {
    "vic": "http://data.ptv.vic.gov.au/downloads/gtfs.zip",
    "nsw": "https://opendata.transport.nsw.gov.au/data/dataset/d1f68d4f-b778-44df-9823-cf2fa922e47f/resource/67974f14-01bf-47b7-bfa5-c7f2f8a950ca/download/full_greater_sydney_gtfs_static_0.zip",
    "qld": "https://gtfsrt.api.translink.com.au/GTFS/SEQ_GTFS.zip",
    "sa": "https://gtfs.adelaidemetro.com.au/v1/static/latest/google_transit.zip",
    "wa": "http://www.transperth.wa.gov.au/TimetablePDFs/GoogleTransit/Production/google_transit.zip",
    "tas": "https://www.transport.tas.gov.au/__data/assets/file/0011/557615/tas_gtfs.zip",
    "act": "https://www.transport.act.gov.au/googletransit/google_transit.zip",
}


def _dict_reader(f) -> csv.DictReader:
    """DictReader with stripped header names (Transperth ships ' stop_id')."""
    rdr = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
    rdr.fieldnames = [fn.strip() for fn in rdr.fieldnames or []]
    return rdr


def _mode_for(route_type: int) -> str | None:
    if route_type == 3 or 700 <= route_type <= 799:
        return "bus"
    if route_type in (0, 900):
        return "tram"
    return None


def _extract(zf: zipfile.ZipFile, state: str) -> list[dict]:
    """Bus/tram stops from one self-consistent GTFS (routes+trips+stop_times+stops)."""
    names = zf.namelist()

    def _open(suffix):
        return [n for n in names if n.endswith(suffix)]

    route_mode: dict[str, str] = {}
    for rf in _open("routes.txt"):
        with zf.open(rf) as f:
            for row in _dict_reader(f):
                try:
                    mode = _mode_for(int(row.get("route_type", -1)))
                except (ValueError, TypeError):
                    continue
                if mode:
                    route_mode[row["route_id"]] = mode
    if not route_mode:
        return []

    trip_mode: dict[str, str] = {}
    for tf in _open("trips.txt"):
        with zf.open(tf) as f:
            for row in _dict_reader(f):
                m = route_mode.get(row.get("route_id"))
                if m:
                    # tram beats bus for the label when a trip set mixes modes
                    prev = trip_mode.get(row["trip_id"])
                    trip_mode[row["trip_id"]] = "tram" if "tram" in (m, prev) else m

    stop_mode: dict[str, str] = {}
    for stf in _open("stop_times.txt"):
        with zf.open(stf) as f:
            for row in _dict_reader(f):
                m = trip_mode.get(row.get("trip_id"))
                if m:
                    sid = row.get("stop_id")
                    prev = stop_mode.get(sid)
                    stop_mode[sid] = "tram" if "tram" in (m, prev) else m
    if not stop_mode:
        return []

    out = []
    for sf in _open("stops.txt"):
        with zf.open(sf) as f:
            for row in _dict_reader(f):
                sid = row.get("stop_id", "").strip()
                mode = stop_mode.get(sid)
                if not mode:
                    continue
                if row.get("location_type", "0") not in ("", "0"):
                    continue
                try:
                    lat, lng = float(row.get("stop_lat", 0)), float(row.get("stop_lon", 0))
                except (ValueError, TypeError):
                    continue
                if lat == 0 or lng == 0:
                    continue
                out.append({
                    "stop_id": f"{state}:{sid}",
                    "stop_name": row.get("stop_name", "").strip(),
                    "lat": lat, "lng": lng, "mode": mode, "state": state,
                })
    return out


def _collect(zf: zipfile.ZipFile, state: str) -> list[dict]:
    """Extract from this zip, then recurse into nested zips (VIC per-mode)."""
    out = _extract(zf, state)
    for iz in [n for n in zf.namelist() if n.endswith(".zip")]:
        try:
            with zf.open(iz) as f:
                with zipfile.ZipFile(io.BytesIO(f.read())) as izf:
                    out.extend(_collect(izf, state))
        except Exception as e:
            print(f"  skip inner zip {iz}: {e}", file=sys.stderr)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--states", default=",".join(SOURCES))
    args = ap.parse_args()
    states = [s.strip() for s in args.states.split(",") if s.strip()]

    out_path = Path(__file__).resolve().parents[1] / "data" / "au_bus_stops.parquet"
    all_stops: list[dict] = []

    for state in states:
        url = SOURCES[state]
        cache = Path(f"/tmp/gtfs_{state}.zip")
        try:
            if not cache.exists() or cache.stat().st_size < 1000:
                print(f"Downloading {state}...", file=sys.stderr)
                req = Request(url, headers={"User-Agent": "property-scores/1.0"})
                cache.write_bytes(urlopen(req, timeout=600).read())
            print(f"Processing {state} ({cache.stat().st_size/1e6:.0f} MB)...", file=sys.stderr)
            with zipfile.ZipFile(cache) as zf:
                stops = _collect(zf, state)
            print(f"  {state}: {len(stops)} bus/tram stops", file=sys.stderr)
            all_stops.extend(stops)
        except Exception as e:
            print(f"  {state} FAILED: {e}", file=sys.stderr)

    # Dedup by rounded coordinate; tram label wins at shared stops
    best: dict[tuple, dict] = {}
    for s in all_stops:
        key = (round(s["lat"], 5), round(s["lng"], 5))
        if key not in best or (s["mode"] == "tram" and best[key]["mode"] == "bus"):
            best[key] = s
    deduped = list(best.values())
    print(f"Total: {len(all_stops)} raw, {len(deduped)} deduped", file=sys.stderr)
    if not deduped:
        sys.exit("no stops extracted, refusing to write empty parquet")

    import pandas as pd
    pd.DataFrame(deduped).to_parquet(out_path, index=False)
    by_state = pd.DataFrame(deduped).groupby(["state", "mode"]).size()
    print(by_state, file=sys.stderr)
    print(f"Saved {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
