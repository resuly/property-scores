"""Contamination score fail-closed semantics.

Anchor case (2026-08-10 audit): an EPA register answering 503 and an Overture
outage both produced a 95 "Very Clean" that was then cached for an hour. A
failed lookup is not a clean register, and the public label must not sound
reassuring when the evidence was never retrieved.
"""
import pytest
import requests

from property_scores.contamination import score as cs


class _Resp:
    def __init__(self, ok, payload=None):
        self.ok = ok
        self.status_code = 200 if ok else 503
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _clear_cache():
    cs._contam_cache.clear()
    yield
    cs._contam_cache.clear()


# ---------------------------------------------------------------------------
# 1. non-2xx must be distinguishable from an empty register
# ---------------------------------------------------------------------------

EPA_FUNCS = [cs._vic_epa_sites, cs._nsw_epa_sites, cs._wa_epa_sites]


@pytest.mark.parametrize("func", EPA_FUNCS)
def test_epa_non_2xx_returns_none(monkeypatch, func):
    monkeypatch.setattr(cs.requests, "get", lambda *a, **k: _Resp(False))
    assert func(-37.8, 144.96) is None


@pytest.mark.parametrize("func", EPA_FUNCS)
def test_epa_exception_returns_none(monkeypatch, func):
    def _boom(*a, **k):
        raise requests.RequestException("connection dropped")

    monkeypatch.setattr(cs.requests, "get", _boom)
    assert func(-37.8, 144.96) is None


@pytest.mark.parametrize("func", EPA_FUNCS)
def test_epa_empty_register_returns_empty_list(monkeypatch, func):
    monkeypatch.setattr(cs.requests, "get", lambda *a, **k: _Resp(True, {"features": []}))
    assert func(-37.8, 144.96) == []


# ArcGIS REST and GeoServer WFS report a failed query with HTTP 200 plus an
# error envelope, which is the more common outage shape than a non-2xx.
HTTP_200_FAILURE_BODIES = [
    {"error": {"code": 400, "message": "Unable to complete operation"}},   # ArcGIS
    {"error": {"code": 500, "message": "Token Required"}},                 # ArcGIS
    {"exceptions": [{"exceptionCode": "NoApplicableCode"}]},               # WFS
    # an error envelope that also carries an empty feature list: the error key
    # wins, an accompanying [] is not evidence the register was searched
    {"error": {"code": 400}, "features": []},
    {"exceptions": [{"exceptionCode": "NoApplicableCode"}], "features": []},
    {},                                                                    # no features key
    {"features": None},
    {"type": "FeatureCollection"},                                         # truncated body
    [],                                                                    # not an object
]


@pytest.mark.parametrize("func", EPA_FUNCS)
@pytest.mark.parametrize("body", HTTP_200_FAILURE_BODIES)
def test_epa_http_200_error_body_returns_none(monkeypatch, func, body):
    monkeypatch.setattr(cs.requests, "get", lambda *a, **k: _Resp(True, body))
    assert func(-37.8, 144.96) is None


def test_http_200_error_body_does_not_produce_a_clean_score(monkeypatch):
    # Botany NSW is the repo's own "very high risk" truth anchor: an ArcGIS
    # error envelope must not turn it into a clean register.
    monkeypatch.setattr(cs.requests, "get",
                        lambda *a, **k: _Resp(True, {"error": {"code": 400}}))
    _patch_industrial(monkeypatch, ok=True)
    r = cs.contamination_score(-33.9450, 151.2000)
    assert r["epa_status"] == "error"
    assert r["label"] == cs.LABEL_INCOMPLETE
    assert cs._contam_cache == {}


# ---------------------------------------------------------------------------
# 2. industrial proximity carries its own status
# ---------------------------------------------------------------------------

def test_industrial_status_ok(monkeypatch):
    monkeypatch.setattr(cs, "get_db", lambda: object())
    monkeypatch.setattr(cs, "pois_near_detailed", lambda *a, **k: [])
    ind = cs._industrial_proximity(-37.8, 144.96)
    assert ind["industrial_status"] == "ok"
    assert ind["count_500m"] == 0


def test_industrial_status_error_on_failure(monkeypatch):
    def _boom():
        raise RuntimeError("overture parquet unreachable")

    monkeypatch.setattr(cs, "get_db", _boom)
    ind = cs._industrial_proximity(-37.8, 144.96)
    assert ind["industrial_status"] == "error"
    # legacy keys stay for backward compatibility
    assert ind["count_500m"] == 0
    assert ind["nearest_m"] is None
    assert ind["sites"] == []


# ---------------------------------------------------------------------------
# 3. label gate
# ---------------------------------------------------------------------------

