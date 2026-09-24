"""The SAPPA atlas version lives in one constant; nothing may pin another.

PlanSA deletes old PropertyPlanningAtlasV{N} services outright (V18 went 404
on 2026-09-24). A scorer, or the canary that is supposed to catch that, left
on an older version would silently read as "service down" after the next bump.
"""

import json
import re
from pathlib import Path

from property_scores.bushfire import score as bushfire
from property_scores.common import sappa
from property_scores.flood import score as flood

ROOT = Path(__file__).resolve().parents[1]
PIN = re.compile(r"PropertyPlanningAtlasV(\d+)")


def _sa_urls():
    return ([url for _n, url, _s in bushfire.ENDPOINTS["SA"]]
            + [url for _n, url, _s in flood.ENDPOINTS["SA"]])


def test_scorers_use_the_shared_atlas_version():
    urls = _sa_urls()
    assert urls
    for url in urls:
        assert url.startswith(sappa.SAPPA_BASE + "/"), url


def test_canaries_follow_the_shared_atlas_version():
    canaries = json.loads(
        (ROOT / "data" / "truth_anchors" / "canaries.json").read_text())
    pinned = [PIN.search(c["url"]) for c in canaries]
    versions = {int(m.group(1)) for m in pinned if m}
    assert versions == {sappa.SAPPA_ATLAS_VERSION}


def test_no_other_source_file_pins_an_atlas_version():
    offenders = []
    for path in list((ROOT / "property_scores").rglob("*.py")) + \
            list((ROOT / "scripts").rglob("*.py")):
        if path.name == "sappa.py":
            continue
        if PIN.search(path.read_text(errors="ignore")):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_layer_ids_verified_on_the_current_version():
    """Tripwire against an accidental id edit, nothing more.

    It compares against a hard-coded list and never contacts the service, so
    it cannot tell whether these ids still name the right layers upstream.
    That was checked by hand, by layer name, against V19 on 2026-09-24 (see
    common/sappa.py) and must be re-checked on every version bump.
    """
    ids = {url.rsplit("/", 1)[1] for url in _sa_urls()}
    assert ids == {"135", "136", "137", "138", "139", "140",
                   "141", "372", "367", "403"}
