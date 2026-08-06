"""The model stamp consumers cache against.

A consumer (DA Leads) keeps score payloads for weeks and decides whether to
trust one by comparing this string. So the properties that matter are: it
MOVES when the engine would score differently, and it does NOT move otherwise.
A stamp that never changes is the bug it was written to fix; a stamp that
changes on every call silently disables every downstream cache.
"""
import os

from fastapi.testclient import TestClient

from property_scores.api import stamp
from property_scores.api.main import app


client = TestClient(app)


def test_stamp_is_stable_across_calls():
    """If this drifts, every cached score in the consumer is invalidated on
    every request -- nine live models recomputed per lookup, forever, and
    nothing anywhere reports a fault."""
    assert stamp.model_stamp() == stamp.model_stamp()


def test_stamp_is_short_and_hex():
    """It is carried inside a cached payload and compared, never parsed."""
    value = stamp.model_stamp()
    assert len(value) == 12
    int(value, 16)


def test_stamp_moves_when_a_component_moves(monkeypatch):
    """The mechanism itself. Driven through components() rather than by
    reaching into the hash, so the test survives a change of hash function."""
    base = stamp.model_stamp()
    monkeypatch.setattr(stamp, "components",
                        lambda: {"code": "git:deadbeef", "noise_config": "x"})
    assert stamp.model_stamp() != base


def test_stamp_is_order_independent(monkeypatch):
    """Adding a component in a different position must not by itself flush
    every consumer cache."""
    monkeypatch.setattr(stamp, "components", lambda: {"a": "1", "b": "2"})
    forward = stamp.model_stamp()
    monkeypatch.setattr(stamp, "components", lambda: {"b": "2", "a": "1"})
    assert stamp.model_stamp() == forward


def test_components_name_what_they_cover():
    """`components` exists so that "why did every cache just flush" has an
    answer. The noise config signature and the resolved model artefact are the
    two that move most often."""
    parts = stamp.components()
    assert "code" in parts
    assert "noise_config" in parts
    assert "noise_model" in parts
    assert all(isinstance(v, str) for v in parts.values())


def test_noise_config_signature_is_the_engines_own(monkeypatch):
    """Not a re-derivation. _CONFIG_SIG already encodes every flag and tunable
    that changes noise numbers without changing code; copying that logic here
    would drift from it."""
    from property_scores.noise.score import _CONFIG_SIG

    assert stamp.components()["noise_config"] == _CONFIG_SIG


def test_a_missing_input_artefact_is_a_distinct_state(monkeypatch, tmp_path):
    """"absent" has to be part of the stamp: a missing WorldCover mosaic
    degrades bushfire fuel to the building-density proxy, so restoring the
    mosaic must invalidate everything scored without it."""
    present = tmp_path / "lc.vrt"
    present.write_text("x")

    monkeypatch.setattr(stamp, "_artefact_paths",
                        lambda: [("worldcover_vrt", present)])
    with_file = stamp.model_stamp()

    monkeypatch.setattr(stamp, "_artefact_paths",
                        lambda: [("worldcover_vrt", tmp_path / "gone.vrt")])
    without_file = stamp.model_stamp()

    assert stamp._artefact_tokens()["worldcover_vrt"] == "absent"
    assert with_file != without_file


def test_code_revision_prefers_an_explicit_deploy_marker(monkeypatch):
    monkeypatch.setattr(stamp, "_code_revision", None)
    monkeypatch.setenv("PROPERTY_SCORES_REV", "release-42")
    assert stamp.code_revision() == "env:release-42"


def test_code_revision_is_never_unknown(monkeypatch):
    """An unknown code revision would make the stamp claim more stability than
    it has: a deploy would look like "nothing changed" to every consumer."""
    monkeypatch.setattr(stamp, "_code_revision", None)
    monkeypatch.delenv("PROPERTY_SCORES_REV", raising=False)
    value = stamp.code_revision()
    assert value.split(":", 1)[0] in {"py", "pid"}
    assert value.split(":", 1)[1]


