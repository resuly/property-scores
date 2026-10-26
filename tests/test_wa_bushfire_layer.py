"""WA bushfire fallback and its sentinel canary must name the same live layer.

Landgate dropped layer 8 (OBRM-023) from Map_of_Bush_Fire_Prone_Areas_3 and
it answers 404 "Layer not found"; the canary on it sat red for 35 days while
the scorer's live fallback pointed at the same dead layer. Layer 11 is
OBRM-026, the catalogue's stated successor (checked 2026-10-27). The canary
exists to prove the scorer's endpoint is alive, so the two must not drift.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from property_scores.bushfire.score import ENDPOINTS

ROOT = Path(__file__).resolve().parents[1]
SERVICE = "/Landgate_Public_Maps/Map_of_Bush_Fire_Prone_Areas_3/MapServer/"
RETIRED = {"8"}


def _layer(url: str) -> str:
    m = re.search(re.escape(SERVICE) + r"(\d+)", url)
    assert m, url
    return m.group(1)


def test_wa_fallback_is_on_obrm_026():
    (_, url, _), = ENDPOINTS["WA"]
    assert _layer(url) == "11"
    assert _layer(url) not in RETIRED


def test_wa_canary_probes_the_scorers_layer():
    canaries = json.loads(
        (ROOT / "data" / "truth_anchors" / "canaries.json").read_text())
    wa = [c for c in canaries if SERVICE in c["url"]]
    assert len(wa) == 1 and wa[0]["active"]
    (_, url, _), = ENDPOINTS["WA"]
    assert _layer(wa[0]["url"]) == _layer(url)
