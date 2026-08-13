"""PlanSA Evidence Required is a due-diligence trigger, not a safety score."""

from pathlib import Path

from property_scores.flood import score as fs


def test_sa_evidence_required_layer_is_queried_without_severity_mapping():
    matches = {layer[0]: layer[2] for layer in fs.ENDPOINTS["SA"]}
    assert matches["Hazards (Flooding Evidence Required)"] == \
        "evidence_required"
    endpoint = next(url for name, url, _severity in fs.ENDPOINTS["SA"]
                    if name == "Hazards (Flooding Evidence Required)")
    assert endpoint.endswith("/MapServer/403")


def test_evidence_required_hit_is_visible_but_neutral(monkeypatch):
    monkeypatch.setattr(fs, "_detect_state", lambda *_: "SA")
    monkeypatch.setattr(
        "property_scores.flood.local_overlays.check", lambda *_: None)
    monkeypatch.setattr(
        fs, "_overlay_check",
        lambda *_: (None, ["Hazards (Flooding Evidence Required)"], []),
    )
    monkeypatch.setattr(fs, "_water_proximity_local", lambda *_: None)
    monkeypatch.setattr(fs, "_hand_local", lambda *_: None)
    monkeypatch.setattr(fs, "_query_ifd", lambda *_: None)
    monkeypatch.setattr(
        "property_scores.flood.study_depth.depth_at", lambda *_: None)

    result = fs.flood_score(-35.0, 139.0)

    assert result["score"] is None
    assert result["label"] == "Flood Evidence Required"
    assert result["official_layer"] == "hit"
    assert result["overlay_basis"] == "state_service"
    assert result["flood_zones"] == [
        "Hazards (Flooding Evidence Required)"]
    note = result["official_layer_note"]
    assert "risk contribution is unknown and neutral" in note
    assert "Obtain the required flood evidence" in note


def test_evidence_required_does_not_hide_a_severity_bearing_hit(monkeypatch):
    monkeypatch.setattr(fs, "_layer_has_features", lambda url, *_a, **_k: (
        url.endswith("/372") or url.endswith("/403")))

    worst, zones, warnings = fs._overlay_check("SA", -35.0, 139.0)

    assert worst == "moderate"
    assert zones == [
        "Hazards (Flooding - General)",
        "Hazards (Flooding Evidence Required)",
    ]
    assert warnings == []


def test_evidence_required_does_not_add_a_multi_zone_penalty(monkeypatch):
    monkeypatch.setattr(fs, "_detect_state", lambda *_: "SA")
    monkeypatch.setattr(
        "property_scores.flood.local_overlays.check", lambda *_: None)
    monkeypatch.setattr(
        fs, "_overlay_check",
        lambda *_: (
            "moderate",
            ["Hazards (Flooding - General)",
             "Hazards (Flooding Evidence Required)"],
            [],
        ),
    )
    monkeypatch.setattr(fs, "_water_proximity_local", lambda *_: None)
    monkeypatch.setattr(fs, "_hand_local", lambda *_: None)
    monkeypatch.setattr(fs, "_query_ifd", lambda *_: None)
    monkeypatch.setattr(
        "property_scores.flood.study_depth.depth_at", lambda *_: None)

    result = fs.flood_score(-35.0, 139.0)

    # General alone maps to 60. Evidence Required must not turn it into 57.
    assert result["score"] == 60


def test_incomplete_sa_service_check_is_not_checked_clean(monkeypatch):
    monkeypatch.setattr(fs, "_detect_state", lambda *_: "SA")
    monkeypatch.setattr(
        "property_scores.flood.local_overlays.check", lambda *_: None)
    monkeypatch.setattr(
        fs, "_overlay_check",
        lambda *_: (None, [], ["Could not reach Evidence Required"]),
    )
    monkeypatch.setattr(fs, "_water_proximity_local", lambda *_: None)
    monkeypatch.setattr(fs, "_hand_local", lambda *_: None)
    monkeypatch.setattr(fs, "_query_ifd", lambda *_: None)
    monkeypatch.setattr(
        "property_scores.flood.study_depth.depth_at", lambda *_: None)

    result = fs.flood_score(-35.0, 139.0)

    assert result["score"] is None
    assert result["label"] == "Official Flood Check Incomplete"
    assert result["official_layer"] == "none"
    assert result["overlay_basis"] == "state_service"
    assert result["warnings"] == ["Could not reach Evidence Required"]


def test_sa_flood_endpoint_bypasses_legacy_numeric_cache(monkeypatch):
    from property_scores.api import main

    cached = {"score": 95, "label": "Very Low Risk", "cached": True}
    live = {"score": None, "label": "Flood Evidence Required", "state": "SA"}
    monkeypatch.setattr(main, "flood_detect_state", lambda *_: "SA")
    monkeypatch.setattr(main, "flood_cache_lookup", lambda *_: cached)
    monkeypatch.setattr(main, "flood_score", lambda *_: live)

    assert main.get_flood(-35.0, 139.0, nocache=False) == live


def test_non_sa_flood_endpoint_can_still_use_cache(monkeypatch):
    from property_scores.api import main

    cached = {"score": 90, "label": "Very Low Risk", "cached": True}
    monkeypatch.setattr(main, "flood_detect_state", lambda *_: "VIC")
    monkeypatch.setattr(main, "flood_cache_lookup", lambda *_: cached)
    monkeypatch.setattr(
        main, "flood_score", lambda *_: (_ for _ in ()).throw(
            AssertionError("live scorer should not run")))

    assert main.get_flood(-37.8, 145.0, nocache=False) == cached


def test_web_surfaces_render_unknown_and_explain_evidence_required():
    static = Path(__file__).resolve().parents[1] / "property_scores/api/static"
    index_html = (static / "index.html").read_text()
    flood_html = (static / "flood.html").read_text()

    assert "unknownCard('Flood Risk'" in index_html
    assert "s.official_layer_note" in index_html
    assert "if (sc == null)" in flood_html
    assert "d.official_layer_note" in flood_html
    assert "severityZones" in flood_html
    assert "inside a government flood overlay" in flood_html