def test_code_revision_ignores_docs_and_static_pages(monkeypatch, tmp_path):
    """This repo holds docs/, CHANGES.md, scripts/, tests/ and api/static/*.html
    beside the scoring modules, and its history is full of docs-only commits.
    Keying on repo HEAD would make a README fix invalidate every cached score
    downstream -- nine live models per parcel for a change that cannot move a
    number. Only the scoring sources are in the stamp."""
    pkg = tmp_path / "property_scores"
    (pkg / "api" / "static").mkdir(parents=True)
    (pkg / "noise").mkdir()
    (pkg / "noise" / "score.py").write_text("SCORE = 1\n")
    (pkg / "api" / "static" / "page.py").write_text("PAGE = 1\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "readme.md").write_text("hello\n")
    monkeypatch.setattr(stamp, "_REPO_ROOT", tmp_path)
    monkeypatch.delenv("PROPERTY_SCORES_REV", raising=False)

    monkeypatch.setattr(stamp, "_code_revision", None)
    before = stamp.code_revision()

    # Docs change, and a static page change: neither may move it.
    (tmp_path / "docs" / "readme.md").write_text("hello, world\n")
    (pkg / "api" / "static" / "page.py").write_text("PAGE = 2\n")
    monkeypatch.setattr(stamp, "_code_revision", None)
    assert stamp.code_revision() == before

    # A scoring module changes: it must move.
    (pkg / "noise" / "score.py").write_text("SCORE = 2\n")
    monkeypatch.setattr(stamp, "_code_revision", None)
    assert stamp.code_revision() != before


def test_code_revision_is_content_not_mtime(monkeypatch, tmp_path):
    """A redeploy that rewrites an unchanged file (rsync, fresh clone) must not
    read as a change."""
    pkg = tmp_path / "property_scores" / "noise"
    pkg.mkdir(parents=True)
    src = pkg / "score.py"
    src.write_text("SCORE = 1\n")
    monkeypatch.setattr(stamp, "_REPO_ROOT", tmp_path)
    monkeypatch.delenv("PROPERTY_SCORES_REV", raising=False)
    monkeypatch.setattr(stamp, "_code_revision", None)
    before = stamp.code_revision()

    os.utime(src, (1_700_000_000, 1_700_000_000))
    src.write_text("SCORE = 1\n")          # same bytes, new mtime
    monkeypatch.setattr(stamp, "_code_revision", None)
    assert stamp.code_revision() == before


def test_small_json_artefacts_are_content_hashed(monkeypatch, tmp_path):
    """Same argument one level down: a deploy that rewrites a byte-identical
    registry.json must not flush every consumer cache."""
    reg = tmp_path / "registry.json"
    reg.write_text('{"active": "eu-transfer-v1"}')
    monkeypatch.setattr(stamp, "_artefact_paths",
                        lambda: [("noise_registry", reg)])

    before = stamp._artefact_tokens()["noise_registry"]
    assert before.startswith("sha:")

    os.utime(reg, (1_700_000_000, 1_700_000_000))
    reg.write_text('{"active": "eu-transfer-v1"}')     # identical content
    assert stamp._artefact_tokens()["noise_registry"] == before

    reg.write_text('{"active": "eu-transfer-v2"}')     # real change
    assert stamp._artefact_tokens()["noise_registry"] != before


def test_repo_head_is_reported_but_not_stamped(monkeypatch):
    """It is the first thing a human wants when asking why the stamp moved, and
    exactly the wrong thing to invalidate caches on."""
    # The direct assertion: the stamped `code` term is the scoped content hash,
    # never the commit id. Without this, swapping code_revision() for
    # repo_head() in components() passes every other test in this file.
    assert stamp.components()["code"].split(":", 1)[0] in {"py", "env", "pid"}
    assert stamp.components()["code"] != f"git:{stamp.repo_head()}"

    monkeypatch.setattr(stamp, "components", lambda: {"code": "py:abc"})
    base = stamp.model_stamp()
    monkeypatch.setattr(stamp, "repo_head", lambda: "ffffff")
    assert stamp.model_stamp() == base

    body = client.get("/version").json()
    assert "repo_head" in body
    assert "repo_head" not in body["components"]


def test_startup_pins_the_code_revision_before_first_use(monkeypatch):
    """A deploy is `git pull` then restart. If the revision were resolved
    lazily on the first /version, a poll landing in that window would report
    the NEW commit while the OLD code is still scoring: consumers flush, refill
    with old-code payloads stamped as new, and the restart does not move the
    stamp again -- so those payloads are trusted for good."""
    monkeypatch.setattr(stamp, "_code_revision", None)
    with TestClient(app):
        assert stamp._code_revision is not None


def test_version_endpoint_publishes_the_stamp():
    r = client.get("/version")
    assert r.status_code == 200
    body = r.json()
    assert body["model_stamp"] == stamp.model_stamp()
    assert body["components"]["noise_config"]


def test_scores_response_carries_the_stamp_that_produced_it(monkeypatch):
    """The consumer records provenance from the response that computed the
    payload, not from a poll it made at some other moment."""
    monkeypatch.setattr(stamp, "model_stamp", lambda: "aabbccddeeff")
    r = client.get("/scores", params={"lat": -37.8, "lng": 145.0})
    assert r.status_code == 200
    assert r.json()["model_stamp"] == "aabbccddeeff"


def test_a_rebuilt_vrt_catalogue_moves_the_stamp(monkeypatch, tmp_path):
    """The mosaic gap is real but narrower than "nothing catches it": a REBUILT
    .vrt (tiles added or removed, paths changed) is caught, because the
    catalogue is content-hashed. Only rewriting the COGs underneath an
    unchanged .vrt escapes, and the docstring says so and names
    PROPERTY_SCORES_REV as the deliberate act for that case."""
    vrt = tmp_path / "lc.vrt"
    vrt.write_text("<VRTDataset><band>1</band></VRTDataset>")
    monkeypatch.setattr(stamp, "_artefact_paths",
                        lambda: [("worldcover_vrt", vrt)])

    before = stamp._artefact_tokens()["worldcover_vrt"]
    assert before.startswith("sha:")

    # Same byte count, different content -- size+mtime alone could miss this if
    # the rebuild landed inside the same second.
    vrt.write_text("<VRTDataset><band>2</band></VRTDataset>")
    assert stamp._artefact_tokens()["worldcover_vrt"] != before


def test_a_restart_alone_does_not_move_the_stamp(monkeypatch):
    """Stated as a limitation in the docstring, so it must be true: the code
    term is a content hash of the source, and restarting changes no source. A
    reader who believes otherwise will swap a mosaic, restart, and think every
    consumer has been invalidated."""
    monkeypatch.delenv("PROPERTY_SCORES_REV", raising=False)
    monkeypatch.setattr(stamp, "_code_revision", None)
    first = stamp.code_revision()
    monkeypatch.setattr(stamp, "_code_revision", None)   # "restart"
    assert stamp.code_revision() == first


def test_property_scores_rev_is_the_deliberate_override(monkeypatch):
    """The documented escape hatch for a mosaic swap. If this stops taking
    precedence, the docstring's only mitigation stops existing."""
    monkeypatch.setattr(stamp, "_code_revision", None)
    monkeypatch.setenv("PROPERTY_SCORES_REV", "mosaic-2026-08-07")
    with_rev = stamp.components()["code"]

    monkeypatch.setattr(stamp, "_code_revision", None)
    monkeypatch.delenv("PROPERTY_SCORES_REV")
    assert stamp.components()["code"] != with_rev


def test_the_static_exclusion_is_a_path_prefix_not_a_substring(monkeypatch,
                                                               tmp_path):
    """`"api/static" in path` would also drop api/static_helpers.py -- a
    scoring source silently outside the stamp, which is the failure this whole
    module exists to prevent, hidden in a string test."""
    pkg = tmp_path / "property_scores"
    (pkg / "api" / "static").mkdir(parents=True)
    (pkg / "api" / "static" / "page.py").write_text("PAGE = 1\n")
    (pkg / "api" / "static_helpers.py").write_text("HELP = 1\n")
    monkeypatch.setattr(stamp, "_REPO_ROOT", tmp_path)
    monkeypatch.delenv("PROPERTY_SCORES_REV", raising=False)

    monkeypatch.setattr(stamp, "_code_revision", None)
    before = stamp.code_revision()

    (pkg / "api" / "static" / "page.py").write_text("PAGE = 2\n")
    monkeypatch.setattr(stamp, "_code_revision", None)
    assert stamp.code_revision() == before, "static pages are excluded"

    (pkg / "api" / "static_helpers.py").write_text("HELP = 2\n")
    monkeypatch.setattr(stamp, "_code_revision", None)
    assert stamp.code_revision() != before, (
        "static_helpers.py is a scoring source and must be in the stamp")
