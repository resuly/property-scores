"""2026-08-27 信号(历史用途/填埋/地下水)的打分集成测试。

网络全部 mock。信号数据层的解析测试在 tests/test_contam_sources.py;
这里只测打分语义: 组件合并/密度门控/fail-closed 标签降级/输出键。
"""

import pytest

from property_scores.contamination import score as cs

MELB = (-37.8136, 144.9631)

NEUTRAL = {"status": "not_integrated", "score": None, "entries": []}


@pytest.fixture(autouse=True)
def _stub_epa_and_industrial(monkeypatch):
    monkeypatch.setattr(cs, "_vic_epa_sites", lambda *a, **k: [])
    monkeypatch.setattr(cs, "get_db", lambda: object())
    monkeypatch.setattr(cs, "pois_near_detailed", lambda *a, **k: [])
    # 缓存只在每个测试开头清一次。曾经放在 _stub_signals() 里, 于是"换桩后
    # 断言没吃缓存"的测试每次换桩都顺手清了缓存, 永远绿(第2轮 review 抓的
    # 空转测试, 测试绿的理由是假的)。
    cs._contam_cache.clear()


def _stub_signals(monkeypatch, hist=None, lf=None, gw=None):
    monkeypatch.setattr(cs, "_historical_use_signal",
                        lambda *a, **k: hist or dict(NEUTRAL))
    monkeypatch.setattr(cs, "_landfill_signal",
                        lambda *a, **k: lf or dict(NEUTRAL))
    monkeypatch.setattr(cs, "_groundwater_signal",
                        lambda *a, **k: gw or dict(NEUTRAL))


def test_neutral_signals_leave_score_untouched(monkeypatch):
    _stub_signals(monkeypatch)
    r = cs.contamination_score(*MELB)
    assert r["score"] == 95
    assert r["historical_use"]["status"] == "not_integrated"


def test_historical_a_hit_caps_score(monkeypatch):
    _stub_signals(monkeypatch, hist={"status": "ok", "score": 50,
                                     "dense_precinct": False,
                                     "entries": [{"tier": "A"}]})
    r = cs.contamination_score(*MELB)
    assert r["score"] == 50
    # 信号档永不到登记册 on-site 的 10
    assert r["score"] > 10


def test_landfill_nearby_component(monkeypatch):
    _stub_signals(monkeypatch, lf={"status": "ok", "score": 70,
                                   "entries": [{"distance_m": 180}]})
    r = cs.contamination_score(*MELB)
    assert r["score"] == 70


def test_groundwater_zone_component_and_payload(monkeypatch):
    _stub_signals(monkeypatch, gw={"status": "ok", "score": 55,
                                   "entries": [{"inside": True,
                                                "source": "VIC EPA GQRUZ"}]})
    r = cs.contamination_score(*MELB)
    assert r["score"] == 55
    assert r["groundwater"]["entries"][0]["inside"] is True


def test_min_combination_across_signals(monkeypatch):
    _stub_signals(monkeypatch,
                  hist={"status": "ok", "score": 50, "entries": []},
                  lf={"status": "ok", "score": 70, "entries": []},
                  gw={"status": "ok", "score": 55, "entries": []})
    r = cs.contamination_score(*MELB)
    assert r["score"] == 50


def test_signal_error_blocks_reassuring_label(monkeypatch):
    # 历史用途查询挂了 + 其他一切干净: 分数可以是 95, 但不许自称 Very Clean
    _stub_signals(monkeypatch, hist={"status": "error", "score": None,
                                     "entries": []})
    r = cs.contamination_score(*MELB)
    assert r["score"] == 95
    assert r["label"] == cs.LABEL_INCOMPLETE


def test_signal_error_keeps_bad_bands(monkeypatch):
    # 失败只拦安慰性标签, 不拦坏消息(与 EPA outage 同语义)
    _stub_signals(monkeypatch,
                  hist={"status": "error", "score": None, "entries": []},
                  lf={"status": "ok", "score": 45, "entries": []})
    r = cs.contamination_score(*MELB)
    assert r["score"] == 45
    assert r["label"] == "Moderate Risk"


def test_result_carries_new_blocks(monkeypatch):
    _stub_signals(monkeypatch)
    r = cs.contamination_score(*MELB)
    for key in ("historical_use", "landfill", "groundwater"):
        assert key in r
        assert set(r[key]) >= {"status", "score", "entries"}


# ---- 信号构建函数自身(数据层 mock 在函数边界) ----

def test_historical_density_gate(monkeypatch):
    rows = ([{"business_type": "Service Stations", "directories": [1954],
              "distance_m": 5}]
            + [{"business_type": "Accountants", "directories": [1930],
                "distance_m": 10}] * 200)
    from property_scores.contamination.sources import vic_wfs
    monkeypatch.setattr(vic_wfs, "sands_near", lambda *a, **k: rows)
    sig = cs._historical_use_signal(*MELB, "VIC")
    assert sig["dense_precinct"] is True
    assert sig["score"] is None          # 密集街区不计分
    assert sig["entries"]                 # 但 evidence 保留


def test_historical_sparse_scores(monkeypatch):
    rows = [{"business_type": "Service Stations", "directories": [1932, 1954],
             "distance_m": 8},
            {"business_type": "Grocers - Retail", "directories": [1930],
             "distance_m": 12}]
    from property_scores.contamination.sources import vic_wfs
    monkeypatch.setattr(vic_wfs, "sands_near", lambda *a, **k: rows)
    sig = cs._historical_use_signal(*MELB, "VIC")
    assert sig["dense_precinct"] is False
    assert sig["score"] == 50
    assert sig["entries"][0]["first_year"] == 1932
    assert sig["entries"][0]["last_year"] == 1954


