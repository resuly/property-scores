"""Tests for local_overlays using sharded feature databases via ShardReader."""
import concurrent.futures
import json
import os
import pytest

duckdb = pytest.importorskip("duckdb")

from property_scores.flood import local_overlays as lo

INSIDE = (-27.53941362, 153.00563112)
OUTSIDE = (-28.0, 153.0)


@pytest.fixture
def fake_shards_library(tmp_path, monkeypatch):
    """Create a temporary sharded layout: manifest.json + shards/<source>.duckdb."""
    shards_dir = tmp_path / "shards"
    shards_dir.mkdir()

    # 1. Flood shard
    flood_db = shards_dir / "qld_hazard_flood_brisbane_fam.duckdb"
    c_flood = duckdb.connect(str(flood_db))
    c_flood.execute("LOAD spatial")
    c_flood.execute(
        "CREATE TABLE features (category VARCHAR, source VARCHAR, state VARCHAR, props JSON, geom GEOMETRY)"
    )
    props_flood = json.dumps({"category": "Maximum extent of 1% AEP"})
    c_flood.execute(
        "INSERT INTO features VALUES ('flood', 'qld_hazard_flood_brisbane_fam', 'qld', ?, "
        "ST_GeomFromText('POLYGON((152.9 -27.6, 153.1 -27.6, 153.1 -27.4, 152.9 -27.4, 152.9 -27.6))'))",
        [props_flood],
    )
    c_flood.close()

    # 2. Bushfire shard
    fire_db = shards_dir / "qld_hazard_bushfire.duckdb"
    c_fire = duckdb.connect(str(fire_db))
    c_fire.execute("LOAD spatial")
    c_fire.execute(
        "CREATE TABLE features (category VARCHAR, source VARCHAR, state VARCHAR, props JSON, geom GEOMETRY)"
    )
    props_fire = json.dumps({"desc": "High Bushfire Hazard Area"})
    c_fire.execute(
        "INSERT INTO features VALUES ('bushfire', 'qld_hazard_bushfire', 'qld', ?, "
        "ST_GeomFromText('POLYGON((152.9 -27.6, 153.1 -27.6, 153.1 -27.4, 152.9 -27.4, 152.9 -27.6))'))",
        [props_fire],
    )
    c_fire.close()

    manifest_path = tmp_path / "manifest.json"
    manifest = {
        "build_id": "test_build_1",
        "generated_at": "2026-09-19T00:00:00Z",
        "sources": {
            "qld_hazard_flood_brisbane_fam": {
                "category": "flood",
                "state": "qld",
                "file": "shards/qld_hazard_flood_brisbane_fam.duckdb",
                "rows": 1,
                "bytes": flood_db.stat().st_size,
                "build_id": "b1",
            },
            "qld_hazard_bushfire": {
                "category": "bushfire",
                "state": "qld",
                "file": "shards/qld_hazard_bushfire.duckdb",
                "rows": 1,
                "bytes": fire_db.stat().st_size,
                "build_id": "b2",
            },
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2))

    monkeypatch.setattr(lo, "FEATURES_MANIFEST", str(manifest_path))
    monkeypatch.setattr(lo, "_reader", None)

    yield manifest_path

    if lo._reader is not None:
        lo._reader.close()
        lo._reader = None


def test_sharded_flood_lookup(fake_shards_library):
    result = lo.check("qld", *INSIDE)
    assert result is not None
    assert "Maximum extent of 1% AEP" in result["hit_zones"]
    assert result["worst"] == "flood"

    outside = lo.check("qld", *OUTSIDE)
    assert outside is not None
    assert outside["worst"] is None
    assert outside["hit_zones"] == []


def test_sharded_bushfire_lookup(fake_shards_library):
    result = lo.check_bushfire("qld", *INSIDE)
    assert result is not None
    assert result["worst"] == "moderate"
    assert "High Bushfire Hazard Area" in result["hit_zones"]


def test_sharded_concurrency(fake_shards_library):
    def mixed(i):
        if i % 2:
            r = lo.check("qld", *INSIDE)
            return "flood_hit" if (r and r["hit_zones"]) else "MISSED"
        r = lo.check_bushfire("qld", *INSIDE)
        return "fire_hit" if (r and r["hit_zones"]) else "MISSED"

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(mixed, range(32)))

    assert results.count("flood_hit") == 16
    assert results.count("fire_hit") == 16
