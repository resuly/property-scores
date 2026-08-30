"""VIC Environmental Audit checks wired into the existing truth sentinel."""

import importlib.util
import sys
from pathlib import Path

import pytest

from property_scores.contamination.sources import vic_wfs

_SPEC = importlib.util.spec_from_file_location(
    "score_truth_probes_vic_audit",
    Path(__file__).resolve().parent.parent / "scripts" / "score_truth_probes.py",
)
probes = importlib.util.module_from_spec(_SPEC)
sys.modules["score_truth_probes_vic_audit"] = probes
_SPEC.loader.exec_module(probes)


def _snapshots(point_count=5434, polygon_count=2616, skew_s=0):
    base = 1_788_000_000.0
    return {
        vic_wfs.LAYER_ENV_AUDIT_POINT: {
            "layer": vic_wfs.LAYER_ENV_AUDIT_POINT,
            "publisher_count": point_count,
            "max_data_extracted_on": "2026-08-29T08:25:00Z",
            "timestamp": base,
            "schema_field_count": 13,
        },
        vic_wfs.LAYER_ENV_AUDIT_POLYGON: {
            "layer": vic_wfs.LAYER_ENV_AUDIT_POLYGON,
            "publisher_count": polygon_count,
            "max_data_extracted_on": "2026-08-29T08:25:00Z",
            "timestamp": base - skew_s,
            "schema_field_count": 13,
        },
    }


def test_vic_audit_probe_success_covers_transport_metrics_and_truth_anchors(
        monkeypatch):
    snapshots = _snapshots()
    monkeypatch.setattr(
        vic_wfs, "environmental_audit_layer_probe", snapshots.get)

    def audits_near(lat, lng, radius_m):
        if (lat, lng, radius_m) == (-37.7925, 144.9855, 250):
            return [{"reference_number": "0008005706"}]
        if (lat, lng, radius_m) == (-37.8003, 144.9633, 25):
            return []
        raise AssertionError("unexpected probe coordinate")

    monkeypatch.setattr(vic_wfs, "environmental_audits_near", audits_near)

    results = probes.run_contamination_source_probes("contamination")

    assert len(results) == 6
    assert {row["status"] for row in results} == {"PASS"}
    by_key = {row["key"]: row for row in results}
    assert "count=5434" in by_key["vic_audit_point_wfs"]["note"]
    assert "schema_fields=13" in by_key["vic_audit_polygon_wfs"]["note"]
    assert "0008005706" in by_key["vic_audit_fitzroy_0008005706"]["note"]
    assert by_key["vic_audit_carlton_25m_empty"]["note"] == "entries=0"
    assert probes.run_contamination_source_probes("bushfire") == []


def test_vic_audit_probe_fails_on_source_shape_low_count_skew_and_query_failure(
        monkeypatch):
    snapshots = _snapshots(point_count=4999, skew_s=86_401)
    monkeypatch.setattr(
        vic_wfs, "environmental_audit_layer_probe", snapshots.get)
    monkeypatch.setattr(
        vic_wfs, "environmental_audits_near", lambda *a, **k: None)

    results = probes.run_contamination_source_probes("contamination")
    by_key = {row["key"]: row for row in results}

    assert by_key["vic_audit_point_wfs"]["status"] == "PASS"
    assert by_key["vic_audit_polygon_wfs"]["status"] == "PASS"
    assert by_key["vic_audit_publisher_counts"]["status"] == "FAIL"
    assert by_key["vic_audit_point_polygon_skew"]["status"] == "FAIL"
    assert by_key["vic_audit_fitzroy_0008005706"]["status"] == "FAIL"
    assert by_key["vic_audit_carlton_25m_empty"]["status"] == "FAIL"


def test_vic_audit_probe_surfaces_layer_http_or_schema_failure(monkeypatch):
    snapshots = _snapshots()
    snapshots[vic_wfs.LAYER_ENV_AUDIT_POLYGON] = None
    monkeypatch.setattr(
        vic_wfs, "environmental_audit_layer_probe", snapshots.get)
    monkeypatch.setattr(
        vic_wfs, "environmental_audits_near", lambda *a, **k: [])

    by_key = {
        row["key"]: row
        for row in probes.run_contamination_source_probes("contamination")
    }

    assert by_key["vic_audit_polygon_wfs"]["status"] == "FAIL"
    assert "HTTP/error shape/schema/freshness" in (
        by_key["vic_audit_polygon_wfs"]["note"])
    assert by_key["vic_audit_publisher_counts"]["status"] == "FAIL"
    assert by_key["vic_audit_point_polygon_skew"]["status"] == "FAIL"


def test_source_only_no_alert_uses_existing_state_path_without_writing(
        monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(probes, "run_contamination_source_probes", lambda _domain: [{
        "domain": "contamination_source",
        "key": "synthetic_green",
        "status": "PASS",
        "note": "ok",
        "expected": "healthy",
    }])
    state = tmp_path / "truth-state.json"
    monkeypatch.setattr(probes, "STATE_FILE", state)
    monkeypatch.setattr(sys, "argv", [
        "score_truth_probes.py", "--domain", "contamination",
        "--source-only", "--no-alert",
    ])

    with pytest.raises(SystemExit) as exc:
        probes.main()

    assert exc.value.code == 0
    assert not state.exists(), "--no-alert source dry-run must not mutate cron state"
    output = capsys.readouterr()
    assert "synthetic_green" in output.out
    assert "state left untouched" in output.err
