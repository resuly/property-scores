"""2026-08-27 信号(历史用途/填埋/地下水)的打分集成测试。

网络全部 mock。信号数据层的解析测试在 tests/test_contam_sources.py;
这里只测打分语义: 组件合并/密度门控/fail-closed 标签降级/输出键。
"""

import pytest
import requests as _requests

from property_scores.contamination import score as cs
from property_scores.contamination.sources import _common

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


def _stub_signals(monkeypatch, hist=None, audit=None, lf=None, gw=None):
    monkeypatch.setattr(cs, "_historical_use_signal",
                        lambda *a, **k: hist or dict(NEUTRAL))
    monkeypatch.setattr(cs, "_environmental_audit_signal",
                        lambda *a, **k: audit or dict(NEUTRAL))
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
    # 历史用途查询挂了 + 其他一切干净: 乐观分数不出站。
    _stub_signals(monkeypatch, hist={"status": "error", "score": None,
                                     "entries": []})
    r = cs.contamination_score(*MELB)
    assert r["score"] is None
    assert r["score_status"] == "unavailable_incomplete_coverage"
    assert r["label"] == cs.LABEL_INCOMPLETE


def test_signal_error_keeps_bad_bands(monkeypatch):
    # 失败只拦安慰性标签, 不拦坏消息(与 EPA outage 同语义)
    _stub_signals(monkeypatch,
                  hist={"status": "error", "score": None, "entries": []},
                  lf={"status": "ok", "score": 45, "entries": []})
    r = cs.contamination_score(*MELB)
    assert r["score"] == 45
    assert r["label"] == "Elevated Mapped Risk"


def test_result_carries_new_blocks(monkeypatch):
    _stub_signals(monkeypatch)
    r = cs.contamination_score(*MELB)
    for key in ("historical_use", "environmental_audit", "landfill", "groundwater"):
        assert key in r
        assert set(r[key]) >= {"status", "score", "entries"}


def test_environmental_audit_is_evidence_only_context_with_explicit_coverage(
        monkeypatch):
    from property_scores.contamination.sources import vic_wfs

    monkeypatch.setattr(vic_wfs, "environmental_audits_near", lambda *a, **k: [{
        "reference_number": "0008005706",
        "file_number": "75730-1",
        "address": "433 SMITH STREET, FITZROY NORTH VIC 3068 433 SMITH ST",
        "suburb": "Fitzroy North",
        "audit_category": "53X Statement",
        "date_completed": "2020-12-22T00:00:00Z",
        "report_available": True,
        "inside": False,
        "distance_m": 70,
        "geom": "polygon",
    }])

    signal = cs._environmental_audit_signal(-37.7925, 144.9855, "VIC")

    assert signal["status"] == "ok"
    assert signal["score"] is None
    assert signal["evidence_only"] is True
    assert signal["evidence_radius_m"] == 250
    assert signal["entries_total"] == signal["entries_returned"] == 1
    assert signal["coverage"] == "vic_epa_environmental_audit_locations"
    assert "does not by itself prove contamination" in signal["coverage_note"]
    assert "EPA Processing" in signal["coverage_note"]
    assert "not a transaction-safe snapshot" in signal["coverage_note"]
    assert "internal 72-hour" in signal["coverage_note"]
    assert "not an EPA or DataVic service-level promise" in signal["coverage_note"]
    assert signal["entries"][0]["source"] == "VIC EPA Environmental Audits"
    assert signal["entries"][0]["evidence_only"] is True


def test_environmental_audit_not_integrated_outside_victoria():
    signal = cs._environmental_audit_signal(-33.9, 151.2, "NSW")
    assert signal["status"] == "not_integrated"
    assert signal["entries"] == []
    assert signal["score"] is None


def test_environmental_audit_builder_propagates_freshness_failure(monkeypatch):
    from property_scores.contamination.sources import vic_wfs

    monkeypatch.setattr(vic_wfs, "environmental_audits_near", lambda *a, **k: None)
    signal = cs._environmental_audit_signal(*MELB, "VIC")

    assert signal["status"] == "error"
    assert signal["entries"] == []
    assert signal["score"] is None


