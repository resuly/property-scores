#!/usr/bin/env python3
"""Build au_rail_stops.parquet from GTFS feeds (train + metro stops only).

Downloads GTFS zips from each state's transit authority and keeps a stop only
when routes.txt -> trips.txt -> stop_times.txt shows it is actually served by
a rail route_type. VIC ships a nested zip (one inner GTFS per mode); every
inner zip is resolved against its own routes/trips/stop_times, so a bus stop
named "... Railway Station" or a "Rail Replacement Bus Stop" no longer passes
on its name.

Trams are deliberately excluded: the walkability tram_bus scenario reads tram
stops from au_bus_stops.parquet (scripts/build_bus_stops.py, also route_type
based), while rail_stops_near() labels every row here "train_station". Keeping
tram here counted one tram stop as a train station too.

Output columns: stop_id, stop_name, lat, lng, route_type, mode, state
  route_type: the GTFS value of the rail route serving the stop (train wins
              over metro when both serve it), mode: train | metro
"""
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
}

def _dict_reader(f):
    """DictReader with stripped header names (Transperth ships ' stop_id')."""
    rdr = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
    rdr.fieldnames = [fn.strip() for fn in rdr.fieldnames or []]
    return rdr


def _mode_for(route_type: int) -> str | None:
    """GTFS route_type -> rail mode, or None for tram/bus/coach/ferry.

    Basic types 2 (rail) and 1 (subway/metro) plus the extended families
    100-117 (railway services) and 400-405 (urban railway / metro), which
    Sydney Metro uses (401). Tram (0, 900) and every bus type, including
    714 rail replacement bus, are not rail.
    """
    if route_type == 2 or 100 <= route_type <= 117:
        return "train"
    if route_type == 1 or 400 <= route_type <= 405:
        return "metro"
    return None


def _better(a, b):
    """Pick the (route_type, mode) to report when two rail modes serve a stop."""
    if b is None:
        return a
    return a if a[1] == "train" or b[1] != "train" else b


def _extract(zf: zipfile.ZipFile, state: str) -> list[dict]:
    """Rail stops from one self-consistent GTFS (routes+trips+stop_times+stops)."""
    names = zf.namelist()

    def _open(suffix):
        # only this zip's own members; nested zips are handled by _collect
        return [n for n in names if n.endswith(suffix)]

    route_rail: dict[str, tuple] = {}
    for rf in _open("routes.txt"):
        with zf.open(rf) as f:
            for row in _dict_reader(f):
                try:
                    rt = int(row.get("route_type", -1))
                except (ValueError, TypeError):
                    continue
                mode = _mode_for(rt)
                if mode:
                    route_rail[row["route_id"]] = (rt, mode)
    if not route_rail:
        return []

    trip_rail: dict[str, tuple] = {}
    for tf in _open("trips.txt"):
        with zf.open(tf) as f:
            for row in _dict_reader(f):
                r = route_rail.get(row.get("route_id"))
                if r:
                    trip_rail[row["trip_id"]] = r

    stop_rail: dict[str, tuple] = {}
    for stf in _open("stop_times.txt"):
        with zf.open(stf) as f:
            for row in _dict_reader(f):
                r = trip_rail.get(row.get("trip_id"))
                if r:
                    sid = row.get("stop_id", "").strip()
                    stop_rail[sid] = _better(r, stop_rail.get(sid))
    if not stop_rail:
        return []

    out = []
    for sf in _open("stops.txt"):
        with zf.open(sf) as f:
            for row in _dict_reader(f):
                sid = row.get("stop_id", "").strip()
                r = stop_rail.get(sid)
                if not r:
                    continue
                if row.get("location_type", "0") not in ("", "0", "1"):
                    continue
                try:
                    lat = float(row.get("stop_lat", 0))
                    lng = float(row.get("stop_lon", 0))
                except (ValueError, TypeError):
                    continue
                if lat == 0 or lng == 0:
                    continue
                out.append({
                    "stop_id": f"{state}:{sid}",
                    "stop_name": row.get("stop_name", "").strip(),
                    "lat": lat, "lng": lng,
                    "route_type": r[0], "mode": r[1], "state": state,
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


def _dedup(stops: list[dict]) -> list[dict]:
    """One row per rounded coordinate; a train label wins over metro."""
    best: dict[tuple, dict] = {}
    for s in stops:
        key = (round(s["lat"], 4), round(s["lng"], 4))
        prev = best.get(key)
        if prev is None or (s["mode"] == "train" and prev["mode"] != "train"):
            best[key] = s
    return list(best.values())


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
                rail = _collect(zf, state)
            # no name-based fallback: an empty result means the feed changed
            # shape and must be looked at, not papered over with "station"
            print(f"  {len(rail)} rail stops (GTFS route_type)", file=sys.stderr)
            all_stops.extend(rail)
        except Exception as e:
            print(f"  FAILED: {e}", file=sys.stderr)

    deduped = _dedup(all_stops)

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
        "route_type": [s["route_type"] for s in deduped],
        "mode": [s["mode"] for s in deduped],
        "state": [s["state"] for s in deduped],
    })
    db.execute(f"COPY tbl TO '{out_path}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    print(f"Written to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
