"""Every SAPPA request on the scoring path must carry the SAPPA Referer.

The geohub host sits behind a CloudFront WAF that rejects requests without
``Referer: https://sappa.plan.sa.gov.au/``. The scorers turn that rejection
into "could not reach", so dropping the header would silently degrade every
SA flood and bushfire score while the rest of the suite stays green. The
expected value is spelled out literally, not read from SAPPA_HEADERS, so that
emptying the shared constant fails here too.
"""

import pytest

from property_scores.bushfire import score as bs
from property_scores.flood import score as fs

REFERER = "https://sappa.plan.sa.gov.au/"
LAT, LNG = -34.9330, 138.5640


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def fake_get(url, params=None, timeout=None, headers=None, **_kw):
        seen.append((url, dict(headers or {})))
        return _Resp({"count": 1, "features": [{"attributes": {}}]})

    monkeypatch.setattr(fs.requests, "get", fake_get)
    monkeypatch.setattr(bs.requests, "get", fake_get)
    return seen


def _assert_all_sappa_calls_have_referer(calls, expected_urls):
    sappa = [(u, h) for u, h in calls if "geohub.sa.gov.au" in u]
    assert {u.rsplit("/query", 1)[0] for u, _h in sappa} == set(expected_urls)
    for url, headers in sappa:
        assert headers.get("Referer") == REFERER, url


def test_flood_sa_overlay_queries_send_the_sappa_referer(calls):
    urls = [url for _n, url, _s in fs.ENDPOINTS["SA"]]
    _severity, hits, warnings = fs._overlay_check("SA", LAT, LNG)
    assert warnings == [] and hits
    _assert_all_sappa_calls_have_referer(calls, urls)


def test_bushfire_sa_overlay_queries_send_the_sappa_referer(calls):
    urls = [url for _n, url, _s in bs.ENDPOINTS["SA"]]
    for name, url, severity in bs.ENDPOINTS["SA"]:
        _sev, _detail, ok = bs._check_layer("SA", name, url, severity, LAT, LNG)
        assert ok, name
    _assert_all_sappa_calls_have_referer(calls, urls)


def test_non_sappa_hosts_do_not_get_the_referer(calls):
    fs._overlay_check("VIC", -37.81, 144.96)
    others = [(u, h) for u, h in calls if "geohub.sa.gov.au" not in u]
    assert others and all("Referer" not in h for _u, h in others)