def test_environmental_audit_failure_withholds_reassuring_score_and_cache(
        monkeypatch):
    _stub_signals(monkeypatch, audit={
        "status": "error", "score": None, "entries": [],
        "evidence_only": True,
        "coverage": "vic_epa_environmental_audit_locations",
        "coverage_note": "official layer unavailable",
    })

    result = cs.contamination_score(*MELB)

    assert result["environmental_audit"]["status"] == "error"
    assert result["score"] is None
    assert result["score_status"] == "unavailable_incomplete_coverage"
    assert result["label"] == cs.LABEL_INCOMPLETE
    assert "Environmental Audit location layers could not be reached" in result["note"]
    assert cs._contam_cache == {}


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


def test_historical_dense_same_parcel_can_score(monkeypatch):
    rows = ([{"business_type": "Service Stations", "directories": [1954],
              "distance_m": 5, "lat": MELB[0], "lng": MELB[1]}]
            + [{"business_type": "Accountants", "directories": [1930],
                "distance_m": 10, "lat": MELB[0], "lng": MELB[1]}] * 200)
    from property_scores.contamination import parcel_attribution
    from property_scores.contamination.sources import vic_wfs
    monkeypatch.setattr(vic_wfs, "sands_near", lambda *a, **k: rows)
    monkeypatch.setattr(parcel_attribution, "same_parcel_flags",
                        lambda *a, **k: [True])

    sig = cs._historical_use_signal(*MELB, "VIC")

    assert sig["dense_precinct"] is True
    assert sig["parcel_attributed"] is True
    assert sig["unattributed_a"] is False
    assert sig["score"] == 50
    assert sig["on_site"] is True


def test_historical_neighbour_parcel_is_not_on_site(monkeypatch):
    rows = [{"business_type": "Service Stations", "directories": [1954],
             "distance_m": 5, "lat": MELB[0], "lng": MELB[1]}]
    from property_scores.contamination import parcel_attribution
    from property_scores.contamination.sources import vic_wfs
    monkeypatch.setattr(vic_wfs, "sands_near", lambda *a, **k: rows)
    monkeypatch.setattr(parcel_attribution, "same_parcel_flags",
                        lambda *a, **k: [False])

    sig = cs._historical_use_signal(*MELB, "VIC")

    assert sig["parcel_attributed"] is True
    assert sig["score"] is None
    assert sig["on_site"] is False
    assert sig["entries"] == []


def test_historical_parcel_unavailable_keeps_density_fallback(monkeypatch):
    rows = ([{"business_type": "Service Stations", "directories": [1954],
              "distance_m": 5, "lat": MELB[0], "lng": MELB[1]}]
            + [{"business_type": "Accountants", "directories": [1930],
                "distance_m": 10, "lat": MELB[0], "lng": MELB[1]}] * 200)
    from property_scores.contamination import parcel_attribution
    from property_scores.contamination.sources import vic_wfs
    monkeypatch.setattr(vic_wfs, "sands_near", lambda *a, **k: rows)
    monkeypatch.setattr(parcel_attribution, "same_parcel_flags",
                        lambda *a, **k: None)

    sig = cs._historical_use_signal(*MELB, "VIC")

    assert sig["parcel_attributed"] is False
    assert sig["status"] == "partial"
    assert sig["score"] is None
    assert sig["unattributed_a"] is True


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


def test_sa_and_qld_licensed_activities_are_on_site_evidence_only(monkeypatch):
    from property_scores.contamination import parcel_attribution
    from property_scores.contamination.sources import qld_ea, sa_licensed
    monkeypatch.setattr(
        sa_licensed, "activities_near",
        lambda *a, **k: [{"licence_number": 20, "activity": "Hydrocarbon",
                           "lat": -34.98, "lng": 138.57}],
    )
    monkeypatch.setattr(parcel_attribution, "same_parcel_flags",
                        lambda *a, **k: None)
    sa = cs._historical_use_signal(-34.98, 138.57, "SA")
    assert sa["score"] is None
    assert sa["on_site"] is True
    assert sa["entries"][0]["evidence_only"] is True
    assert sa["parcel_attributed"] is False
    assert sa["status"] == "partial"
    assert "lat" not in sa["entries"][0] and "lng" not in sa["entries"][0]

    monkeypatch.setattr(
        qld_ea, "activities_at",
        lambda *a, **k: [{"permit_reference": "EA1", "activity": "Waste"}],
    )
    qld = cs._historical_use_signal(-27.5, 153.0, "QLD")
    assert qld["status"] == "ok"
    assert qld["score"] is None
    assert qld["on_site"] is True


