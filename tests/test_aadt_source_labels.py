"""Traffic-source provenance: which authority published a measured AADT row, and
which state the counter physically sits in.

Two defects found 2026-08-05 while working a customer's defect list:

1. noise/score.py stamped ``"source": "vicroads"`` on every row aadt_near()
   returned, but that function globs data/aadt_*.parquet across five states. A
   Transport for NSW counter on the Pacific Highway shipped as VicRoads data.
   Worse than cosmetic: scripts/export_noise_grid_csv.py picks its CC-BY
   attribution block off this exact label, so published extracts credited
   Victoria for four other states' data.

2. The NFDH national counter file records the REPORTING AGENCY's jurisdiction
   in its ``state`` column, not the counter's location: all 15 NFDH rows inside
   the ACT (Majura Parkway / Federal Hwy, clientid nswwim/nswrms) say NSW. Any
   state we publish for a source must therefore be computed from geometry.

These are pure-function tests: the mapping and the geographic lookup are the
parts that were wrong, and neither needs the 4 GB of parquets to exercise.
"""
from pathlib import Path

import pytest

from property_scores.common import overture
from property_scores.noise import score as ns


# --- 1. filename -> publishing authority ------------------------------------

@pytest.mark.parametrize("filename,expected", [
    ("aadt_vic.parquet", "vicroads"),
    ("aadt_nsw.parquet", "tfnsw"),
    ("aadt_qld.parquet", "qld_tmr"),
    ("aadt_sa.parquet", "sa_dit"),
    ("aadt_wa.parquet", "mrwa"),
    # Full paths are what DuckDB's filename column actually returns.
    ("/var/www/property-scores/data/aadt_nsw.parquet", "tfnsw"),
])
def test_source_label_follows_the_file_not_victoria(filename, expected):
    assert overture.aadt_source_for_file(filename) == expected


def test_every_state_maps_to_a_distinct_publisher():
    """The bug was five states collapsing onto one label. Guard the collapse."""
    labels = list(overture.AADT_SOURCE_BY_STATE.values())
    assert len(labels) == len(set(labels)), f"duplicate publisher labels: {labels}"


def test_unregistered_state_is_marked_not_guessed():
    """A new state's parquet must not inherit somebody else's credit line.

    The label it gets is deliberately absent from export_noise_grid_csv.py's
    _AADT_LICENSOR, so the export raises instead of shipping a wrong licensor.
    """
    label = overture.aadt_source_for_file("aadt_nt.parquet")
    assert label == "aadt_nt"
    assert label not in overture.AADT_SOURCE_BY_STATE.values()


