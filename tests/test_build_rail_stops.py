"""Rail stop extraction keys on GTFS route_type, not on stop names.

The VIC fixture is real bytes cut from the PTV gtfs.zip downloaded 2026-10-20
(tests/fixtures/ptv_gtfs_20261020/{2,3,4}: metro train, tram, bus). Header
lines keep their UTF-8 BOM, rows keep PTV's quoting and CRLF endings; only
rows not needed for the cases below were dropped. The fixture is rezipped in
the real nested layout (outer zip with one N/google_transit.zip per mode).
"""

import importlib.util
import io
import zipfile
from pathlib import Path

PTV_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ptv_gtfs_20261020"


def _load():
    path = Path(__file__).resolve().parents[1] / "scripts" / "build_rail_stops.py"
    spec = importlib.util.spec_from_file_location("_build_rail_stops", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


brs = _load()

ROUTES_HDR = ('"route_id","agency_id","route_short_name","route_long_name",'
              '"route_type","route_color","route_text_color"')
TRIPS_HDR = '"route_id","service_id","trip_id","shape_id","trip_headsign","direction_id"'
ST_HDR = ('"trip_id","arrival_time","departure_time","stop_id","stop_sequence",'
          '"stop_headsign","pickup_type","drop_off_type","shape_dist_traveled"')
STOPS_HDR = '"stop_id","stop_name","stop_lat","stop_lon"'


def _gtfs(routes, trips, stop_times, stops) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, hdr, rows in (("routes.txt", ROUTES_HDR, routes),
                                ("trips.txt", TRIPS_HDR, trips),
                                ("stop_times.txt", ST_HDR, stop_times),
                                ("stops.txt", STOPS_HDR, stops)):
            z.writestr(name, ("﻿" + "\r\n".join([hdr, *rows]) + "\r\n").encode("utf-8"))
    return buf.getvalue()


def _ptv_nested() -> zipfile.ZipFile:
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as z:
        for mode_dir in sorted(PTV_FIXTURE.iterdir()):
            inner = io.BytesIO()
            with zipfile.ZipFile(inner, "w") as iz:
                for txt in sorted(mode_dir.glob("*.txt")):
                    iz.writestr(txt.name, txt.read_bytes())
            z.writestr(f"{mode_dir.name}/google_transit.zip", inner.getvalue())
    outer.seek(0)
    return zipfile.ZipFile(outer)


def test_fixture_is_raw_ptv_bytes():
    stops = (PTV_FIXTURE / "2" / "stops.txt").read_bytes()
    assert stops.startswith(b"\xef\xbb\xbfstop_id,")
    assert b'"Replacement bus"\r\n' in stops


def test_vic_nested_feed_keeps_only_rail_served_stops():
    with _ptv_nested() as zf:
        rows = brs._dedup(brs._collect(zf, "vic"))
    by_id = {r["stop_id"]: r for r in rows}
    # Box Hill platform (train route 400) survives. Dropped: the Box Hill rail
    # replacement bus stop, which PTV serves from route aus:vic:vic-02-LIL-R:
    # with route_type 400 just like the trains; the tram stop named
    # "Southern Cross Railway Station/..."; the bus stop "Box Hill Bus Station".
    assert set(by_id) == {"vic:12221"}
    box = by_id["vic:12221"]
    assert box["route_type"] == 400
    assert box["stop_name"] == "Box Hill Station"


def test_replacement_bus_flag_reads_platform_code_or_name():
    # each signal alone is enough; the 2026-10 PTV rows carry both
    assert brs._is_replacement_bus({"stop_name": "Box Hill", "platform_code": "Replacement bus"})
    assert brs._is_replacement_bus({"stop_name": "Box Hill Rail Replacement Bus Stop",
                                    "platform_code": ""})
    assert not brs._is_replacement_bus({"stop_name": "Box Hill Station", "platform_code": "1"})
    assert not brs._is_replacement_bus({"stop_name": "Box Hill Station"})


def test_flat_feed_reads_extended_metro_and_drops_light_rail():
    # NSW-style single feed: Sydney Metro uses extended type 401, light rail
    # 900. The old {0, 1, 2} set dropped the metro and kept the tram.
    data = _gtfs(
        routes=['"SMNW_M1","SMNW","M1","Tallawong to Sydenham","401","168388","FFFFFF"',
                '"IWLR-191","LR","L1","Dulwich Hill Line","900","BE1622","FFFFFF"'],
        trips=['"SMNW_M1","S1","M1.1","s1","Sydenham","0"',
               '"IWLR-191","S1","L1.1","s2","Central","0"'],
        stop_times=['"M1.1","05:00:00","05:00:00","2155384","1","","0","0","0"',
                    '"L1.1","05:00:00","05:00:00","2037511","1","","0","0","0"'],
        stops=['"2155384","Tallawong Station Platform 1","-33.6917","150.9064"',
               '"2037511","Lilyfield Light Rail","-33.8747","151.1653"'],
    )
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        rows = brs._collect(zf, "nsw")
    assert [(r["stop_id"], r["route_type"], r["mode"]) for r in rows] == [
        ("nsw:2155384", 401, "metro")]


def test_mode_mapping_excludes_tram_and_bus_types():
    assert brs._mode_for(2) == "train"
    assert brs._mode_for(1) == "metro"
    for rt in (0, 900, 3, 700, 714, 4, 5):
        assert brs._mode_for(rt) is None
