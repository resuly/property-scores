"""降级组件导致的 FAIL 要复测一次，并在 note 里点名降级组件。

背景: 2026-10-27 07:02 AEDT vic_fitzroy_audit_headline 报 'Incomplete Check'。
原因是 landfill 信号（VIC VLR + 全国 GA WMF，共用 8s 预算）超时返回 error，
服务按设计 fail-closed。几分钟后同一点恢复为 'Mapped Context - Review'。
告警里看不出是 landfill，只能人工重建原因。

夹具是同一点在生产 /scores/contamination 的真实响应（2026-10-27 20:2x UTC
抓取，健康状态）；降级版本按 score.py 在 landfill 超时时的真实输出形状构造：
landfill.status=error、score=None、score_status=unavailable_incomplete_coverage、
label=Incomplete Check。
"""
import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "score_truth_probes_reprobe", ROOT / "scripts" / "score_truth_probes.py")
probes = importlib.util.module_from_spec(_SPEC)
sys.modules["score_truth_probes_reprobe"] = probes
_SPEC.loader.exec_module(probes)

HEALTHY = json.loads(
    (ROOT / "tests" / "fixtures" / "contamination_vic_fitzroy_20261027.json").read_text())
EXPECTED = "label Mapped Context / Review"


def _degraded(component="landfill"):
    d = copy.deepcopy(HEALTHY)
    d[component] = {**d[component], "status": "error", "score": None, "entries": []}
    d["score"] = None
    d["score_status"] = "unavailable_incomplete_coverage"
    d["label"] = "Incomplete Check"
    return d


@pytest.fixture
def anchors(tmp_path, monkeypatch):
    def write(rows):
        p = tmp_path / "contamination_anchors.csv"
        lines = ["lat,lng,expected,rationale,id,blocker,reminder_days"]
        for r in rows:
            lines.append(f"-37.7925,144.9855,{r['expected']},x,{r['id']},{r.get('blocker', '')},")
        p.write_text("\n".join(lines) + "\n")
        monkeypatch.setattr(probes, "ANCHOR_DIR", tmp_path)
    return write


@pytest.fixture
def slept(monkeypatch):
    calls = []
    monkeypatch.setattr(probes.time, "sleep", lambda s: calls.append(s))
    return calls


def _serve(monkeypatch, *payloads):
    seq = list(payloads)
    calls = []

    def fake_get(url, *a, **kw):
        calls.append(url)
        return copy.deepcopy(seq.pop(0))
    monkeypatch.setattr(probes, "_get", fake_get)
    return calls


def test_real_healthy_payload_has_no_degraded_components():
    assert HEALTHY["label"] == "Mapped Context - Review"
    assert probes.degraded_components(HEALTHY) == []


def test_industrial_error_is_named_once_despite_two_status_fields():
    d = copy.deepcopy(HEALTHY)
    d["industrial"]["industrial_status"] = "error"
    d["industrial_status"] = "error"
    assert probes.degraded_components(d) == ["industrial=error"]
    d2 = copy.deepcopy(HEALTHY)
    d2["epa_status"] = "error"
    assert probes.degraded_components(d2) == ["epa=error"]


def test_transient_landfill_stall_is_reprobed_and_passes(monkeypatch, anchors, slept):
    anchors([{"expected": EXPECTED, "id": "vic_fitzroy_audit_headline"}])
    calls = _serve(monkeypatch, _degraded(), HEALTHY)
    [r] = probes.run_anchors("http://x", "contamination")
    assert len(calls) == 2, "降级 FAIL 必须复测一次"
    assert probes.REPROBE_WAIT_S in slept, "复测前必须真的等待"
    assert r["status"] == "PASS"
    assert "landfill=error" in r["note"], "复测通过也要留下首测降级记录"


def test_persistent_outage_stays_red_and_names_the_component(monkeypatch, anchors, slept):
    anchors([{"expected": EXPECTED, "id": "vic_fitzroy_audit_headline"}])
    _serve(monkeypatch, _degraded(), _degraded())
    [r] = probes.run_anchors("http://x", "contamination")
    assert r["status"] == "FAIL", "持续降级仍是红灯，不能被复测吞掉"
    assert "Incomplete Check" in r["note"]
    assert "landfill=error" in r["note"]


def test_reprobe_unreachable_keeps_the_first_failure(monkeypatch, anchors, slept):
    anchors([{"expected": EXPECTED, "id": "vic_fitzroy_audit_headline"}])
    _serve(monkeypatch, _degraded(), None)
    [r] = probes.run_anchors("http://x", "contamination")
    assert r["status"] == "FAIL"
    assert "landfill=error" in r["note"] and "unreachable" in r["note"]


def test_a_real_truth_regression_is_not_reprobed(monkeypatch, anchors, slept):
    """组件全健康却给出错误标签 = 真回归，必须一次就红，不复测。"""
    wrong = copy.deepcopy(HEALTHY)
    wrong["label"] = "No Mapped Red Flag"
    anchors([{"expected": EXPECTED, "id": "vic_fitzroy_audit_headline"}])
    calls = _serve(monkeypatch, wrong, HEALTHY)
    [r] = probes.run_anchors("http://x", "contamination")
    assert len(calls) == 1
    assert r["status"] == "FAIL"


def test_blocked_anchor_is_not_reprobed(monkeypatch, anchors, slept):
    anchors([{"expected": EXPECTED, "id": "blocked_one", "blocker": "licence"}])
    calls = _serve(monkeypatch, _degraded(), HEALTHY)
    [r] = probes.run_anchors("http://x", "contamination")
    assert len(calls) == 1
    assert r["status"] == "BLOCKED"


def test_audit_error_reported_once_from_nested_and_top_level_status():
    d = copy.deepcopy(HEALTHY)
    d["environmental_audit"]["status"] = "error"
    d["environmental_audit_status"] = "error"
    assert probes.degraded_components(d) == ["environmental_audit=error"]