def test_tas_epa_context_is_evidence_only(monkeypatch):
    from property_scores.contamination.sources import tas_epa
    monkeypatch.setattr(tas_epa, "regulated_sites_near", lambda *a, **k: [{
        "site_id": 6198,
        "premises_name": "Mountain Stream Fishery",
        "activity_category": "Food Production",
        "distance_m": 0,
    }])
    monkeypatch.setattr(tas_epa, "upss_near", lambda *a, **k: [{
        "site_id": 348,
        "source_kind": "EPA underground petroleum storage system",
        "status": "Active",
        "distance_m": 13,
    }])

    sig = cs._historical_use_signal(-42.8, 147.3, "TAS")

    assert sig["status"] == "ok"
    assert sig["score"] is None
    assert sig["on_site"] is False
    assert sig["evidence_radius_m"] == 500
    assert sig["representative_points_only"] is True
    assert {row["source"] for row in sig["entries"]} == {
        "TAS EPA Regulated Sites",
        "TAS EPA Underground Petroleum Storage Systems",
    }
    assert all(row["evidence_only"] is True for row in sig["entries"])
    assert "CLIENT_NAME" not in str(sig)


def test_tas_score_response_carries_exact_source_rights(monkeypatch):
    from property_scores.contamination.sources import tas_epa
    monkeypatch.setattr(tas_epa, "regulated_sites_near", lambda *a, **k: [])
    monkeypatch.setattr(tas_epa, "upss_near", lambda *a, **k: [{
        "site_id": "348", "status": "Active", "distance_m": 0,
        "source_kind": "EPA underground petroleum storage system",
    }])
    monkeypatch.setattr(cs, "_detect_state", lambda *a: "TAS")
    monkeypatch.setattr(cs, "_industrial_proximity", lambda *a: {
        "score": 95, "count_500m": 0, "nearest_m": None, "sites": [],
        "industrial_status": "ok",
    })
    monkeypatch.setattr(cs, "_landfill_signal", lambda *a: {
        "status": "not_integrated", "score": None, "entries": []})
    monkeypatch.setattr(cs, "_groundwater_signal", lambda *a: {
        "status": "not_integrated", "score": None, "entries": []})

    cs._contam_cache.clear()
    result = cs.contamination_score(-42.8122728, 147.3418094)

    assert result["attribution"] == [{
        "source": "TAS EPA Underground Petroleum Storage Systems",
        "attribution": (
            "EPA Underground Petroleum Storage Systems from theLIST "
            "© State of Tasmania"
        ),
        "licence": "CC BY 3.0 AU",
        "licence_url": "https://creativecommons.org/licenses/by/3.0/au/",
    }]


def test_tas_partial_context_blocks_reassuring_label(monkeypatch):
    from property_scores.contamination.sources import tas_epa
    monkeypatch.setattr(tas_epa, "regulated_sites_near", lambda *a, **k: [{
        "site_id": 6198,
        "premises_name": "Mountain Stream Fishery",
        "activity_category": "Food Production",
        "distance_m": 0,
    }])
    monkeypatch.setattr(tas_epa, "upss_near", lambda *a, **k: None)

    sig = cs._historical_use_signal(-42.8, 147.3, "TAS")

    assert sig["status"] == "partial"
    assert sig["entries"]
    _stub_signals(monkeypatch, hist=sig)
    monkeypatch.setattr(cs, "_detect_state", lambda *a: "TAS")
    result = cs.contamination_score(-42.8, 147.3)
    assert result["label"] == cs.LABEL_INCOMPLETE
    assert result.get("cached") is not True


def test_sa_licensed_same_parcel_filter_is_evidence_only(monkeypatch):
    from property_scores.contamination import parcel_attribution
    from property_scores.contamination.sources import sa_licensed
    rows = [
        {"licence_number": 20, "activity": "Hydrocarbon",
         "lat": -34.9800, "lng": 138.5700, "distance_m": 4},
        {"licence_number": 21, "activity": "Waste",
         "lat": -34.9801, "lng": 138.5701, "distance_m": 13},
    ]
    monkeypatch.setattr(sa_licensed, "activities_near",
                        lambda *a, **k: rows)
    seen = {}

    def same_parcel(*args, **kwargs):
        seen.update(kwargs)
        return [True, False]

    monkeypatch.setattr(parcel_attribution, "same_parcel_flags", same_parcel)

    sig = cs._historical_use_signal(-34.98, 138.57, "SA")

    assert sig["status"] == "ok"
    assert sig["score"] is None
    assert sig["parcel_attributed"] is True
    assert sig["on_site"] is True
    assert [row["licence_number"] for row in sig["entries"]] == [20]
    assert sig["entries"][0]["evidence_only"] is True
    assert 0 < seen["timeout_s"] <= cs._SIGNAL_BUDGET_S


