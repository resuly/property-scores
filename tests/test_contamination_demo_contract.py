from pathlib import Path


DEMO = (Path(__file__).resolve().parents[1]
        / "property_scores/api/static/contamination.html").read_text(
            encoding="utf-8")


def test_demo_renders_tas_evidence_without_register_or_score_claim():
    assert "d.historical_use?.entries" in DEMO
    assert "Evidence-only activity context (500m)" in DEMO
    assert "Petroleum storage notification" in DEMO
    assert "does not prove contamination or activity on this parcel" in DEMO
    assert "These records do not change the score" in DEMO
    assert "TAS, ACT, NT rely on industrial proximity only" not in DEMO


def test_demo_escapes_upstream_context_fields_before_inner_html():
    assert "function escapeHtml(value)" in DEMO
    for value in ("name", "kind", "detail", "entry.distance_m"):
        assert f"escapeHtml({value})" in DEMO