def test_label_bands_unchanged_when_everything_ok():
    assert cs._contamination_label(95, epa_status="ok", ind_failed=False) == "Very Clean"
    assert cs._contamination_label(80, epa_status="ok", ind_failed=False) == "Clean"
    assert cs._contamination_label(55, epa_status="ok", ind_failed=False) == "Low Risk"
    assert cs._contamination_label(35, epa_status="ok", ind_failed=False) == "Moderate Risk"
    assert cs._contamination_label(20, epa_status="ok", ind_failed=False) == "High Risk"
    assert cs._contamination_label(5, epa_status="ok", ind_failed=False) == "Very High Risk"


@pytest.mark.parametrize("score", [70, 80, 95, 100])
def test_reassuring_label_blocked_on_epa_error(score):
    assert cs._contamination_label(score, epa_status="error", ind_failed=False) == \
        cs.LABEL_INCOMPLETE


@pytest.mark.parametrize("score", [70, 80, 95, 100])
def test_reassuring_label_blocked_on_industrial_error(score):
    assert cs._contamination_label(score, epa_status="ok", ind_failed=True) == \
        cs.LABEL_INCOMPLETE


@pytest.mark.parametrize("score", [70, 80, 95, 100])
def test_reassuring_label_blocked_when_register_not_integrated(score):
    label = cs._contamination_label(score, epa_status="not_integrated", ind_failed=False)
    assert label == cs.LABEL_REGISTER_NOT_CHECKED
    assert "clean" not in label.lower()


@pytest.mark.parametrize("score", [10, 25, 45, 65])
def test_risk_labels_survive_degradation(score):
    # a bad band is still useful information, only reassurance is withheld
    assert cs._contamination_label(score, epa_status="error", ind_failed=True) == \
        cs._score_band_label(score)


def test_no_signal_at_all_is_check_unavailable():
    assert cs._contamination_label(None, epa_status="error", ind_failed=True) == \
        cs.LABEL_CHECK_UNAVAILABLE


def test_no_reassuring_label_reachable_while_degraded():
    for score in range(0, 101):
        for epa_status in ("ok", "error", "not_integrated"):
            for ind_failed in (False, True):
                label = cs._contamination_label(score, epa_status, ind_failed)
                degraded = epa_status != "ok" or ind_failed
                if degraded:
                    assert label not in cs._REASSURING_LABELS


# ---------------------------------------------------------------------------
# 4. end to end through contamination_score
# ---------------------------------------------------------------------------

MELB = (-37.8136, 144.9631)   # VIC, register integrated
BRIS = (-27.4698, 153.0251)   # QLD, no register integrated


def _patch_epa(monkeypatch, mode):
    """mode: 'ok' (empty register), 'http_error', 'exception'."""
    if mode == "ok":
        monkeypatch.setattr(cs.requests, "get",
                            lambda *a, **k: _Resp(True, {"features": []}))
    elif mode == "http_error":
        monkeypatch.setattr(cs.requests, "get", lambda *a, **k: _Resp(False))
    else:
        def _boom(*a, **k):
            raise requests.RequestException("dropped")
        monkeypatch.setattr(cs.requests, "get", _boom)


def _patch_industrial(monkeypatch, ok=True):
    if ok:
        monkeypatch.setattr(cs, "get_db", lambda: object())
        monkeypatch.setattr(cs, "pois_near_detailed", lambda *a, **k: [])
    else:
        def _boom():
            raise RuntimeError("overture down")
        monkeypatch.setattr(cs, "get_db", _boom)


def test_happy_path_still_very_clean(monkeypatch):
    _patch_epa(monkeypatch, "ok")
    _patch_industrial(monkeypatch, ok=True)
    r = cs.contamination_score(*MELB)
    assert r["score"] == 95
    assert r["label"] == "Very Clean"
    assert r["epa_status"] == "ok"
    assert r["industrial_status"] == "ok"
    assert "note" not in r


@pytest.mark.parametrize("mode", ["http_error", "exception"])
def test_epa_outage_is_not_clean_and_is_not_cached(monkeypatch, mode):
    _patch_epa(monkeypatch, mode)
    _patch_industrial(monkeypatch, ok=True)
    r = cs.contamination_score(*MELB)
    assert r["epa_status"] == "error"
    assert r["industrial_status"] == "ok"
    assert r["label"] == cs.LABEL_INCOMPLETE
    assert "clean" not in r["label"].lower()
    # the note is the only channel that carries the warning into the UI
    assert "EPA register could not be reached" in r["note"]
    assert "may understate risk" in r["note"]
    assert cs._contam_cache == {}


