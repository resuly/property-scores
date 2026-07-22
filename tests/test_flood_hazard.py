"""reg-09: graded ARR flood-hazard class (H1..H6) classification + score surfacing.

The library-baked flood layer used to answer binary "in the 1% AEP zone". These
tests cover turning a council study's combined depth x velocity hazard class into
a graded severity + provenance, and cover that the score reflects the difference
between shallow nuisance (H1) and life-threatening floodway (H6) — the Skirving St
over-report class. Pure-function tests need no DB; the score test monkeypatches the
library + satellite/terrain signals so it isolates the reg-09 wiring.
"""
import pytest

from property_scores.flood import local_overlays as lo
from property_scores.flood import score as fs


# --- _hazard_class: normalise the encodings councils actually publish --------
@pytest.mark.parametrize("props,expected", [
    ({"hazard_class": "H3"}, "H3"),
    ({"hazard_class": "h6"}, "H6"),
    ({"severity": "5"}, "H5"),          # da_leads bake canonical key (Hazard->severity)
    ({"severity": 3}, "H3"),
    ({"gridcode": 1}, "H1"),
    ({"gridcode": "5"}, "H5"),
    ({"HAZARD": "High"}, "H5"),
    ({"class": "Low"}, "H1"),
    ({"category": "Medium hazard"}, "H3"),
    ({"category": "1% AEP flood extent"}, None),   # extent-only, no class
    ({"gridcode": 9}, None),                        # out of range
    ({}, None),
])
def test_hazard_class_normalisation(props, expected):
    assert lo._hazard_class(props) == expected


# --- _classify: hazard source -> graded severity kind ------------------------
def test_classify_hazard_source_grades_severity():
    # H1 shallow/slow must not read as hard as an H5 floodway
    kind1, label1 = lo._classify("nsw_newcastle_flood_hazard", {"hazard_class": "H1"})
    kind5, label5 = lo._classify("nsw_newcastle_flood_hazard", {"hazard_class": "H5"})
    assert kind1 == "moderate" and "H1" in label1
    assert kind5 == "floodway" and "H5" in label5
    # severity rank: floodway is worse (lower number) than moderate
    assert lo._SEVERITY_RANK[kind5] < lo._SEVERITY_RANK[kind1]


def test_classify_production_newcastle_source_id():
    kind, label = lo._classify(
        "nsw_hazard_flood_newcastle",
        {"severity": "6", "source": "nsw_flood_newcastle"},
    )
    assert kind == "floodway"
    assert label == lo._HAZARD_CLASS_DESC["H6"]


def test_classify_hazard_source_unclassifiable_does_not_score():
    # a hazard source whose value can't be parsed must not move the number
    assert lo._classify("qld_om_flood_hazard", {"category": "Development Constraints"}) is None


def test_classify_existing_extent_source_unchanged():
    # extent-only sources keep their exact prior behaviour (backward compat)
    assert lo._classify("act_hazard_flood", {}) == ("flood", "1% AEP Flood Extent (ACT)")
    assert lo._classify("nsw_hazard_flood", {"category": "Flood Planning Area"})[0] == "flood"


# --- score surfacing: H1 nuisance vs H6 floodway read differently ------------
def _isolate_signals(monkeypatch):
    """Neutralise satellite/terrain/rainfall so the test isolates the overlay+hazard path."""
    monkeypatch.setattr(fs, "_water_proximity_local", lambda *a, **k: None)
    monkeypatch.setattr(fs, "_hand_local", lambda *a, **k: None)
    monkeypatch.setattr(fs, "_query_ifd", lambda *a, **k: None)


def _fake_local(worst, hazard):
    return lambda state, lat, lng: {
        "worst": worst, "hit_zones": ["study hazard hit"],
        "trust": "hit_only", "hazard": hazard,
    }


def test_score_surfaces_hazard_with_provenance(monkeypatch):
    _isolate_signals(monkeypatch)
    monkeypatch.setattr(lo, "check", _fake_local("moderate", {
        "hazard_class": "H1", "description": lo._HAZARD_CLASS_DESC["H1"],
        "source": "Corowa/Howlong/Mulwala Flood Study", "aep": "1% AEP",
        "year": 2024, "licence": "CC BY 4.0",
    }))
    r = fs.flood_score(-35.99, 146.39)  # Corowa
    assert r["flood_hazard"]["class"] == "H1"
    assert "2024" in r["flood_hazard"]["provenance"]
    assert r["flood_hazard"]["licence"] == "CC BY 4.0"
    assert "H1" in r["flood_hazard_summary"]


def test_h1_scores_safer_than_h6(monkeypatch):
    _isolate_signals(monkeypatch)
    monkeypatch.setattr(lo, "check", _fake_local("moderate", {
        "hazard_class": "H1", "description": lo._HAZARD_CLASS_DESC["H1"], "aep": "1% AEP",
        "source": "s", "year": 2024, "licence": None,
    }))
    low = fs.flood_score(-35.99, 146.39)

    monkeypatch.setattr(lo, "check", _fake_local("floodway", {
        "hazard_class": "H6", "description": lo._HAZARD_CLASS_DESC["H6"], "aep": "1% AEP",
        "source": "s", "year": 2024, "licence": None,
    }))
    high = fs.flood_score(-35.99, 146.39)

    # the whole point of reg-09: shallow nuisance must score materially safer
    # than life-threatening floodway, not the same binary "in flood zone"
    assert low["score"] > high["score"]
    assert low["flood_hazard"]["class"] == "H1"
    assert high["flood_hazard"]["class"] == "H6"
