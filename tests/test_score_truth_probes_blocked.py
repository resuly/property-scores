"""Anchors with a declared external blocker are BLOCKED, not FAIL.

Bo 2026-09-03: eight of nine red rows were waiting on licences or sources
that do not exist yet, and every new-failure alert re-listed them. They stay
tracked and still get their reminder digest, but they are not news: no error
alert, no exit 1.
"""
import csv
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "score_truth_probes_blocked",
    Path(__file__).resolve().parent.parent / "scripts" / "score_truth_probes.py")
probes = importlib.util.module_from_spec(_SPEC)
sys.modules["score_truth_probes_blocked"] = probes
_SPEC.loader.exec_module(probes)

DAY = 86400


def _anchor_dir(tmp_path, rows):
    d = tmp_path / "anchors"
    d.mkdir()
    with open(d / "flood_anchors.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["lat", "lng", "expected", "why",
                                          "reminder_days", "blocker"],
                           lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    return d


def test_run_anchors_marks_a_red_row_with_a_blocker_as_blocked(tmp_path, monkeypatch):
    rows = [
        {"lat": "-28.8131", "lng": "153.2735", "expected": "score<40",
         "why": "Lismore", "reminder_days": "30", "blocker": "licence pending"},
        {"lat": "-27.4650", "lng": "152.9990", "expected": "score<55",
         "why": "Rosalie no blocker", "reminder_days": "", "blocker": ""},
        {"lat": "-41.4225", "lng": "147.1347", "expected": "score<=70",
         "why": "passes anyway", "reminder_days": "30", "blocker": "x"},
    ]
    monkeypatch.setattr(probes, "ANCHOR_DIR", _anchor_dir(tmp_path, rows))
    monkeypatch.setattr(probes, "SLEEP_S", 0)
    monkeypatch.setattr(probes, "_get", lambda url, **kw: {"score": 60})
    results = probes.run_anchors("http://x", "flood")
    assert [r["status"] for r in results] == ["BLOCKED", "FAIL", "PASS"]
    assert results[0]["note"] == "score=60 expected <40"
    assert results[0]["blocker"] == "licence pending"


def test_unreachable_endpoint_is_still_a_failure_even_with_a_blocker(tmp_path, monkeypatch):
    rows = [{"lat": "-28.8131", "lng": "153.2735", "expected": "score<40",
             "why": "Lismore", "reminder_days": "30", "blocker": "licence pending"}]
    monkeypatch.setattr(probes, "ANCHOR_DIR", _anchor_dir(tmp_path, rows))
    monkeypatch.setattr(probes, "SLEEP_S", 0)
    monkeypatch.setattr(probes, "_get", lambda url, **kw: None)
    assert probes.run_anchors("http://x", "flood")[0]["status"] == "FAIL"


@pytest.fixture
def harness(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    monkeypatch.setattr(probes, "STATE_FILE", state)
    monkeypatch.setattr(probes, "STALE_RED_DAYS", 7.0)
    monkeypatch.setattr(sys, "argv", ["probes"])
    sent = []
    fake = type(sys)("alert_telegram")
    fake.send_alert = lambda **kw: (sent.append(kw), True)[1]
    monkeypatch.setitem(sys.modules, "alert_telegram", fake)

    def run(results, now):
        monkeypatch.setattr(probes.time, "time", lambda: now)
        monkeypatch.setattr(probes, "run_canaries", lambda: [])
        monkeypatch.setattr(probes, "run_contamination_source_probes", lambda *_: [])
        monkeypatch.setattr(probes, "run_anchors", lambda *a, **k: list(results))
        sent.clear()
        try:
            probes.main()
        except SystemExit as e:
            code = e.code
        return code, list(sent), json.loads(state.read_text())
    return run


BLOCKED = {"domain": "flood", "key": "-28.8131,153.2735", "status": "BLOCKED",
           "note": "score=50 expected <40", "reminder_days": "30",
           "blocker": "licence pending"}
FAIL = {"domain": "flood", "key": "-27.4650,152.9990", "status": "FAIL",
        "note": "score=81 expected <55", "reminder_days": None, "blocker": ""}


def test_blocked_rows_do_not_alert_or_fail_the_run_but_are_tracked(harness):
    t0 = 1_700_000_000.0
    code, sent, state = harness([BLOCKED], t0)
    assert code == 0 and sent == []
    assert state["failing"] == ["flood|-28.8131,153.2735"]
    assert state["since"]["flood|-28.8131,153.2735"] == t0


def test_blocked_rows_still_get_their_reminder_digest(harness):
    t0 = 1_700_000_000.0
    harness([BLOCKED], t0)
    _code, sent, _ = harness([BLOCKED], t0 + 29 * DAY)
    assert sent == []
    code, sent, _ = harness([BLOCKED], t0 + 30 * DAY)
    assert code == 0 and len(sent) == 1
    assert sent[0]["level"] == "warn"
    assert "阻塞: licence pending" in sent[0]["message"]


def test_a_genuinely_new_failure_still_alerts_next_to_blocked_rows(harness):
    t0 = 1_700_000_000.0
    harness([BLOCKED], t0)
    code, sent, _ = harness([BLOCKED, FAIL], t0 + DAY)
    assert code == 1 and len(sent) == 1
    assert sent[0]["title"] == "真值哨兵: 1 项新失败"
    assert "-27.4650" in sent[0]["message"]
    assert "-28.8131" not in sent[0]["message"], "blocked rows are not re-listed"


def test_a_blocked_row_that_recovers_is_dropped_from_state(harness):
    t0 = 1_700_000_000.0
    harness([BLOCKED], t0)
    _code, _sent, state = harness([], t0 + DAY)
    assert state["failing"] == []
