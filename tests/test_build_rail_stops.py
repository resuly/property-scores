"""Rail stop extraction keys on GTFS route_type, not on stop names.

Fixture rows follow the real PTV GTFS layout: an outer gtfs.zip holding one
inner google_transit.zip per mode folder (2 = metro train, 3 = metro tram,
4 = metro bus), quoted CSV with a UTF-8 BOM. Stop ids, names and coordinates
are the real PTV rows already in au_rail_stops.parquet that the old
name heuristic let through.
"""

import importlib.util
import io
import zipfile
from pathlib import Path


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
    train = _gtfs(
        routes=['"2-BEL-mjp-1","1","Belgrave","Belgrave - City","2","094C8D","FFFFFF"',
                # rail replacement buses ship inside the TRAIN feed
                '"2-RRB-mjp-1","1","Replacement Bus","Arden - Parkville","714","FF8200","000000"'],
        trips=['"2-BEL-mjp-1","T0","1.T0.2-BEL-mjp-1.1.H","2-BEL-mjp-1.1.H","City","0"',
               '"2-RRB-mjp-1","T0","9.T0.2-RRB-mjp-1.1.H","2-RRB-mjp-1.1.H","Parkville","0"'],
        stop_times=['"1.T0.2-BEL-mjp-1.1.H","05:01:00","05:01:00","12206","1","","0","0","0"',
                    '"9.T0.2-RRB-mjp-1.1.H","06:10:00","06:10:00","26323","1","","0","0","0"'],
        stops=['"12206","East Camberwell Station","-37.82584107","145.06824844"',
               '"26323","Arden Railway Station Rail Replacement Bus Stop",'
               '"-37.80128591","144.9417746"'],
    )
    tram = _gtfs(
        routes=['"3-16-mjp-1","1","16","Melbourne University - Kew via St Kilda Beach",'
                '"0","78BE20","000000"'],
        trips=['"3-16-mjp-1","T0","3.T0.3-16-mjp-1.1.H","3-16-mjp-1.1.H","Kew","0"'],
        stop_times=['"3.T0.3-16-mjp-1.1.H","05:30:00","05:30:00","20526","1","","0","0","0"'],
        stops=['"20526","Malvern Railway Station/Glenferrie Rd #53",'
               '"-37.86526359","145.02777434"'],
    )
    bus = _gtfs(
        routes=['"4-903-mjp-1","4","903","Altona - Mordialloc (SMARTBUS Service)",'
                '"3","FF8200","FFFFFF"'],
        trips=['"4-903-mjp-1","T0","4.T0.4-903-mjp-1.1.H","4-903-mjp-1.1.H","Mordialloc","0"'],
        stop_times=['"4.T0.4-903-mjp-1.1.H","05:40:00","05:40:00","19648","1","","0","0","0"'],
        stops=['"19648","Box Hill Bus Station/Station St","-37.8198278","145.12214065"'],
    )
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as z:
        z.writestr("2/google_transit.zip", train)
        z.writestr("3/google_transit.zip", tram)
        z.writestr("4/google_transit.zip", bus)
    outer.seek(0)
    return zipfile.ZipFile(outer)


def test_vic_nested_feed_keeps_only_rail_served_stops():
    with _ptv_nested() as zf:
        rows = brs._dedup(brs._collect(zf, "vic"))
    by_id = {r["stop_id"]: r for r in rows}
    # only the real train station survives; the bus stop named "... Station",
    # the tram stop named "... Railway Station" and the rail replacement bus
    # stop inside the train feed are all out
    assert set(by_id) == {"vic:12206"}
    east = by_id["vic:12206"]
    assert east["route_type"] == 2
    assert east["mode"] == "train"
    assert east["stop_name"] == "East Camberwell Station"


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