def test_sa_licensed_neighbour_only_is_not_on_site(monkeypatch):
    from property_scores.contamination import parcel_attribution
    from property_scores.contamination.sources import sa_licensed
    monkeypatch.setattr(sa_licensed, "activities_near", lambda *a, **k: [{
        "licence_number": 21, "activity": "Waste",
        "lat": -34.9801, "lng": 138.5701, "distance_m": 13,
    }])
    monkeypatch.setattr(parcel_attribution, "same_parcel_flags",
                        lambda *a, **k: [False])

    sig = cs._historical_use_signal(-34.98, 138.57, "SA")

    assert sig["parcel_attributed"] is True
    assert sig["entries"] == []
    assert sig["on_site"] is False
    assert sig["score"] is None


def test_sa_licensed_malformed_parcel_result_uses_radius_fallback(monkeypatch):
    from property_scores.contamination import parcel_attribution
    from property_scores.contamination.sources import sa_licensed
    monkeypatch.setattr(sa_licensed, "activities_near", lambda *a, **k: [{
        "licence_number": 20, "activity": "Hydrocarbon",
        "lat": -34.98, "lng": 138.57, "distance_m": 4,
    }])
    monkeypatch.setattr(parcel_attribution, "same_parcel_flags",
                        lambda *a, **k: [])

    sig = cs._historical_use_signal(-34.98, 138.57, "SA")

    assert sig["parcel_attributed"] is False
    assert sig["status"] == "partial"
    assert [row["licence_number"] for row in sig["entries"]] == [20]


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


def test_groundwater_nsw_vulnerability_is_evidence_only(monkeypatch):
    from property_scores.contamination.sources import nsw_groundwater
    monkeypatch.setattr(
        nsw_groundwater, "vulnerability_at",
        lambda *a, **k: [{
            "inside": True,
            "layer_class": "Groundwater Vulnerable",
            "distance_m": 0,
        }],
    )
    sig = cs._groundwater_signal(-33.63, 148.32, "NSW")
    assert sig["status"] == "ok"
    assert sig["score"] is None
    assert sig["entries"][0]["inside"] is True
    assert sig["entries"][0]["source"] \
        == "NSW DPHI EPI Groundwater Vulnerability"


def test_evidence_only_context_blocks_very_clean_without_inventing_score(
        monkeypatch):
    _stub_signals(
        monkeypatch,
        gw={
            "status": "ok",
            "score": None,
            "entries": [{"inside": True, "layer_class": "Groundwater Vulnerable"}],
        },
    )
    monkeypatch.setattr(cs, "_detect_state", lambda *a: "NSW")
    monkeypatch.setattr(cs, "_nsw_epa_sites", lambda *a, **k: [])
    result = cs.contamination_score(-33.63, 148.32)
    assert result["score"] == 95
    assert result["label"] == cs.LABEL_MAPPED_CONTEXT


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
    assert r["score"] is None
    assert r["score_status"] == "unavailable_incomplete_coverage"
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


# ---------------------------------------------------------------------------
# 延迟预算 (2026-08-27 latency review)
#
# 背景: 三个 builder 串行发请求, 每个请求 1 次 + 1 次重试 x 10s timeout。
# _landfill_signal 最坏 60s, Sands 分页最坏更久, 而 /scores 的
# _BATCH_DEADLINE_S 是 25s。超预算必须 fail-closed 成 status="error"
# (拦安慰标签 + 不缓存), 而不是把 deadline 撞穿再留下 STRAGGLER 线程。
#
# 这些测试用假时钟推进, 不真的 sleep。
# ---------------------------------------------------------------------------


@pytest.fixture
def burning_clock(monkeypatch):
    """假时钟 + "每个请求烧掉自己整个 timeout 然后失败" 的假 socket。

    返回 (clock, calls)。unbudgeted 时每次请求烧 10s, 所以没有预算的实现
    会一路把 6 次请求全打完 (clock 走到 60), 有预算的实现必须早停。
    """
    clock = [1000.0]
    calls = []
    monkeypatch.setattr(_common, "_now", lambda: clock[0])
    monkeypatch.setattr(_common, "_RETRY_SLEEP_S", 0)

    def _get(url, params=None, timeout=None, headers=None):
        calls.append((params or {}).get("typeNames") or url)
        clock[0] += timeout
        raise _requests.ConnectionError("upstream hung")

    monkeypatch.setattr(_common.requests, "get", _get)
    return clock, calls


