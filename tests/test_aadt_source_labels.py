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


def test_registered_labels_all_have_a_licensor_block():
    """Every publisher we can emit must be creditable, or exports break at ship time."""
    import importlib.util
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent
           / "scripts" / "export_noise_grid_csv.py").read_text(encoding="utf-8")
    for label in list(overture.AADT_SOURCE_BY_STATE.values()) + ["nfdh"]:
        assert f'"{label}"' in src, (
            f"{label!r} can be emitted as a source but has no attribution block "
            "in export_noise_grid_csv.py's _AADT_LICENSOR")
    assert importlib.util.find_spec is not None  # keep import meaningful


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
