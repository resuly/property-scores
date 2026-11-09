"""build_rail_stops against real Transport for NSW GTFS bytes.

tests/fixtures/gtfs_nsw_20261109 is a verbatim slice of the full Greater Sydney
GTFS downloaded 2026-11-09: one trip each of a 700 bus, T1 (2), M1 Sydney
Metro (401), a 204 TrainLink coach, a 106 regional train and L2 light rail
(900), with exactly the stop_times and stops those trips use. The synthetic
rows in test_build_rail_stops.py pin the mapping; this pins it on the real
feed, where the production parquet built from the June feed still had no
Crows Nest or Victoria Cross.
"""
import csv
import importlib.util
import io
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "gtfs_nsw_20261109"

_SPEC = importlib.util.spec_from_file_location(
    "build_rail_stops_nsw", ROOT / "scripts" / "build_rail_stops.py")
brs = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(brs)


def _zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for p in sorted(FIXTURE.glob("*.txt")):
            zf.write(p, p.name)
    buf.seek(0)
    return zipfile.ZipFile(buf)


def _stop_ids(zf, route_prefix):
    trips = {r["trip_id"] for r in csv.DictReader(io.TextIOWrapper(
        zf.open("trips.txt"), encoding="utf-8-sig"))
        if r["route_id"].startswith(route_prefix)}
    return {r["stop_id"] for r in csv.DictReader(io.TextIOWrapper(
        zf.open("stop_times.txt"), encoding="utf-8-sig"))
        if r["trip_id"] in trips}


@pytest.fixture(scope="module")
def rail():
    return {s["stop_name"]: s for s in brs._collect(_zip(), "nsw")}


def test_every_metro_station_of_the_real_m1_trip_is_kept(rail):
    for name in ("Crows Nest Station, Platform 1",
                 "Victoria Cross Station, Platform 1",
                 "Tallawong Station, Platform 2",
                 "Bankstown Station, Platform 4"):
        assert (rail[name]["route_type"], rail[name]["mode"]) == (401, "metro"), name
    assert sum(1 for s in rail.values() if s["mode"] == "metro") == 31


def test_regional_106_and_sydney_trains_kept(rail):
    assert rail["Uralla Station, Platform 1"]["route_type"] == 106  # 106 only
    assert rail["Walcha Road Station, Platform 1"]["mode"] == "train"
    assert rail["Turramurra Station, Platform 1"]["route_type"] == 2


def test_light_rail_coach_and_bus_stops_are_not_rail(rail):
    zf = _zip()
    rail_only = (_stop_ids(zf, "2-T1-") | _stop_ids(zf, "3-M1-")
                 | _stop_ids(zf, "76-223-"))
    other = (_stop_ids(zf, "78-L2-") | _stop_ids(zf, "1-SC0-")
             | _stop_ids(zf, "76-135-")) - rail_only
    assert len(other) > 20
    kept = {s["stop_id"] for s in rail.values()}
    assert not ({f"nsw:{i}" for i in other} & kept)
    assert not any("Light Rail" in n for n in rail)
