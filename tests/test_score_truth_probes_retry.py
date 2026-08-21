"""_get 的单次重试。

背景: "endpoint unreachable" 自 2026-06-13 起在 11 个不同日期触发过, 而这些
anchor 手工复测都在 0.2s 内正常返回。哨兵只对新失败告警, 所以一次瞬时超时
= 一条假告警。重试把瞬时抖动和真正的端点死亡分开。
"""
import importlib.util
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "score_truth_probes",
    Path(__file__).resolve().parent.parent / "scripts" / "score_truth_probes.py")
probes = importlib.util.module_from_spec(_SPEC)
sys.modules["score_truth_probes"] = probes
_SPEC.loader.exec_module(probes)


class _Resp:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def slept(monkeypatch):
    """记录 sleep 调用而不是真睡, 好让测试能断言退避确实发生。"""
    calls = []
    monkeypatch.setattr(probes.time, "sleep", lambda s: calls.append(s))
    return calls


def test_transient_failure_on_first_attempt_still_returns_the_payload(monkeypatch, slept):
    calls = {"n": 0}

    def flaky(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("timed out")
        return _Resp(b'{"score": 22}')

    monkeypatch.setattr(probes.urllib.request, "urlopen", flaky)
    assert probes._get("https://x/scores/bushfire", retry_wait=5.0) == {"score": 22}
    assert calls["n"] == 2, "第一次超时必须重试, 否则瞬时抖动变成假的新失败"
    assert slept == [5.0], "两次之间必须真的退避, 立刻重试打的多半是同一个抖动"


def test_a_genuinely_dead_endpoint_still_reports_unreachable(monkeypatch):
    calls = {"n": 0}

    def dead(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(probes.urllib.request, "urlopen", dead)
    assert probes._get("https://x/dead") is None
    assert calls["n"] == 2, "两次都失败才算真死, 但不能无限重试"


def test_a_healthy_endpoint_is_fetched_once(monkeypatch, slept):
    calls = {"n": 0}

    def ok(req, timeout=None):
        calls["n"] += 1
        return _Resp(b'{"count": 1}')

    monkeypatch.setattr(probes.urllib.request, "urlopen", ok)
    assert probes._get("https://x/ok") == {"count": 1}
    assert calls["n"] == 1, "正常端点不该被打两次, 151 个实探 anchor 会翻倍"
    assert slept == [], "正常路径一秒都不该等"


def test_headers_and_timeout_survive_the_retry(monkeypatch):
    seen = []

    def capture(req, timeout=None):
        seen.append((req.headers, timeout))
        if len(seen) == 1:
            raise TimeoutError("timed out")
        return _Resp(b'{}')

    monkeypatch.setattr(probes.urllib.request, "urlopen", capture)
    probes._get("https://x/y", timeout=30, headers={"X-Probe": "1"})
    assert len(seen) == 2
    for headers, timeout in seen:
        assert timeout == 30
        # urllib title-cases header keys.
        assert headers.get("X-probe") == "1", headers