def test_landfill_signal_stops_at_its_budget(burning_clock):
    clock, calls = burning_clock
    start = clock[0]
    sig = cs._landfill_signal(*MELB, "VIC")
    assert sig["status"] == "error"
    assert sig["entries"] == []
    # 预算 8s: VLR 两层并发各拿一次 8s timeout, 回来预算已空, 不再重试也不
    # 再打 GA。没有预算检查时这里是 6 次请求 / 60s。
    assert len(calls) <= 2, f"budget did not stop the chain: {calls}"
    assert clock[0] - start <= 2 * cs._SIGNAL_BUDGET_S


def test_groundwater_signal_stops_at_its_budget(burning_clock):
    clock, calls = burning_clock
    sig = cs._groundwater_signal(*MELB, "VIC")
    assert sig["status"] == "error"
    # 一次 8s 请求就把预算烧完, 重试不许再发。无预算时是 2 次。
    assert len(calls) == 1, f"retried past the budget: {calls}"


def test_historical_signal_stops_paging_at_its_budget(monkeypatch):
    """Sands 分页最坏是几千次 GetFeature: 预算必须在循环里也生效。"""
    clock = [1000.0]
    calls = []
    monkeypatch.setattr(_common, "_now", lambda: clock[0])

    def _get(url, params=None, timeout=None, headers=None):
        calls.append(params)
        clock[0] += 1.0  # 每页 1s, 慢但不失败
        return _FakePage()

    monkeypatch.setattr(_common.requests, "get", _get)
    sig = cs._historical_use_signal(*MELB, "VIC")
    assert sig["status"] == "error"
    # 8s 预算 / 每页 1s = 最多 8 页。没有预算检查时 numberMatched=5000
    # 会让它一页一页拉到 5000。
    assert len(calls) <= int(cs._SIGNAL_BUDGET_S) + 1, f"{len(calls)} pages"


class _FakePage:
    """一页 Sands: 一条特征, 但 numberMatched 说还有 5000 条。"""
    status_code = 200
    ok = True

    def json(self):
        return {
            "type": "FeatureCollection",
            "numberMatched": 5000,
            "features": [{
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [144.9631, -37.8136]},
                "properties": {"business_type": "Grocers", "directory": 1900,
                               "adopted_street_name": "Collins",
                               "adopted_locality": "Melbourne", "vdpid": 1},
            }],
        }


def test_signals_are_unbudgeted_nowhere_else(monkeypatch):
    """预算是块作用域的: builder 返回后不许把 deadline 留在线程里, 否则
    同一 worker 上跑的下一个请求会莫名其妙被砍。
    delta review P3: 旧版走 NT 在进入 budget() 前就返回, 断言恒真什么都没证;
    现在走 VIC 真路径(gqruz mock 掉), budget 块真的开过再验证已关。"""
    from property_scores.contamination.sources import vic_wfs
    monkeypatch.setattr(vic_wfs, "gqruz_near", lambda *a, **k: [])
    assert _common.remaining_budget() is None
    sig = cs._groundwater_signal(*MELB, "VIC")
    assert sig["status"] == "ok"
    assert _common.remaining_budget() is None


def test_landfill_budget_discards_partial_results(monkeypatch):
    """delta review P1: VLR 已拿到记录、GA 随后撞破预算时, 必须丢弃部分结果
    返回 error, 不许带着"半读出的没有/有填埋场"落成 partial+score
    (Keele St 形态)。此前该刻意行为零测试覆盖, 变异保留部分结果 573 全绿。"""
    from property_scores.contamination.sources import ga_waste, vic_wfs
    monkeypatch.setattr(vic_wfs, "landfills_near",
                        lambda *a, **k: [{"name": "old tip", "distance_m": 120}])

    def _ga_busts_budget(*a, **k):
        raise _common.BudgetExceeded("simulated budget exhaustion")

    monkeypatch.setattr(ga_waste, "landfills_near", _ga_busts_budget)
    sig = cs._landfill_signal(*MELB, "VIC")
    assert sig["status"] == "error"
    assert sig["entries"] == []
    assert sig["score"] is None
