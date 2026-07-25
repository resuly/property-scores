"""The local overlay library must answer the same way under concurrent load.

Regression cover for the 2026-07-25 defect: `_get_conn` handed the same raw
DuckDB connection to every caller, and the /scores batch path drives flood and
bushfire through it in parallel. Under that load the query mostly returned an
EMPTY result set (occasionally an InternalException instead), so a flood-plain
address lost its official overlay hit and scored 60 "Moderate Risk" rather than
35 "High Risk" -- wrong in the reassuring direction, and cached for 90 days
downstream.

Measured against production before the fix, 100 Sherwood Road Rocklea: serial
calls hit the overlay every time; concurrency 4 lost it on 2 of 8, concurrency 8
on 4 of 16.
"""
import concurrent.futures
import json

import pytest

duckdb = pytest.importorskip("duckdb")

from property_scores.flood import local_overlays as lo


@pytest.fixture
def fake_library(tmp_path, monkeypatch):
    """A one-polygon features.duckdb the module will open read-only."""
    path = tmp_path / "features.duckdb"
    con = duckdb.connect(str(path))
    try:
        con.execute("INSTALL spatial")
    except Exception:
        pass
    con.execute("LOAD spatial")
    con.execute("CREATE TABLE features (state VARCHAR, category VARCHAR, "
                "source VARCHAR, props VARCHAR, geom GEOMETRY)")
    props = json.dumps({"category": "Maximum extent of 1% AEP"})
    con.execute(
        "INSERT INTO features VALUES ('qld', 'flood', 'qld_hazard_flood_brisbane_fam', ?, "
        "ST_GeomFromText('POLYGON((152.9 -27.6, 153.1 -27.6, "
        "153.1 -27.4, 152.9 -27.4, 152.9 -27.6))'))", [props])
    con.close()

    monkeypatch.setattr(lo, "FEATURES_DB", str(path))
    monkeypatch.setattr(lo, "_conn", None, raising=False)
    monkeypatch.setattr(lo, "_conn_ino", None, raising=False)
    yield path
    lo._conn = None
    lo._conn_ino = None


# Inside the polygon above; the same shape of point as Rocklea.
INSIDE = (-27.53941362, 153.00563112)


def test_serial_lookup_finds_the_overlay(fake_library):
    result = lo.check("qld", *INSIDE)
    assert result is not None, "library should answer for a covered state"
    assert result["hit_zones"], "point is inside the polygon"


def test_concurrent_lookups_all_find_the_overlay(fake_library):
    """The defect: some of these came back empty, so the caller scored the
    address as if no official overlay covered it."""
    def one(_):
        r = lo.check("qld", *INSIDE)
        if r is None:
            return "library_returned_none"
        return "hit" if r["hit_zones"] else "MISSED"

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(one, range(48)))

    bad = [r for r in results if r != "hit"]
    assert not bad, (
        f"{len(bad)} of {len(results)} concurrent lookups lost the overlay hit: "
        f"{sorted(set(bad))}")


def test_flood_and_bushfire_share_the_handle_safely(fake_library):
    """Both entry points run in parallel in the /scores batch path."""
    def mixed(i):
        if i % 2:
            r = lo.check("qld", *INSIDE)
            return "hit" if (r and r["hit_zones"]) else "MISSED"
        lo.check_bushfire("qld", *INSIDE)  # different category, must not blow up
        return "hit"

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(mixed, range(48)))

    assert all(r == "hit" for r in results), sorted(set(results))