def _load_export_module():
    """Import the export script by path (scripts/ is not a package)."""
    import importlib.util

    path = (Path(__file__).resolve().parent.parent
            / "scripts" / "export_noise_grid_csv.py")
    spec = importlib.util.spec_from_file_location("_export_noise_grid_csv", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_registered_labels_all_have_a_licensor_block():
    """Every publisher we can emit must be creditable, or exports break at ship time.

    Compares the actual dicts. The earlier version substring-matched the script's
    SOURCE TEXT for '"vicroads"', which a 2026-08-05 review defeated by deleting
    the whole VicRoads licensor entry: the string still occurred in two comments,
    so the test stayed green while any VIC grid would have crashed at ship time.
    """
    export = _load_export_module()
    emittable = set(overture.AADT_SOURCE_BY_STATE.values()) | {"nfdh"}
    missing = emittable - set(export._AADT_LICENSOR)
    assert not missing, (
        f"{sorted(missing)} can be emitted as a source but have no attribution "
        "block in export_noise_grid_csv.py's _AADT_LICENSOR")
    # And the set the export trusts must be exactly the set it can credit.
    assert set(export._MEASURED_AADT_SOURCES) == set(export._AADT_LICENSOR)


def test_export_does_not_treat_overture_as_a_traffic_publisher():
    """dominant_road.source is 'overture' whenever no counter is in range.

    Feeding that into the credit block made every counter-free grid refuse to
    ship (2026-08-05 review, blocking). Overture is credited under its own ODbL
    block, so it must not be in the measured-AADT publisher set.
    """
    export = _load_export_module()
    assert "overture" not in export._MEASURED_AADT_SOURCES
    assert "overture" not in export._AADT_LICENSOR


def test_export_credits_publishers_that_were_not_the_loudest():
    """Credit follows USE, not just the dominant source.

    dominant_road is only the loudest source. A measured counter can feed the
    exported level without being loudest: on a Brisbane CBD sample 8 of 9 points
    had a measured counter contributing while 7 of those had
    dominant_road.source == "overture". Crediting from dominant_road alone
    published "no measured traffic-counter dataset covered this extract" over
    grids that were in fact using CC-BY counter data (review, 2026-08-05).
    """
    export = _load_export_module()
    row = {"dominant_road": {"source": "overture"},
           "measured_traffic_sources": ["vicroads", "nfdh"]}
    assert export._measured_publishers(row) == {"vicroads", "nfdh"}, (
        "publishers that fed the level must be credited even when Overture was loudest")


def test_export_collection_step_ignores_overture_as_a_publisher():
    """Overture is a modelled network, credited under ODbL, not a traffic publisher."""
    export = _load_export_module()
    # Overture never appears in measured_traffic_sources, and a grid made only of
    # Overture rows must still ship.
    facts = {"volume_sources": set(), "name_sources": set()}
    for _ in range(5):
        facts["volume_sources"] |= export._measured_publishers(
            {"dominant_road": {"source": "overture"}, "measured_traffic_sources": []})
    used, credit = export._aadt_attribution(facts)
    assert used == set() and "Overture" in credit
    # And if it did leak in, it is dropped rather than treated as unregistered.
    used2, _ = export._aadt_attribution(
        {"volume_sources": {"overture"}, "name_sources": set()})
    assert used2 == set()


def test_export_refuses_an_unregistered_publisher_reaching_it_from_the_collector():
    """The guard must be reachable from the real collection path.

    An earlier version filtered to known publishers inside _measured_publishers,
    so an unregistered one was silently dropped and the RuntimeError below could
    never fire (review, 2026-08-05).
    """
    export = _load_export_module()
    facts = {"volume_sources": set(), "name_sources": set()}
    facts["volume_sources"] |= export._measured_publishers(
        {"dominant_road": {"source": "aadt_nt", "road_name": "Stuart Highway"},
         "measured_traffic_sources": ["aadt_nt"]})
    assert facts["volume_sources"] == {"aadt_nt"}, "collector must pass it through"
    with pytest.raises(RuntimeError, match="attribution block"):
        export._aadt_attribution(facts)


def test_export_has_exactly_one_licensor_table():
    """A duplicate copy inside write_docs would drift from the one that gates."""
    export = _load_export_module()
    src = (Path(__file__).resolve().parent.parent
           / "scripts" / "export_noise_grid_csv.py").read_text(encoding="utf-8")
    assert src.count("_AADT_LICENSOR = {") == 1, (
        "more than one licensor table: the copy that is not used by "
        "_aadt_attribution can silently go stale")
    assert set(export._MEASURED_AADT_SOURCES) == set(export._AADT_LICENSOR)


def test_vintage_gate_is_a_named_function_used_before_writing():
    """It used to raise inside write_docs, after the CSV was already on disk."""
    export = _load_export_module()
    with pytest.raises(RuntimeError, match="no input vintage recorded"):
        export._require_vintage("NSW")
    assert export._require_vintage("VIC")


def test_export_credits_every_publisher_that_drove_a_row():
    export = _load_export_module()
    used, credit = export._aadt_attribution(
        {"volume_sources": {"tfnsw", "vicroads"}, "name_sources": {"mrwa"}})
    assert used == {"tfnsw", "vicroads", "mrwa"}
    assert "Transport for NSW" in credit
    assert "Main Roads Western Australia" in credit
    assert "VicRoads" in credit


# --- 2. source_state comes from geometry, never an upstream column -----------

# Real coordinates of the NFDH counters this defect was found on.
_MAJURA_PKWY_ACT = (-35.21389999999997, 149.1880000000001)   # NFDH says state='NSW'
_PACIFIC_HWY_NSW = (-33.75317, 151.151077)                   # the Foundit defect row


def test_act_counter_is_not_reported_as_nsw():
    """The exact defect: an ACT counter whose upstream row is labelled NSW."""
    lat, lng = _MAJURA_PKWY_ACT
    assert ns._source_state(lat, lng) == "ACT"


def test_nsw_counter_still_reads_nsw():
    lat, lng = _PACIFIC_HWY_NSW
    assert ns._source_state(lat, lng) == "NSW"


def test_source_state_is_none_outside_australia_not_a_guess():
    assert ns._source_state(51.5, -0.12) is None      # London
    assert ns._source_state(None, None) is None


def test_source_state_ignores_any_upstream_state_column():
    """_source_state takes coordinates only.

    If it ever grows a parameter that lets a caller pass an upstream `state`
    through, the NFDH ACT rows go back to reporting NSW. Pin the signature.
    """
    import inspect

    params = list(inspect.signature(ns._source_state).parameters)
    assert params == ["src_lat", "src_lng"], (
        f"_source_state must derive state from geometry alone, got params {params}")


# --- 3. integration: the label must survive the real scoring path ------------
#
# The two blocking defects of 2026-08-05 (a 6-tuple unpack left in score.py, and
# a hardcoded "vicroads" that the pure-function tests above cannot see) both got
# through a green suite. These tests drive the real code with a stub DB, so a
# tuple-shape change or a re-hardcoded label fails here without needing the 4 GB
# of production parquets.

class _StubDB:
    """Minimal stand-in for the DuckDB handle noise_score threads through."""


# Shape of aircraft_noise_penalty() with no ANEF zone at the point.
_NO_AIRCRAFT = {"penalty_db": 0.0, "zone_code": None, "anef_min": None,
                "anef_max": None, "impact": None, "airport_type": None, "lga": None}


def _stub_aadt_rows():
    # (aadt, hv_pct, road_name, dist_m, near_lng, near_lat, source)
    return [
        (41461, 0.05, "Pacific Highway", 158.0, 151.151077, -33.75317, "tfnsw"),
        (12000, 0.04, "Bank Street", 240.0, 151.09034, -33.81764, "tfnsw"),
    ]


def test_score_publishes_the_source_from_the_row_not_a_constant(monkeypatch):
    """Re-hardcoding "vicroads" in score.py must fail a test."""
    rows = _stub_aadt_rows()
    monkeypatch.setattr(ns, "aadt_near", lambda *a, **k: rows)
    monkeypatch.setattr(ns, "nfdh_near", lambda *a, **k: [])
    monkeypatch.setattr(ns, "roads_near", lambda *a, **k: [])
    monkeypatch.setattr(ns, "rail_near", lambda *a, **k: [])
    monkeypatch.setattr(ns, "gtfs_rail_near", lambda *a, **k: [])
    monkeypatch.setattr(ns, "get_db", lambda *a, **k: _StubDB())
    monkeypatch.setattr(ns, "buildings_in_radius", lambda *a, **k: [])
    monkeypatch.setattr(ns, "buildings_to_arrays", lambda *a, **k: None)
    monkeypatch.setattr(ns, "barrier_attenuation", lambda *a, **k: 0.0)
    monkeypatch.setattr(ns, "aircraft_noise_penalty", lambda *a, **k: _NO_AIRCRAFT)
    monkeypatch.setattr(ns, "terrain_attenuation", lambda *a, **k: 0.0)
    monkeypatch.setattr(ns, "_cache_get", lambda *a, **k: None)
    monkeypatch.setattr(ns, "_cache_put", lambda *a, **k: None)

    result = ns.noise_score(-33.75457, 151.15077, 500)

    dom = result.get("dominant_road") or {}
    assert dom.get("source") == "tfnsw", (
        "the published source must come from the row, not a constant; "
        f"got {dom.get('source')!r}")
    assert dom.get("source_state") == "NSW"


def test_score_survives_the_full_aadt_tuple(monkeypatch):
    """Guards the 6-vs-7 tuple unpack that crashed every measured point.

    score.py destructures aadt_near rows in three separate places. Two were
    updated when the tuple grew and one (measured_distances) was not, so every
    location with a counter raised ValueError while the suite stayed green.
    """
    rows = _stub_aadt_rows()
    monkeypatch.setattr(ns, "aadt_near", lambda *a, **k: rows)
    monkeypatch.setattr(ns, "nfdh_near", lambda *a, **k: [])
    # A real Overture road, so the measured_distances dedup path actually runs.
    monkeypatch.setattr(ns, "roads_near",
                        lambda *a, **k: [("primary", 160.0, 60, 151.1511, -33.7532)])
    monkeypatch.setattr(ns, "rail_near", lambda *a, **k: [])
    monkeypatch.setattr(ns, "gtfs_rail_near", lambda *a, **k: [])
    monkeypatch.setattr(ns, "get_db", lambda *a, **k: _StubDB())
    monkeypatch.setattr(ns, "buildings_in_radius", lambda *a, **k: [])
    monkeypatch.setattr(ns, "buildings_to_arrays", lambda *a, **k: None)
    monkeypatch.setattr(ns, "barrier_attenuation", lambda *a, **k: 0.0)
    monkeypatch.setattr(ns, "aircraft_noise_penalty", lambda *a, **k: _NO_AIRCRAFT)
    monkeypatch.setattr(ns, "terrain_attenuation", lambda *a, **k: 0.0)
    monkeypatch.setattr(ns, "_cache_get", lambda *a, **k: None)
    monkeypatch.setattr(ns, "_cache_put", lambda *a, **k: None)

    result = ns.noise_score(-33.75457, 151.15077, 500)

    assert result.get("score") is not None
    assert result["aadt_segments"] == 2


def test_every_road_source_carries_source_state(monkeypatch):
    """source_state must be on Overture rows too, not only measured ones."""
    monkeypatch.setattr(ns, "aadt_near", lambda *a, **k: [])
    monkeypatch.setattr(ns, "nfdh_near", lambda *a, **k: [])
    monkeypatch.setattr(ns, "roads_near",
                        lambda *a, **k: [("primary", 60.0, 60, 144.9950, -37.8180)])
    monkeypatch.setattr(ns, "rail_near", lambda *a, **k: [])
    monkeypatch.setattr(ns, "gtfs_rail_near", lambda *a, **k: [])
    monkeypatch.setattr(ns, "get_db", lambda *a, **k: _StubDB())
    monkeypatch.setattr(ns, "buildings_in_radius", lambda *a, **k: [])
    monkeypatch.setattr(ns, "buildings_to_arrays", lambda *a, **k: None)
    monkeypatch.setattr(ns, "barrier_attenuation", lambda *a, **k: 0.0)
    monkeypatch.setattr(ns, "aircraft_noise_penalty", lambda *a, **k: _NO_AIRCRAFT)
    monkeypatch.setattr(ns, "terrain_attenuation", lambda *a, **k: 0.0)
    monkeypatch.setattr(ns, "_cache_get", lambda *a, **k: None)
    monkeypatch.setattr(ns, "_cache_put", lambda *a, **k: None)

    result = ns.noise_score(-37.8180, 144.9950, 500)
    dom = result.get("dominant_road") or {}
    assert dom.get("source") == "overture"
    assert "source_state" in dom, "source_state must be present on every road source"
    assert dom["source_state"] == "VIC"
