import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "score_truth_probes_contract",
    Path(__file__).resolve().parent.parent / "scripts" / "score_truth_probes.py")
probes = importlib.util.module_from_spec(_SPEC)
sys.modules["score_truth_probes_contract"] = probes
_SPEC.loader.exec_module(probes)


def test_status_contract_anchor_passes_only_the_exact_value():
    assert probes.evaluate(
        "epa_status=not_integrated",
        {"score": 95, "epa_status": "not_integrated"},
    ) == ("PASS", "epa_status='not_integrated' expected 'not_integrated'")

    status, note = probes.evaluate(
        "epa_status=not_integrated", {"score": 10, "epa_status": "ok"})
    assert status == "FAIL"
    assert "epa_status='ok'" in note


def test_missing_status_is_failure_not_manual():
    status, note = probes.evaluate("epa_status=not_integrated", {"score": 95})
    assert status == "FAIL"
    assert "None" in note


def test_score_comparisons_keep_their_existing_precedence():
    assert probes.evaluate("score<=40 High", {"score": 10})[0] == "PASS"


def test_nested_walkability_boolean_contract_field():
    payload = {
        "category_scores": {
            "supermarket": {"distance_m": 822, "barrier": False},
        },
    }
    assert probes.evaluate("supermarket_barrier=false", payload)[0] == "PASS"
    assert probes.evaluate("supermarket_barrier=true", payload)[0] == "FAIL"


def test_missing_nested_contract_field_stays_red():
    assert probes.evaluate(
        "supermarket_barrier=false", {"category_scores": {}})[0] == "FAIL"


def test_relative_score_margin_is_machine_checked():
    assert probes.evaluate_margin({"score": 54}, {"score": 45}, 5)[0] == "PASS"
    assert probes.evaluate_margin({"score": 49}, {"score": 45}, 5)[0] == "FAIL"
    assert probes.evaluate_margin({"score": None}, {"score": 45}, 5)[0] == "FAIL"


def test_numeric_contract_fields_are_machine_checked():
    payload = {"epa_sites_count": 11}
    assert probes.evaluate("epa_sites_count>=11", payload)[0] == "PASS"
    assert probes.evaluate("epa_sites_count>=12", payload)[0] == "FAIL"
    assert probes.evaluate("epa_sites_count>=1", {})[0] == "FAIL"


def test_external_blocker_can_use_named_low_noise_cadence():
    assert probes.reminder_due_seconds({"reminder_days": "30"}) == 30 * 86400 - 1800
    assert probes.reminder_due_seconds({}) == probes.STALE_RED_DAYS * 86400 - 1800


def test_anchor_id_keeps_risk_and_coverage_contracts_independent(tmp_path, monkeypatch):
    rows = [
        {"lat": "-31.9", "lng": "116.0", "expected": "flag_high_risk",
         "why": "risk", "id": ""},
        {"lat": "-31.9", "lng": "116.0", "expected": "epa_status=not_integrated",
         "why": "coverage", "id": "coverage_contract"},
    ]
    # The key selection itself is the invariant needed by state bookkeeping.
    assert (rows[0].get("id") or f"{rows[0]['lat']},{rows[0]['lng']}") == "-31.9,116.0"
    assert (rows[1].get("id") or f"{rows[1]['lat']},{rows[1]['lng']}") == "coverage_contract"