def test_industrial_outage_is_not_clean_and_is_not_cached(monkeypatch):
    _patch_epa(monkeypatch, "ok")
    _patch_industrial(monkeypatch, ok=False)
    r = cs.contamination_score(*MELB)
    assert r["epa_status"] == "ok"
    assert r["industrial_status"] == "error"
    assert r["label"] == cs.LABEL_INCOMPLETE
    assert cs._contam_cache == {}


def test_both_signals_down_gives_no_score(monkeypatch):
    _patch_epa(monkeypatch, "http_error")
    _patch_industrial(monkeypatch, ok=False)
    r = cs.contamination_score(*MELB)
    assert r["score"] is None
    assert r["label"] == cs.LABEL_CHECK_UNAVAILABLE
    assert cs._contam_cache == {}


def test_not_integrated_state_keeps_score_but_not_clean_label(monkeypatch):
    _patch_epa(monkeypatch, "ok")
    _patch_industrial(monkeypatch, ok=True)
    r = cs.contamination_score(*BRIS)
    assert r["state"] == "QLD"
    assert r["score"] == 95
    assert r["epa_status"] == "not_integrated"
    assert r["industrial_status"] == "ok"
    assert r["label"] == cs.LABEL_REGISTER_NOT_CHECKED
    assert "clean" not in r["label"].lower()
    assert "No QLD EPA register is integrated" in r["note"]
    # a missing register is a stable fact, not an outage: still cacheable
    assert len(cs._contam_cache) == 1


def test_industrial_outage_note_names_the_source(monkeypatch):
    _patch_epa(monkeypatch, "ok")
    _patch_industrial(monkeypatch, ok=False)
    r = cs.contamination_score(*MELB)
    assert "industrial land use data could not be reached" in r["note"]
    assert "may understate risk" in r["note"]


def test_healthy_result_is_cached(monkeypatch):
    _patch_epa(monkeypatch, "ok")
    _patch_industrial(monkeypatch, ok=True)
    cs.contamination_score(*MELB)
    assert len(cs._contam_cache) == 1
    assert cs.contamination_score(*MELB)["cached"] is True


# ---------------------------------------------------------------------------
# 5. cache key resolution
# ---------------------------------------------------------------------------

def test_cache_key_does_not_merge_neighbouring_parcels(monkeypatch):
    _patch_epa(monkeypatch, "ok")
    _patch_industrial(monkeypatch, ok=True)
    lng = MELB[1]
    # Both latitudes land in the same round(3) cell but are ~109m apart, which
    # straddles the 100m EPA distance band: under the old key one parcel was
    # served the other one's answer.
    lat_a, lat_b = -37.81251, -37.81349
    assert round(lat_a, 3) == round(lat_b, 3)
    assert round(lat_a, 4) != round(lat_b, 4)

    cs.contamination_score(lat_a, lng)
    assert cs.contamination_score(lat_b, lng).get("cached") is not True
    assert len(cs._contam_cache) == 2


def test_cache_key_still_merges_identical_coordinates(monkeypatch):
    _patch_epa(monkeypatch, "ok")
    _patch_industrial(monkeypatch, ok=True)
    lat, lng = MELB
    cs.contamination_score(lat, lng)
    # ~1m apart: below the round(4) grid, still one entry
    assert cs.contamination_score(lat + 0.000001, lng)["cached"] is True
    assert len(cs._contam_cache) == 1


# ---------------------------------------------------------------------------
# 6. the truth-probe harness must not go quiet when the score disappears
# ---------------------------------------------------------------------------

def _load_probes():
    import importlib.util
    import pathlib
    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "score_truth_probes.py"
    spec = importlib.util.spec_from_file_location("score_truth_probes", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_probe_scoreless_payload_fails_not_manual():
    # score None is newly reachable in Australia (both signal layers down).
    # MANUAL never reaches new_failures, so a dead anchor would alert nobody.
    probes = _load_probes()
    unavailable = {"score": None, "label": cs.LABEL_CHECK_UNAVAILABLE}
    assert probes.evaluate("flag_high_risk", unavailable)[0] == "FAIL"
    assert probes.evaluate("score<=40", unavailable)[0] == "FAIL"
    assert probes.evaluate("score>=80", unavailable)[0] == "FAIL"


def test_probe_still_evaluates_real_scores():
    probes = _load_probes()
    assert probes.evaluate("flag_high_risk", {"score": 10})[0] == "PASS"
    assert probes.evaluate("flag_high_risk", {"score": 95})[0] == "FAIL"
    assert probes.evaluate("score<=40", {"score": 10})[0] == "PASS"
    assert probes.evaluate("score<=40", {"score": 95})[0] == "FAIL"
    assert probes.evaluate("score>=80", {"score": 95})[0] == "PASS"
