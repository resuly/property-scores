"""Score model v2 (flood): CC BY council layers, VIC SBO, unverified-terrain guard.

The contract under test has two halves and both matter equally:

* v1 (the default, Foundit's pinned model) must reproduce production exactly.
  The fixtures carry the production /scores/flood score recorded at the same
  point on 2026-11-05, and v1 is asserted to equal it.
* v2 must move those same points for the documented reasons.

Fixtures are real bytes: props JSON and polygons copied verbatim from the
production feature shards, VicPlan's own /query response bodies, and the
production payload's recorded HAND / water / rainfall inputs. See each
fixture's `_provenance`.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")

from property_scores.common import model_version
from property_scores.flood import local_overlays as lo
from property_scores.flood import score as fs
from property_scores.flood import study_depth

FIX = Path(__file__).parent / "fixtures"
COUNCIL = json.loads((FIX / "flood_council_layers_20261105.json").read_text())["rows"]
SBO = json.loads((FIX / "vicplan_sbo_20261105.json").read_text())["cases"]


def _row(source, needle=None):
    for r in COUNCIL:
        if r["source"] == source and (needle is None or needle in r["props"]):
            return r
    raise KeyError((source, needle))


@pytest.fixture(autouse=True)
def _reset_sbo_breaker(monkeypatch):
    monkeypatch.setattr(lo, "_sbo_state", {"fails": 0, "open_until": 0.0})


@pytest.fixture
def version(monkeypatch):
    def _set(v):
        if v is None:
            monkeypatch.delenv(model_version.ENV_VAR, raising=False)
        else:
            monkeypatch.setenv(model_version.ENV_VAR, v)
    return _set


@pytest.fixture
def shard_library(tmp_path, monkeypatch):
    """A real sharded library built from the fixture rows (geometry + props)."""
    shards = tmp_path / "shards"
    shards.mkdir()
    sources = {}
    for r in COUNCIL:
        if not r.get("wkt"):
            continue
        path = shards / f"{r['source']}.duckdb"
        con = duckdb.connect(str(path))
        con.execute("LOAD spatial")
        con.execute("CREATE TABLE IF NOT EXISTS features (category VARCHAR, "
                    "source VARCHAR, state VARCHAR, props JSON, geom GEOMETRY)")
        con.execute("INSERT INTO features VALUES (?, ?, ?, ?, ST_GeomFromText(?))",
                    [r["category"], r["source"], r["state"], r["props"], r["wkt"]])
        con.close()
        sources[r["source"]] = {"category": "flood", "state": r["state"],
                                "file": f"shards/{r['source']}.duckdb", "rows": 1,
                                "bytes": path.stat().st_size, "build_id": r["source"]}
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"build_id": "fx", "generated_at": "2026-11-05",
                                    "sources": sources, "version": 1}))
    monkeypatch.setattr(lo, "FEATURES_MANIFEST", str(manifest))
    monkeypatch.setattr(lo, "_reader", None)
    yield manifest
    if lo._reader is not None:
        lo._reader.close()
    lo._reader = None


def _patch_inputs(monkeypatch, recorded):
    """Recorded production inputs at the point; the overlay is NOT patched."""
    monkeypatch.setattr(fs, "_water_proximity_local",
                        lambda lat, lng: recorded.get("water_proximity"))
    monkeypatch.setattr(fs, "_hand_local",
                        lambda lat, lng, state=None: recorded.get("hand"))
    monkeypatch.setattr(fs, "_query_ifd",
                        lambda lat, lng: recorded.get("design_rainfall"))
    monkeypatch.setattr(study_depth, "depth_at",
                        lambda lat, lng: recorded.get("flood_depth"))


# --- the version switch -----------------------------------------------------

def test_default_is_v1(version):
    version(None)
    assert model_version.current() == "v1"
    assert not model_version.at_least("v2")


def test_unknown_version_fails_closed(version):
    version("v3")
    with pytest.raises(model_version.UnknownModelVersion):
        model_version.current()
    version("v2")
    with pytest.raises(model_version.UnknownModelVersion):
        model_version.at_least("v9")


def test_stamp_separates_v1_and_v2(version):
    from property_scores.api import stamp
    version("v1")
    v1 = stamp.model_stamp()
    assert stamp.components()["score_model"] == "v1"
    version("v2")
    assert stamp.components()["score_model"] == "v2"
    assert stamp.model_stamp() != v1


# --- council layers: classification on real props -----------------------------

EXPECTED_V2 = [
    (("nsw_hazard_flood_ku_ring_gai", "Mainstream Flow"), "flood"),
    (("nsw_hazard_flood_ku_ring_gai", "Overland FPA"), "moderate"),
    (("nsw_hazard_flood_hawkesbury", None), "flood"),
    (("nsw_hazard_flood_campbelltown", None), "flood"),
    (("qld_hazard_flood_sunshine_coast", None), "moderate"),
]
DISPLAY_ONLY = ["nsw_hazard_flood_ses", "qld_hazard_flood_gold_coast",
                "qld_hazard_flood_logan_city", "qld_hazard_flood_redland_city",
                "nsw_hazard_flood_georges_river", "qld_hazard_flood_floodplain"]


@pytest.mark.parametrize("key,kind", EXPECTED_V2)
def test_v2_classifies_cleared_council_rows(version, key, kind):
    r = _row(*key)
    props = json.loads(r["props"])
    version("v1")
    assert lo._classify(r["source"], props) is None
    version("v2")
    got = lo._classify(r["source"], props)
    assert got is not None and got[0] == kind


@pytest.mark.parametrize("source", DISPLAY_ONLY)
def test_unverified_or_restricted_rows_stay_display_only(version, source):
    version("v2")
    assert lo._classify(source, json.loads(_row(source)["props"])) is None


def test_renamed_category_drops_out_instead_of_guessing(version):
    version("v2")
    props = json.loads(_row("nsw_hazard_flood_campbelltown")["props"])
    props["category"] = "PMF flood extent"
    assert lo._classify("nsw_hazard_flood_campbelltown", props) is None


@pytest.mark.parametrize("key,kind", EXPECTED_V2)
def test_check_over_real_shards(version, shard_library, key, kind):
    r = _row(*key)
    p = r["point"]
    version("v1")
    v1 = lo.check(r["state"], p["lat"], p["lng"])
    assert v1["worst"] is None and v1["hit_zones"] == []
    version("v2")
    v2 = lo.check(r["state"], p["lat"], p["lng"])
    assert v2["worst"] == kind and len(v2["hit_zones"]) == 1


def test_restricted_hornsby_rows_never_score(version, shard_library):
    r = _row("nsw_hazard_flood_ses")
    version("v2")
    res = lo.check("nsw", r["point"]["lat"], r["point"]["lng"])
    assert res["worst"] is None and res["hit_zones"] == []


# --- end to end: v1 reproduces production, v2 moves the number ---------------

E2E = [
    # (fixture key, expected v2 score, expected v2 label)
    (("nsw_hazard_flood_campbelltown", None), 40, "Moderate Risk"),
    (("nsw_hazard_flood_ku_ring_gai", "Overland FPA"), 60, "Moderate Risk"),
    (("nsw_hazard_flood_ku_ring_gai", "Mainstream Flow"), 35, "High Risk"),
]


@pytest.mark.parametrize("key,v2_score,v2_label", E2E)
def test_flood_score_v1_equals_production_v2_moves(
        version, shard_library, monkeypatch, key, v2_score, v2_label):
    r = _row(*key)
    _patch_inputs(monkeypatch, r["recorded_inputs"])
    p = r["point"]
    version(None)
    v1 = fs.flood_score(p["lat"], p["lng"])
    assert (v1["score"], v1["label"]) == (r["live_v1_score"], r["live_v1_label"])
    version("v2")
    v2 = fs.flood_score(p["lat"], p["lng"])
    assert (v2["score"], v2["label"]) == (v2_score, v2_label)
    assert v2["official_layer"] == "hit"


# --- VIC SBO ------------------------------------------------------------------

class _Resp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return json.loads(self._body)


@pytest.fixture
def vic_library(monkeypatch):
    """The real VIC library shape: LSIO/FO sources, no SBO source. Returns
    the recorded library hits for the point."""
    class Reader:
        rows: list = []

        def query(self, *a, **k):
            return self.rows

        def manifest(self):
            return {"sources": {"vic_hazard_flood": {}, "vic_hazard_flood_fo": {}}}
    reader = Reader()
    monkeypatch.setattr(lo, "_get_reader", lambda: reader)
    return reader


@pytest.mark.parametrize("case", SBO, ids=[c["truth"] for c in SBO])
def test_sbo_v1_matches_production(version, vic_library, monkeypatch, case):
    calls = []
    monkeypatch.setattr(fs.requests, "get",
                        lambda url, **k: calls.append(url) or _Resp(case["vicplan_sbo_count_response"]))
    _patch_inputs(monkeypatch, case["recorded_inputs"])
    version("v1")
    out = fs.flood_score(case["lat"], case["lng"])
    assert out["score"] == case["live_v1_score"]
    assert out["official_layer"] == case["live_v1_official_layer"]
    assert calls == []  # v1 never asks VicPlan


def test_sbo_v2_scores_moderate(version, vic_library, monkeypatch):
    case = next(c for c in SBO if c["truth"] == "in_sbo")
    monkeypatch.setattr(fs.requests, "get",
                        lambda url, **k: _Resp(case["vicplan_sbo_count_response"]))
    _patch_inputs(monkeypatch, case["recorded_inputs"])
    version("v2")
    out = fs.flood_score(case["lat"], case["lng"])
    assert out["official_layer"] == "hit"
    assert "SBO - Special Building Overlay" in out["flood_zones"]
    assert (out["score"], out["label"]) == (60, "Moderate Risk")


def test_sbo_v2_outside_keeps_clean_check(version, vic_library, monkeypatch):
    case = next(c for c in SBO if c["truth"] == "outside")
    monkeypatch.setattr(fs.requests, "get",
                        lambda url, **k: _Resp(case["vicplan_sbo_count_response"]))
    _patch_inputs(monkeypatch, case["recorded_inputs"])
    version("v2")
    out = fs.flood_score(case["lat"], case["lng"])
    assert out["official_layer"] == "checked_no_hit"
    assert out["score"] == case["live_v1_score"]


def test_sbo_v2_vicplan_down_is_not_a_clean_check(version, vic_library, monkeypatch):
    case = next(c for c in SBO if c["truth"] == "outside")
    import requests

    def boom(url, **k):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(fs.requests, "get", boom)
    version("v2")
    res = lo.check("vic", case["lat"], case["lng"])
    assert res["trust"] == "hit_only"
    assert res["warnings"] == [lo._SBO_WARNING]
    # and the warning reaches the payload, so da_leads does not pin it
    _patch_inputs(monkeypatch, case["recorded_inputs"])
    out = fs.flood_score(case["lat"], case["lng"])
    assert out["warnings"] == [lo._SBO_WARNING]
    assert out["official_layer"] == "checked_partial_coverage"


def test_sbo_call_uses_short_timeout(version, vic_library, monkeypatch):
    case = next(c for c in SBO if c["truth"] == "outside")
    seen = {}

    def get(url, **k):
        seen["timeout"] = k.get("timeout")
        return _Resp(case["vicplan_sbo_count_response"])
    monkeypatch.setattr(fs.requests, "get", get)
    version("v2")
    lo.check("vic", case["lat"], case["lng"])
    connect, read = seen["timeout"]
    assert connect <= 4 and read <= 4


def test_sbo_breaker_opens_after_repeated_failures(version, vic_library, monkeypatch):
    import requests
    calls = []

    def boom(url, **k):
        calls.append(url)
        raise requests.Timeout("slow")
    monkeypatch.setattr(fs.requests, "get", boom)
    version("v2")
    for _ in range(lo._SBO_FAILS_TO_OPEN):
        lo.check("vic", -37.98, 145.07)
    assert len(calls) == lo._SBO_FAILS_TO_OPEN
    res = lo.check("vic", -37.98, 145.07)          # breaker open: no call
    assert len(calls) == lo._SBO_FAILS_TO_OPEN
    assert res["trust"] == "hit_only" and res["warnings"] == [lo._SBO_SKIPPED]
    # after the window it tries again, and a success closes it
    monkeypatch.setattr(lo.time, "monotonic", lambda: 10**9)
    monkeypatch.setattr(fs.requests, "get", lambda url, **k: _Resp('{"count":0}'))
    res = lo.check("vic", -37.98, 145.07)
    assert res["trust"] == "full" and "warnings" not in res


def _library_rows(monkeypatch, rows, sources):
    class Reader:
        def query(self, *a, **k):
            return rows

        def manifest(self):
            return {"sources": {s: {} for s in sources}}
    monkeypatch.setattr(lo, "_get_reader", lambda: Reader())


LSIO_ROW = ("vic_hazard_flood",
            json.dumps({"category": "LSIO - Land Subject to Inundation Overlay"}))


@pytest.mark.parametrize("sbo_via", ["vicplan", "library"])
def test_lsio_plus_sbo_stacks_like_the_remote_path(version, monkeypatch, sbo_via):
    """LSIO keeps the worst severity ('flood', not SBO's 'moderate') and SBO
    adds one zone: 40 - 3 = 37 High, as the pre-07-03 remote path scored it.
    v1 ignores SBO: LSIO alone, 40 Moderate."""
    if sbo_via == "vicplan":
        _library_rows(monkeypatch, [LSIO_ROW], ["vic_hazard_flood"])
        monkeypatch.setattr(fs.requests, "get", lambda url, **k: _Resp('{"count":1}'))
    else:
        _library_rows(monkeypatch, [LSIO_ROW, ("vic_hazard_flood_sbo", json.dumps(
            {"category": "SBO - Special Building Overlay"}))],
            ["vic_hazard_flood", "vic_hazard_flood_sbo"])
        monkeypatch.setattr(fs.requests, "get",
                            lambda *a, **k: pytest.fail("VicPlan must not be asked"))
    hand = {"hand_m": 6.0, "point_elev_m": 30.0, "drainage_elev_m": 24.0,
            "relief_m": 8.0, "uncertain": False, "source": "lidar_5m_local"}
    _patch_inputs(monkeypatch, {"hand": hand, "water_proximity": NO_WATER})
    version("v1")
    v1 = fs.flood_score(-37.98, 145.07)
    assert (v1["score"], v1["label"]) == (40, "Moderate Risk")
    version("v2")
    res = lo.check("vic", -37.98, 145.07)
    assert res["worst"] == "flood" and len(res["hit_zones"]) == 2
    v2 = fs.flood_score(-37.98, 145.07)
    assert (v2["score"], v2["label"]) == (37, "High Risk")


def test_sbo_after_fo_keeps_floodway(version, monkeypatch):
    _library_rows(monkeypatch, [("vic_hazard_flood_fo", json.dumps(
        {"category": "FO - Floodway Overlay"}))], ["vic_hazard_flood_fo"])
    monkeypatch.setattr(fs.requests, "get", lambda url, **k: _Resp('{"count":1}'))
    version("v2")
    res = lo.check("vic", -37.98, 145.07)
    assert res["worst"] == "floodway" and len(res["hit_zones"]) == 2


def test_sbo_source_in_library_point_outside_does_not_ask_vicplan(version, monkeypatch):
    _library_rows(monkeypatch, [], ["vic_hazard_flood", "vic_hazard_flood_sbo"])
    monkeypatch.setattr(fs.requests, "get",
                        lambda *a, **k: pytest.fail("VicPlan must not be asked"))
    version("v2")
    res = lo.check("vic", -37.98, 145.07)
    assert res == {"worst": None, "hit_zones": [], "trust": "full"}


def test_unknown_version_stops_startup(version):
    from property_scores.api import main
    version("v3")
    with pytest.raises(model_version.UnknownModelVersion):
        main._resolve_score_stamp()


def test_sbo_from_library_when_baked(version, monkeypatch):
    """Once da_leads bakes SBO, the library row scores and VicPlan is not asked."""
    class Reader:
        def query(self, *a, **k):
            return [("vic_hazard_flood_sbo",
                     json.dumps({"category": "SBO - Special Building Overlay"}))]

        def manifest(self):
            return {"sources": {"vic_hazard_flood_sbo": {}}}
    monkeypatch.setattr(lo, "_get_reader", lambda: Reader())
    monkeypatch.setattr(fs.requests, "get",
                        lambda *a, **k: pytest.fail("VicPlan must not be asked"))
    version("v2")
    res = lo.check("vic", -37.98, 145.07)
    assert res["worst"] == "moderate"


# --- unverified terrain guard ---------------------------------------------------

def _mo_inputs(monkeypatch, hand, water=None):
    monkeypatch.setattr(lo, "check", lambda st, lat, lng: {
        "worst": None, "hit_zones": [], "trust": "hit_only"})
    _patch_inputs(monkeypatch, {"hand": hand, "water_proximity": water})


NO_WATER = {"max_occurrence_pct": 0, "nearest_water_m": None, "wet_cells": 0,
            "flood_cells": 0, "total_cells": 121, "mean_occurrence_pct": 0}


@pytest.mark.parametrize("hand", [
    {"hand_m": 3.1, "point_elev_m": 115.7, "drainage_elev_m": 112.6,
     "relief_m": 4.2, "uncertain": True, "source": "dem_relief"},   # flat 30 m DEM
    {"hand_m": 1.2, "point_elev_m": 9.0, "drainage_elev_m": 7.8,
     "relief_m": 6.0, "uncertain": False, "source": "lidar_5m_local"},  # low HAND
    None,                                                              # no elevation
    {"hand_m": 20.0},                                  # proxy read, no source
], ids=["uncertain", "low_hand", "no_hand", "proxy_no_source"])
def test_v2_unverified_terrain_is_never_low(version, monkeypatch, hand):
    _mo_inputs(monkeypatch, hand, NO_WATER)
    version("v1")
    v1 = fs.flood_score(-36.38, 145.40)   # Shepparton, NSW-like (no overlay)
    assert v1["score"] >= 70
    version("v2")
    v2 = fs.flood_score(-36.38, 145.40)
    assert v2["score"] == 65 and v2["label"] == "Moderate Risk"
    assert v2["terrain_check"] == "unverified"


def test_v2_verified_high_ground_still_very_low(version, monkeypatch):
    hand = {"hand_m": 25.0, "point_elev_m": 140.0, "drainage_elev_m": 115.0,
            "relief_m": 30.0, "uncertain": False, "source": "lidar_5m_local"}
    _mo_inputs(monkeypatch, hand, NO_WATER)
    version("v2")
    out = fs.flood_score(-33.75, 151.15)
    assert out["score"] >= 90 and "terrain_check" not in out


def test_v2_clean_statutory_check_is_not_capped(version, monkeypatch):
    hand = {"hand_m": 0.4, "point_elev_m": 113.0, "drainage_elev_m": 112.6,
            "relief_m": 1.6, "uncertain": True, "source": "dem_relief"}
    monkeypatch.setattr(lo, "check", lambda st, lat, lng: {
        "worst": None, "hit_zones": [], "trust": "full"})
    _patch_inputs(monkeypatch, {"hand": hand, "water_proximity": NO_WATER})
    version("v2")
    out = fs.flood_score(-36.38, 145.40)
    assert out["score"] == 90 and "terrain_check" not in out


def test_v2_overlay_hit_ceiling_blocks_hand_boost(version, monkeypatch):
    hand = {"hand_m": 25.0, "point_elev_m": 140.0, "drainage_elev_m": 115.0,
            "relief_m": 30.0, "uncertain": False, "source": "lidar_5m_local"}
    monkeypatch.setattr(lo, "check", lambda st, lat, lng: {
        "worst": "moderate", "hit_zones": ["x"], "trust": "hit_only"})
    _patch_inputs(monkeypatch, {"hand": hand, "water_proximity": NO_WATER})
    version("v1")
    assert fs.flood_score(-33.75, 151.15)["score"] == 70
    version("v2")
    assert fs.flood_score(-33.75, 151.15)["score"] == 60
