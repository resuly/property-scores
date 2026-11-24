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


def _drop_source(manifest_path, name):
    m = json.loads(manifest_path.read_text())
    m["sources"].pop(name)
    m["build_id"] = "test_build_dropped"
    manifest_path.write_text(json.dumps(m, indent=2))
    # A new file, so the reader's inode/mtime stamp moves as a rebuild's would.
    os.utime(manifest_path, (1, 1))


def test_missing_bushfire_shard_is_no_answer_not_outside(fake_shards_library):
    """A rebuild that drops a state's bushfire shard must hand the point to
    the remote endpoints (None), not score the whole state as outside the
    official zone (worst None, which bushfire scoring reads as a clean miss
    for a 'full' trust state)."""
    assert lo.check_bushfire("qld", *INSIDE)["worst"] == "moderate"
    _drop_source(fake_shards_library, "qld_hazard_bushfire")
    assert lo.check_bushfire("qld", *INSIDE) is None
    assert lo.check_bushfire("qld", *OUTSIDE) is None
    # The flood layer of the same state still answers.
    assert lo.check("qld", *INSIDE)["worst"] == "flood"


def test_present_bushfire_shard_still_vouches_a_clean_miss(fake_shards_library):
    outside = lo.check_bushfire("qld", *OUTSIDE)
    assert outside is not None
    assert outside["worst"] is None and outside["hit_zones"] == []


def test_a_states_bushfire_needs_its_own_shard_not_another_states(fake_shards_library):
    # NSW is a 'full' trust state; only the QLD shard is in this manifest.
    assert lo.check_bushfire("nsw", -33.87, 151.21) is None


def test_missing_flood_shard_is_no_answer_not_outside(fake_shards_library):
    _drop_source(fake_shards_library, "qld_hazard_flood_brisbane_fam")
    assert lo.check("qld", *INSIDE) is None
    assert lo.check_bushfire("qld", *INSIDE)["worst"] == "moderate"


def test_every_required_bushfire_source_is_one_the_classifier_scores():
    for st, source in lo._BUSHFIRE_SOURCE.items():
        assert lo._classify_bushfire(source, {}) is not None, (st, source)
    assert set(lo._BUSHFIRE_SOURCE) == set(lo.BUSHFIRE_TRUST)


def test_a_non_scoring_bushfire_shard_does_not_stand_in(fake_shards_library):
    """Another bushfire layer of the state (display-only, not the statewide
    source the classifier scores) cannot vouch a miss on its own."""
    extra = fake_shards_library.parent / "shards" / "qld_hazard_bushfire_council.duckdb"
    c = duckdb.connect(str(extra))
    c.execute("LOAD spatial")
    c.execute("CREATE TABLE features (category VARCHAR, source VARCHAR, state VARCHAR, "
              "props JSON, geom GEOMETRY)")
    c.close()
    m = json.loads(fake_shards_library.read_text())
    m["sources"]["qld_hazard_bushfire_council"] = {
        "category": "bushfire", "state": "qld",
        "file": "shards/qld_hazard_bushfire_council.duckdb",
        "rows": 0, "bytes": extra.stat().st_size, "build_id": "b3"}
    fake_shards_library.write_text(json.dumps(m, indent=2))
    _drop_source(fake_shards_library, "qld_hazard_bushfire")
    assert lo.check_bushfire("qld", *INSIDE) is None


def test_another_states_flood_shard_does_not_answer_for_this_state(fake_shards_library):
    # VIC is a 'full' flood trust state; only a QLD flood shard is present.
    assert lo.check("vic", -37.81, 144.96) is None
