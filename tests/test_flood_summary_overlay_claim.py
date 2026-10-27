"""The flood page's summary may only talk about overlays that were checked.

2026-10-27: the summary said "no significant overlay indicators" for places
whose official flood mapping is not held (partial NSW/QLD/WA coverage), next
to a "no official flood overlay is held" warning. official_layer says what was
checked: only checked_no_hit (a statewide statutory layer came back empty)
earns an "outside the overlays" sentence. Score and label are untouched.

The summary logic is a pure block inside flood.html (between the
BEGIN/END flood-summary markers); it is run here in node.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "property_scores" / "api" / "static"
FLOOD_HTML = STATIC / "flood.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _block() -> str:
    html = FLOOD_HTML.read_text()
    start = html.index("// BEGIN flood-summary")
    end = html.index("// END flood-summary")
    return html[start:end]


def _run(cases: list[tuple[dict, int | None]]) -> list[dict]:
    js = _block() + (
        "\nconst cases = " + json.dumps(cases) + ";\n"
        "console.log(JSON.stringify(cases.map(([d, sc]) => ({"
        "summary: floodSummaryText(d, sc), line: floodOverlayLine(d)}))));\n")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


HIGH = {"hand": {"hand_m": 25}}


def test_well_elevated_without_an_official_check_does_not_claim_overlays():
    for layer in ("checked_partial_coverage", "none", None):
        r = _run([({**HIGH, "official_layer": layer}, 92)])[0]
        assert "overlay indicators" not in r["summary"]
        assert "Not within any" not in r["line"]
        assert r["summary"].startswith("Well elevated above drainage; ")
    r = _run([({**HIGH, "official_layer": "none"}, 92)])[0]
    assert r["summary"] == ("Well elevated above drainage; no official flood "
                            "overlay was checked here.")
    assert r["line"] == "No official flood overlay was checked here."


def test_partial_coverage_says_what_was_checked():
    r = _run([({**HIGH, "official_layer": "checked_partial_coverage"}, 92)])[0]
    assert "none of the held flood overlays covers this point" in r["summary"]
    assert "partial" in r["summary"]
    assert "does not mean the site is outside a flood area" in r["line"]


def test_statewide_clear_keeps_the_overlay_wording():
    r = _run([({**HIGH, "official_layer": "checked_no_hit"}, 92)])[0]
    assert r["summary"] == ("Well elevated above drainage; no official flood "
                            "overlay covers this point.")
    assert r["line"] == "Not within any government flood overlay"
    r = _run([({"official_layer": "checked_no_hit"}, 90)])[0]
    assert r["summary"] == "No significant flood indicators detected at this location."


def test_no_terrain_signal_and_no_check():
    r = _run([({"official_layer": "checked_partial_coverage"}, 85)])[0]
    assert r["summary"].startswith("No terrain or water-record flood indicators detected; ")
    assert "No significant flood indicators" not in r["summary"]
    assert _run([({"official_layer": "none"}, 60)])[0]["summary"] is None


def test_overlay_hit_reads_as_before():
    d = {"official_layer": "hit", "flood_zones": ["LSIO (1% AEP)"], "hand": {"hand_m": 1}}
    r = _run([(d, 40)])[0]
    assert r["summary"] == "Inside a government flood overlay, low elevation above drainage."


def test_evidence_required_hit_is_not_called_unchecked():
    """PlanSA "Flooding Evidence Required" is the only hit: official_layer is
    'hit', flood_zones is non-empty, but there is no severity zone. The page
    must say the evidence control applies, not that nothing was checked."""
    ev = {"official_layer": "hit",
          "flood_zones": ["Hazards (Flooding Evidence Required)"]}
    r = _run([({**ev, **HIGH}, 85), (ev, 85), ({**ev, "flood_zones": []}, 60)])
    for x in r:
        assert "was checked here" not in x["summary"]
        assert "requiring site-specific flood evidence applies here" in x["summary"]
    assert r[0]["summary"] == ("Well elevated above drainage; an official control "
                               "requiring site-specific flood evidence applies here.")
    assert r[1]["summary"] == ("An official control requiring site-specific flood "
                               "evidence applies here.")


def _zones_cell(cases):
    html = (STATIC / "index.html").read_text()
    block = html[html.index("// BEGIN flood-zones-cell"):html.index("// END flood-zones-cell")]
    js = block + ("\nconsole.log(JSON.stringify(" + json.dumps(cases)
                  + ".map(floodZonesCell)));\n")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_index_overlay_zones_cell():
    assert _zones_cell([
        {"official_layer": "checked_no_hit", "flood_zones": []},
        {"official_layer": "checked_partial_coverage"},
        {"official_layer": "none"},
        {},
        {"official_layer": "hit", "flood_zones": ["LSIO (1% AEP)", "FO"]},
    ]) == ["None", "None of the held overlays (partial coverage)", "Not checked",
           "Not checked", "LSIO (1% AEP), FO"]


def test_page_uses_the_helpers():
    html = FLOOD_HTML.read_text()
    assert "const summary = floodSummaryText(d, sc);" in html
    assert "${floodOverlayLine(d)}" in html
    assert html.count("Not within any government flood overlay") == 1  # only in floodOverlayLine
    index = (STATIC / "index.html").read_text()
    assert "const zones = floodZonesCell(s);" in index