def test_historical_fail_closed(monkeypatch):
    from property_scores.contamination.sources import vic_wfs
    monkeypatch.setattr(vic_wfs, "sands_near", lambda *a, **k: None)
    sig = cs._historical_use_signal(*MELB, "VIC")
    assert sig["status"] == "error"
    assert sig["score"] is None


def test_historical_not_integrated_outside_vic():
    assert cs._historical_use_signal(-33.9, 151.2, "NSW")["status"] == "not_integrated"


def test_landfill_bands(monkeypatch):
    from property_scores.contamination.sources import ga_waste, vic_wfs
    monkeypatch.setattr(vic_wfs, "landfills_near", lambda *a, **k: [])
    for dist, expect in ((40, 45), (180, 70), (900, 85)):
        monkeypatch.setattr(ga_waste, "landfills_near",
                            lambda *a, _d=dist, **k: [{"name": "tip",
                                                       "distance_m": _d}])
        sig = cs._landfill_signal(*MELB, "VIC")
        assert sig["score"] == expect, dist


def test_landfill_partial_failure_is_not_silent(monkeypatch):
    # VIC 层挂了但 GA 层有数据: 结果可用但 status 必须暴露 partial
    from property_scores.contamination.sources import ga_waste, vic_wfs
    monkeypatch.setattr(vic_wfs, "landfills_near", lambda *a, **k: None)
    monkeypatch.setattr(ga_waste, "landfills_near",
                        lambda *a, **k: [{"name": "tip", "distance_m": 500}])
    sig = cs._landfill_signal(*MELB, "VIC")
    assert sig["status"] == "partial"
    assert sig["score"] == 85


def test_landfill_total_failure(monkeypatch):
    from property_scores.contamination.sources import ga_waste, vic_wfs
    monkeypatch.setattr(vic_wfs, "landfills_near", lambda *a, **k: None)
    monkeypatch.setattr(ga_waste, "landfills_near", lambda *a, **k: None)
    sig = cs._landfill_signal(*MELB, "VIC")
    assert sig["status"] == "error"
    assert sig["score"] is None


def test_groundwater_inside_scores_nearby_does_not(monkeypatch):
    from property_scores.contamination.sources import vic_wfs
    monkeypatch.setattr(vic_wfs, "gqruz_near",
                        lambda *a, **k: [{"inside": False, "distance_m": 300}])
    assert cs._groundwater_signal(*MELB, "VIC")["score"] is None
    monkeypatch.setattr(vic_wfs, "gqruz_near",
                        lambda *a, **k: [{"inside": True, "distance_m": 0}])
    assert cs._groundwater_signal(*MELB, "VIC")["score"] == 55


def test_groundwater_sa_route(monkeypatch):
    from property_scores.contamination.sources import sa_gpa
    monkeypatch.setattr(sa_gpa, "areas_near",
                        lambda *a, **k: [{"inside": True, "distance_m": 0,
                                          "site": "Edwardstown"}])
    sig = cs._groundwater_signal(-34.98, 138.57, "SA")
    assert sig["score"] == 55
    assert sig["entries"][0]["source"] == "SA EPA GPA"


# ---- 第1轮 review 修复的锚定(P0-1/P1-2/P1-3/P1-5) ----

def test_landfill_partial_blocks_reassuring_label(monkeypatch):
    # P0-1: VIC 缺 VLR(该州唯一的历史填埋源)时不许说 Very Clean
    _stub_signals(monkeypatch, lf={"status": "partial", "score": None,
                                   "entries": []})
    r = cs.contamination_score(*MELB)
    assert r["label"] == cs.LABEL_INCOMPLETE


def test_aux_failure_is_not_cached(monkeypatch):
    # P1-2: 信号 outage 不许被钉一小时(乐观分数方向)
    _stub_signals(monkeypatch, gw={"status": "error", "score": None,
                                   "entries": []})
    cs.contamination_score(*MELB)
    _stub_signals(monkeypatch, gw={"status": "ok", "score": 55,
                                   "entries": [{"inside": True}]})
    r = cs.contamination_score(*MELB)
    assert r.get("cached") is not True
    assert r["score"] == 55


def test_healthy_result_with_signals_is_cached(monkeypatch):
    # 正向对照: 健康结果确实进缓存。没有这条, 谁把缓存整个关掉这个
    # 文件也不会响(第2轮 review)。
    _stub_signals(monkeypatch, gw={"status": "ok", "score": 55,
                                   "entries": [{"inside": True}]})
    cs.contamination_score(*MELB)
    r = cs.contamination_score(*MELB)
    assert r.get("cached") is True


def test_dense_unattributed_a_blocks_reassuring_label(monkeypatch):
    # P1-3: "Very Clean 旁边挂着一个 5m 未归属加油站" 不许发生
    _stub_signals(monkeypatch, hist={"status": "ok", "score": None,
                                     "dense_precinct": True,
                                     "unattributed_a": True,
                                     "entries": [{"tier": "A",
                                                  "distance_m": 5}]})
    r = cs.contamination_score(*MELB)
    assert r["score"] == 95
    assert r["label"] == cs.LABEL_INCOMPLETE


def test_on_site_reflects_new_signals(monkeypatch):
    # P1-5: 45 分来自 7m 的 Sands 命中时, on_site 不许全 false
    _stub_signals(monkeypatch,
                  hist={"status": "ok", "score": 45, "dense_precinct": False,
                        "unattributed_a": False,
                        "entries": [{"tier": "A", "distance_m": 7}]},
                  gw={"status": "ok", "score": 55,
                      "entries": [{"inside": True}]})
    r = cs.contamination_score(*MELB)
    assert r["on_site"]["historical_use"] is True
    assert r["on_site"]["groundwater"] is True
    assert r["on_site"]["landfill"] is False
