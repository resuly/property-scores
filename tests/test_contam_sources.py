"""Unit tests for the contamination data source adapters.

All network access is mocked. Fixtures under tests/fixtures/contam_sources/
are real captured responses from the six upstream services (2026-08-27), so
the parsing assertions are about the actual payload shapes, not invented ones.

The spine of every adapter test is the fail-closed contract: a failed query
must return None and an empty register must return [], and no test may pass
if those two collapse into each other.
"""

import copy
import json
import os

import pytest

from property_scores.contamination.sources import (
    _common,
    act_register,
    ga_waste,
    nsw_groundwater,
    nsw_sites,
    qld_ea,
    sa_gpa,
    sa_licensed,
    tas_epa,
    vic_wfs,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "contam_sources")

# Captured at import, i.e. before the autouse _isolate fixture zeroes it for
# the other tests. This is the shipped backoff, and the backoff test asserts
# on it rather than on a literal so it cannot drift silently.
SHIPPED_RETRY_SLEEP_S = _common._RETRY_SLEEP_S

MELBOURNE = (-37.8136, 144.9631)
BOTANY = (-33.9500, 151.2000)
EDWARDSTOWN = (-34.9800, 138.5700)
ACT_BRADDON_BP = (-35.27582, 149.13277)


def _act_rows():
    return [
        {
            "id": "42",
            "district_notified": "Canberra Central",
            "division_notified": "Braddon",
            "section_notified": "29",
            "block_notified": "12",
            "notified_under_section": "76A(1)",
            "site_description": "Active BP Service Station",
            "owner_name": "must not ship",
        },
        {
            "id": "99",
            "district_current": "Canberra Central",
            "division_current": "Kingston",
            "section_current": "62",
            "block_current": "14+15+16+17+18",
            "notified_under_section": "91D(1)",
            "site_description": "Kingston Foreshore audit area",
        },
    ]


def _act_block(*, division="BRADDON", section=29, block=12,
               lifecycle="REGISTERED"):
    return {"features": [{"attributes": {
        "OBJECTID": 305387,
        "DISTRICT_NAME": "CANBERRA CENTRAL",
        "DIVISION_NAME": division,
        "SECTION_NUMBER": section,
        "BLOCK_NUMBER": block,
        "CURRENT_LIFECYCLE_STAGE": lifecycle,
    }}]}


def test_act_register_exact_count_and_parcel_join(monkeypatch):
    calls = []

    def fake_fetch(url, params):
        calls.append((url, params))
        if url == act_register.BLOCK_QUERY_URL:
            return _act_block()
        if params == {"$select": "count(*)"}:
            return [{"count": "2"}]
        return _act_rows()

    monkeypatch.setattr(act_register, "fetch_json", fake_fetch)
    hits = act_register.sites_at(*ACT_BRADDON_BP)

    assert len(hits) == 1
    assert hits[0] == {
        "site_id": "42",
        "name": "Active BP Service Station",
        "issue": (
            "ACT Register of contaminated sites, notified under section 76A(1)"
        ),
        "activity_type": "ACT contaminated sites register",
        "management_class": "76A(1)",
        "distance_m": 0,
        "geom": "polygon",
        "source": "ACT EPA Register of contaminated sites",
    }
    assert "owner_name" not in str(hits)
    block_params = calls[0][1]
    assert block_params["geometry"] == "149.13277,-35.27582"
    assert block_params["returnGeometry"] == "false"


def test_act_register_composite_blocks_match_but_divisions_do_not(monkeypatch):
    monkeypatch.setattr(
        act_register, "all_rows",
        lambda: act_register._parse_register_rows(_act_rows(), 2),
    )
    monkeypatch.setattr(
        act_register, "_subject_blocks",
        lambda *a: [{"district": "CANBERRA CENTRAL", "division": "KINGSTON",
                     "section": "62", "block": "16"}],
    )
    assert [row["site_id"] for row in act_register.sites_at(-35.3, 149.1)] == ["99"]

    monkeypatch.setattr(
        act_register, "_subject_blocks",
        lambda *a: [{"district": "CANBERRA CENTRAL", "division": "GRIFFITH",
                     "section": "62", "block": "16"}],
    )
    assert act_register.sites_at(-35.3, 149.1) == []


def test_act_register_count_schema_and_block_fail_closed(monkeypatch):
    monkeypatch.setattr(act_register, "fetch_json", lambda url, params: (
        [{"count": "3"}] if params == {"$select": "count(*)"} else _act_rows()))
    assert act_register.fetch_all_rows() is None

    monkeypatch.setattr(act_register, "_subject_blocks", lambda *a: None)
    monkeypatch.setattr(
        act_register, "all_rows",
        lambda: act_register._parse_register_rows(_act_rows(), 2),
    )
    assert act_register.sites_at(*ACT_BRADDON_BP) is None

    retired = _act_block(lifecycle="RETIRED")
    monkeypatch.setattr(act_register, "fetch_json", lambda *a, **k: retired)
    assert act_register._subject_blocks(*ACT_BRADDON_BP) is None


def test_act_register_handles_publisher_optional_fields_and_one_reversed_row():
    rows = act_register._parse_register_rows([{
        "district_notified": "Fyshwick",
        "division_notified": "Canberra Central",
        "section_notified": "2",
        "block_notified": "38",
        "notified_under_section": "76A(1)",
        "site_description": "Former Newcastle House",
    }, {
        "id": "10071",
        "district_notified": "Jerrabomberra",
        "division_notified": "Beard",
        "section_notified": "8",
        "block_notified": "24",
    }], 2)

    assert rows is not None
    assert rows[0]["district"] == "CANBERRA CENTRAL"
    assert rows[0]["division"] == "FYSHWICK"
    assert rows[0]["site_id"].startswith("derived-")
    assert rows[1]["notified_under_section"] == "Not specified"
    assert rows[1]["site_description"] == "ACT contaminated sites register entry"


def test_act_register_refresh_failure_serves_last_good(monkeypatch):
    act_register.clear_cache()
    clock = [1000.0]
    monkeypatch.setattr(act_register._time, "time", lambda: clock[0])
    monkeypatch.setattr(act_register, "fetch_all_rows", lambda: act_register._parse_register_rows(
        _act_rows(), 2))
    first = act_register.all_rows()
    clock[0] += act_register.REGISTER_CACHE_TTL_S + 1
    monkeypatch.setattr(act_register, "fetch_all_rows", lambda: None)
    assert act_register.all_rows() == first


def test_act_register_refresh_failure_rejects_expired_last_good(monkeypatch):
    act_register.clear_cache()
    clock = [1_700_000_000.0]
    monkeypatch.setattr(act_register._time, "time", lambda: clock[0])
    monkeypatch.setattr(act_register, "fetch_all_rows", lambda: act_register._parse_register_rows(
        _act_rows(), 2))
    assert act_register.all_rows()

    clock[0] += act_register.REGISTER_MAX_STALE_S + 1
    monkeypatch.setattr(act_register, "fetch_all_rows", lambda: None)
    assert act_register.all_rows() is None


def load_fixture(name: str):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return json.load(handle)


class FakeResponse:
    def __init__(self, payload=None, status_code=200, raw=None, bad_json=False):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._payload = payload
        self._bad_json = bad_json
        self.content = raw if raw is not None else json.dumps(payload or {}).encode()

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._payload


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """No real sockets, no retry sleeps, no cache bleed between tests."""
    monkeypatch.setattr(_common, "_RETRY_SLEEP_S", 0)

    def _forbidden(*args, **kwargs):  # pragma: no cover - safety net
        raise AssertionError("unexpected network call in a unit test")

    monkeypatch.setattr(_common.requests, "get", _forbidden)
    nsw_sites.clear_cache()
    act_register.clear_cache()
    sa_gpa.clear_cache()
    sa_licensed.clear_cache()
    tas_epa.clear_cache()
    yield
    nsw_sites.clear_cache()
    sa_gpa.clear_cache()
    sa_licensed.clear_cache()
    tas_epa.clear_cache()


def test_tas_epa_adapters_are_evidence_only_and_privacy_slim(monkeypatch):
    regulated_payload = {"features": [{
        "geometry": {"type": "Point", "coordinates": [147.3779101, -41.3006338]},
        "properties": {
            "OBJECTID": 1,
            "SITEID": 6198,
            "PREMISES_NAME": "Mountain Stream Fishery",
            "CLIENT_NAME": "Jane Citizen",
            "COMPANY_OR_BUSINESS_NO": "123456789",
            "ACTIVITY_CATEGORY": "Food Production",
            "LIST_GUID": "{50b02e07-0960-4b38-bddb-19f3540698de}",
        },
    }]}
    calls = install_responses(monkeypatch, [
        FakeResponse({"count": 1}), FakeResponse(regulated_payload)])

    rows = tas_epa.regulated_sites_near(-41.3006338, 147.3779101, 75)

    assert rows == [{
        "site_id": "6198",
        "premises_name": "Mountain Stream Fishery",
        "activity_category": "Food Production",
        "source_kind": "EPA regulated site",
        "distance_m": 0,
    }]
    assert calls[1]["url"] == tas_epa.REGULATED_QUERY_URL
    assert "CLIENT_NAME" not in calls[1]["params"]["outFields"]
    assert "COMPANY_OR_BUSINESS_NO" not in calls[1]["params"]["outFields"]
    assert "LIST_GUID" not in calls[1]["params"]["outFields"]
    assert "Citizen" not in json.dumps(rows)
    assert "50b02e07" not in json.dumps(rows)

    upss_payload = {"features": [{
        "geometry": {"type": "Point", "coordinates": [147.3418094, -42.8122728]},
        "properties": {
            "OBJECTID": 2,
            "SITE_ID": 348,
            "STATUS": "Active",
            "LIST_GUID": "{08bae563-3145-4494-8091-96371b3b6c01}",
        },
    }]}
    install_responses(monkeypatch, [
        FakeResponse({"count": 1}), FakeResponse(upss_payload)])
    rows = tas_epa.upss_near(-42.8122728, 147.3418094, 75)
    assert rows[0]["source_kind"] == "EPA underground petroleum storage system"
    assert rows[0]["site_id"] == "348"
    assert rows[0]["status"] == "Active"
    assert "LIST_GUID" not in json.dumps(rows)


def test_tas_epa_malformed_payload_fails_closed(monkeypatch):
    install_responses(monkeypatch, [
        FakeResponse({"count": 1}), FakeResponse({"features": [None]})])
    assert tas_epa.regulated_sites_near(-42.8, 147.3) is None


@pytest.mark.parametrize("page", [
    {"features": []},
    {"features": [{
        "geometry": {"type": "Point", "coordinates": [147.3, -42.8]},
        "properties": {"OBJECTID": 1, "SITEID": None},
    }]},
])
def test_tas_epa_count_or_required_id_mismatch_fails_closed(monkeypatch, page):
    install_responses(monkeypatch, [
        FakeResponse({"count": 1}), FakeResponse(page)])
    assert tas_epa.regulated_sites_near(-42.8, 147.3) is None


def test_second_batch_adapters_are_evidence_only_and_privacy_slim(monkeypatch):
    sa_payload = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [138.57, -34.98]},
            "properties": {
                "EPALICENCE": 20,
                "ACTIVITY": "Hydrocarbon and Chemical",
                "LICENCE_NAME": "Jane Citizen",
                "PR_LINK": "https://example.test/20",
            },
        }],
    }
    calls = install_responses(monkeypatch, [FakeResponse(sa_payload)])
    rows = sa_licensed.activities_near(-34.98, 138.57, 30)
    assert rows and rows[0]["activity"] == "Hydrocarbon and Chemical"
    assert "lat" not in rows[0] and "lng" not in rows[0]
    assert "Citizen" not in json.dumps(rows)
    assert calls[0]["url"] == sa_licensed.GEOJSON_URL

    internal_rows = sa_licensed.activities_near(
        -34.98, 138.57, 30, include_coordinates=True)
    assert internal_rows[0]["lat"] == -34.98
    assert internal_rows[0]["lng"] == 138.57
    assert "Citizen" not in json.dumps(internal_rows)

    qld_payload = {"features": [{
        "attributes": {
            "permit_ref_no": "EA1",
            "era": "ERA 57 - Regulated Waste Transport",
            "primary_holder": "John Citizen",
            "site": "1/RP1",
        },
    }]}
    install_responses(monkeypatch, [FakeResponse(qld_payload)])
    rows = qld_ea.activities_at(-27.5, 153.0)
    assert rows and rows[0]["inside"] is True
    assert "Citizen" not in json.dumps(rows)

    nsw_payload = {"features": [{
        "attributes": {
            "EPI_NAME": "Forbes Local Environmental Plan 2013",
            "LGA_NAME": "FORBES",
            "LAY_CLASS": "Groundwater Vulnerable",
            "EPI_TYPE": "LEP",
        },
    }]}
    install_responses(monkeypatch, [FakeResponse(nsw_payload)])
    rows = nsw_groundwater.vulnerability_at(-33.63, 148.32)
    assert rows and rows[0]["inside"] is True
    assert rows[0]["layer_class"] == "Groundwater Vulnerable"


def test_sa_licensed_refresh_failure_does_not_serve_stale_as_ok(monkeypatch):
    sa_licensed._cache = ([
        {"licence_number": 20, "activity": "old", "lat": -34.9, "lng": 138.6}
    ], 0)
    monkeypatch.setattr(sa_licensed, "fetch_json", lambda *a, **k: None)
    assert sa_licensed.all_activities(force_refresh=True) is None


def install_responses(monkeypatch, responses):
    """Serve ``responses`` in order and record the params of every call."""
    calls = []
    queue = list(responses)

    def _get(url, params=None, timeout=None, headers=None):
        calls.append({"url": url, "params": params or {},
                      "timeout": timeout, "headers": headers or {}})
        if not queue:
            raise AssertionError("more requests than the test provided")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(_common.requests, "get", _get)
    return calls


def install_responses_by_layer(monkeypatch, by_type_name):
    """Serve a response per WFS ``typeNames``, order-independent.

    ``landfills_near`` queries the VLR polygon and point layers concurrently
    (latency fix, 2026-08-27), so "the first request gets the first response"
    is no longer a fact about the adapter. Keying on the layer says what these
    tests actually mean: THIS layer answers THIS payload.
    """
    calls = []

    def _get(url, params=None, timeout=None, headers=None):
        params = params or {}
        calls.append({"url": url, "params": params,
                      "timeout": timeout, "headers": headers or {}})
        try:
            item = by_type_name[params.get("typeNames")]
        except KeyError:  # pragma: no cover - test wiring error
            raise AssertionError(
                f"unstubbed layer {params.get('typeNames')!r}") from None
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(_common.requests, "get", _get)
    return calls


# ---------------------------------------------------------------------------
# Shared politeness contract
# ---------------------------------------------------------------------------

def test_requests_carry_browser_ua_and_timeout(monkeypatch):
    payload = load_fixture("nsw_contaminated_sites_botany.json")
    calls = install_responses(monkeypatch, [FakeResponse(payload)])
    nsw_sites.sites_near(*BOTANY, 2000)
    assert calls[0]["timeout"] == 10
    assert "Mozilla/5.0" in calls[0]["headers"]["User-Agent"]


def test_transport_failure_gets_exactly_one_retry(monkeypatch):
    import requests as real_requests
    calls = install_responses(
        monkeypatch, [real_requests.ConnectionError("boom")])
    assert _common.fetch_json("https://example.test") is None
    assert len(calls) == 2


def test_upstream_error_body_is_not_retried(monkeypatch):
    """An HTTP 200 carrying an error document is a logical failure, not a
    flaky socket. Retrying it would only double the load on the service."""
    calls = install_responses(
        monkeypatch, [FakeResponse({"error": {"code": 400}})])
    assert _common.fetch_json("https://example.test") == {"error": {"code": 400}}
    assert len(calls) == 1


def test_retry_backoff_is_a_real_backoff(monkeypatch):
    """0.5s was a hammer, not a backoff: the retry only fires on a
    transport-level failure, i.e. when the far end is already unhappy."""
    import requests as real_requests
    slept = []
    monkeypatch.setattr(_common, "_RETRY_SLEEP_S", SHIPPED_RETRY_SLEEP_S)
    monkeypatch.setattr(_common._time, "sleep", slept.append)
    install_responses(monkeypatch, [real_requests.ConnectionError("boom")])
    assert _common.fetch_json("https://example.test") is None
    assert slept == [SHIPPED_RETRY_SLEEP_S]
    assert SHIPPED_RETRY_SLEEP_S >= 2.0


# ---------------------------------------------------------------------------
# Wall-clock budget (2026-08-27 latency review)
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_clock(monkeypatch):
    """A monotonic clock the test advances by hand, so budget tests cost 0s."""
    now = [1000.0]
    monkeypatch.setattr(_common, "_now", lambda: now[0])
    return now


def test_unbudgeted_calls_keep_the_full_timeout(monkeypatch):
    """The budget is opt-in: outside a budget block nothing is clamped."""
    calls = install_responses(monkeypatch, [FakeResponse({"features": []})])
    assert _common.remaining_budget() is None
    _common.fetch_json("https://example.test")
    assert calls[0]["timeout"] == 10


def test_socket_timeout_is_clamped_to_the_remaining_budget(
        monkeypatch, fake_clock):
    calls = install_responses(monkeypatch, [FakeResponse({"features": []})])
    with _common.budget(8.0):
        fake_clock[0] += 5.0
        _common.fetch_json("https://example.test")
    # 3s left, so a 10s socket timeout would overshoot the budget by 7s.
    assert calls[0]["timeout"] == pytest.approx(3.0)


def test_a_spent_budget_refuses_to_start_the_call(monkeypatch, fake_clock):
    calls = install_responses(monkeypatch, [FakeResponse({"features": []})])
    with pytest.raises(_common.BudgetExceeded):
        with _common.budget(8.0):
            fake_clock[0] += 8.5
            _common.fetch_json("https://example.test")
    assert calls == [], "started a request that could not finish in budget"


def test_a_spent_budget_stops_the_retry_instead_of_sleeping(
        monkeypatch, fake_clock):
    """The retry backoff must not outlive the budget either."""
    import requests as real_requests
    monkeypatch.setattr(_common, "_RETRY_SLEEP_S", SHIPPED_RETRY_SLEEP_S)
    slept = []
    monkeypatch.setattr(_common._time, "sleep", slept.append)

    def _get(url, params=None, timeout=None, headers=None):
        fake_clock[0] += timeout  # the request burned its whole timeout
        raise real_requests.ConnectionError("boom")

    monkeypatch.setattr(_common.requests, "get", _get)
    with pytest.raises(_common.BudgetExceeded):
        with _common.budget(8.0):
            _common.fetch_json("https://example.test")
    assert slept == [], "slept a 2s backoff with no budget left to use it"


def test_budget_is_restored_after_the_block(monkeypatch, fake_clock):
    with _common.budget(8.0):
        assert _common.remaining_budget() == pytest.approx(8.0)
    assert _common.remaining_budget() is None
    _common.check_budget()  # must not raise once the block is gone


def test_child_context_carries_the_budget_into_a_worker_thread(
        monkeypatch, fake_clock):
    """vic_wfs runs the two VLR layers on a local pool; unbudgeted workers
    would put the whole point of the budget back on the floor."""
    from concurrent.futures import ThreadPoolExecutor
    with _common.budget(8.0):
        with ThreadPoolExecutor(max_workers=1) as pool:
            inherited = pool.submit(
                _common.child_context().run, _common.remaining_budget).result()
            plain = pool.submit(_common.remaining_budget).result()
    assert inherited == pytest.approx(8.0)
    assert plain is None


def test_fetch_bytes_honours_the_budget(monkeypatch, fake_clock):
    """SA's whole-of-state bundle goes through the other fetcher."""
    calls = install_responses(monkeypatch, [FakeResponse({})])
    with _common.budget(8.0):
        fake_clock[0] += 6.0
        _common.fetch_bytes("https://example.test")
    assert calls[0]["timeout"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# NSW Contaminated Sites List
# ---------------------------------------------------------------------------

def test_nsw_parses_real_payload(monkeypatch):
    payload = load_fixture("nsw_contaminated_sites_botany.json")
    payload = copy.deepcopy(payload)
    payload.pop("exceededTransferLimit", None)
    install_responses(monkeypatch, [FakeResponse(payload)])

    sites = nsw_sites.sites_near(*BOTANY, 5000)
    assert sites is not None and sites
    assert [s["distance_m"] for s in sites] == sorted(s["distance_m"] for s in sites)
    first = sites[0]
    assert set(first) >= {"name", "activity_type", "distance_m", "lat", "lng"}
    assert first["activity_type"]


def test_nsw_strips_trailing_space_in_management_class(monkeypatch):
    """Upstream ships 'Regulation under CLM Act not required ' with a trailing
    space. Untrimmed it breaks every equality check downstream."""
    payload = copy.deepcopy(load_fixture("nsw_contaminated_sites_botany.json"))
    payload.pop("exceededTransferLimit", None)
    raw_classes = [f["attributes"]["managementclass"] for f in payload["features"]]
    assert any(c != c.strip() for c in raw_classes), "fixture lost the trap"

    install_responses(monkeypatch, [FakeResponse(payload)])
    sites = nsw_sites.sites_near(*BOTANY, 5000)
    assert sites
    assert all(s["management_class"] == s["management_class"].strip() for s in sites)
    assert all(not s["management_class"].endswith(" ") for s in sites)


def test_nsw_empty_register_is_a_list_not_none(monkeypatch):
    install_responses(monkeypatch, [FakeResponse({"features": []})])
    assert nsw_sites.sites_near(*BOTANY, 2000) == []


def test_nsw_error_body_under_http_200_fails_closed(monkeypatch):
    install_responses(monkeypatch, [
        FakeResponse({"error": {"code": 500, "message": "unable to complete"}})])
    assert nsw_sites.sites_near(*BOTANY, 2000) is None


def test_nsw_non_200_fails_closed(monkeypatch):
    install_responses(monkeypatch, [FakeResponse({}, status_code=503)])
    assert nsw_sites.sites_near(*BOTANY, 2000) is None


def test_nsw_missing_coordinates_fail_closed(monkeypatch):
    payload = copy.deepcopy(load_fixture("nsw_contaminated_sites_botany.json"))
    payload.pop("exceededTransferLimit", None)
    payload["features"][0]["attributes"]["latitude"] = None
    install_responses(monkeypatch, [FakeResponse(payload)])
    assert nsw_sites.sites_near(*BOTANY, 2000) is None


def test_nsw_failure_and_empty_are_distinguishable(monkeypatch):
    install_responses(monkeypatch, [FakeResponse({"features": []})])
    empty = nsw_sites.sites_near(*BOTANY, 2000)
    nsw_sites.clear_cache()
    install_responses(monkeypatch, [FakeResponse({}, status_code=500)])
    failed = nsw_sites.sites_near(*BOTANY, 2000)
    assert empty == [] and failed is None
    assert empty is not failed


def test_nsw_radius_excludes_distant_sites(monkeypatch):
    payload = copy.deepcopy(load_fixture("nsw_contaminated_sites_botany.json"))
    payload.pop("exceededTransferLimit", None)
    install_responses(monkeypatch, [FakeResponse(payload)])
    near = nsw_sites.sites_near(*BOTANY, 5000)
    nsw_sites.clear_cache()
    install_responses(monkeypatch, [FakeResponse(payload)])
    assert nsw_sites.sites_near(*MELBOURNE, 5000) == []
    assert near


def test_nsw_cache_serves_second_call_without_a_request(monkeypatch):
    payload = copy.deepcopy(load_fixture("nsw_contaminated_sites_botany.json"))
    payload.pop("exceededTransferLimit", None)
    calls = install_responses(monkeypatch, [FakeResponse(payload)])
    nsw_sites.sites_near(*BOTANY, 5000)
    assert len(calls) == 1
    nsw_sites.sites_near(*BOTANY, 5000)
    assert len(calls) == 1, "second query should come from the 24h cache"


def test_nsw_expired_cache_refetches(monkeypatch):
    payload = copy.deepcopy(load_fixture("nsw_contaminated_sites_botany.json"))
    payload.pop("exceededTransferLimit", None)
    calls = install_responses(monkeypatch, [FakeResponse(payload)])
    nsw_sites.sites_near(*BOTANY, 5000)
    stale_sites, stale_ts = nsw_sites._cache
    nsw_sites._cache = (stale_sites, stale_ts - nsw_sites.CACHE_TTL_S - 1)
    nsw_sites.sites_near(*BOTANY, 5000)
    assert len(calls) == 2


def test_nsw_pages_while_transfer_limit_is_exceeded(monkeypatch):
    payload = copy.deepcopy(load_fixture("nsw_contaminated_sites_botany.json"))
    page1 = copy.deepcopy(payload)
    page1["exceededTransferLimit"] = True
    page2 = copy.deepcopy(payload)
    page2.pop("exceededTransferLimit", None)
    page2["features"] = page2["features"][:1]
    calls = install_responses(monkeypatch, [FakeResponse(page1), FakeResponse(page2)])
    sites = nsw_sites.all_sites()
    assert sites is not None
    assert len(sites) == len(payload["features"]) + 1
    assert calls[1]["params"]["resultOffset"] == len(payload["features"])


# ---------------------------------------------------------------------------
# VIC WFS: Sands & McDougall
# ---------------------------------------------------------------------------

def _sands_payload(features):
    return {"type": "FeatureCollection", "features": features,
            "numberMatched": len(features), "numberReturned": len(features)}


def _sands_yearly_clones(years):
    """One shopfront listed once per directory year, which is exactly how the
    upstream layer is shaped and the reason de-duplication exists."""
    base = load_fixture("vic_sands_melbourne_cbd.json")["features"][0]
    out = []
    for year in years:
        clone = copy.deepcopy(base)
        clone["properties"]["directory"] = year
        clone["id"] = f"{clone['id']}-{year}"
        out.append(clone)
    return out


def test_sands_bbox_uses_urn_lat_lon_axis_order(monkeypatch):
    """WFS 2.0 with a urn CRS takes bbox as lat,lon. Swapping it returns
    nothing at all, which would read as an empty register."""
    calls = install_responses(monkeypatch, [FakeResponse(_sands_payload([]))])
    lat, lng = MELBOURNE
    vic_wfs.sands_near(lat, lng, 200)
    bbox = calls[0]["params"]["bbox"]
    south, west, north, east, crs = bbox.split(",")
    south, west, north, east = float(south), float(west), float(north), float(east)
    assert crs == "urn:ogc:def:crs:EPSG::4326"
    # the trap: with a urn CRS the axis order is lat,lon. Slots 0 and 2 must
    # bracket the latitude and slots 1 and 3 the longitude, never the reverse.
    assert south < lat < north, f"slots 0/2 must bracket lat {lat}: {bbox}"
    assert west < lng < east, f"slots 1/3 must bracket lng {lng}: {bbox}"
    assert south < 0 and north < 0, "Australian latitudes are negative"
    assert west > 100 and east > 100, "Australian longitudes are around 145"


def test_sands_deduplicates_by_address_and_business_type(monkeypatch):
    years = [1896, 1930, 1974]
    features = _sands_yearly_clones(years)
    install_responses(monkeypatch, [FakeResponse(_sands_payload(features))])
    base = features[0]["geometry"]["coordinates"]
    result = vic_wfs.sands_near(base[1], base[0], 200)
    assert result is not None
    assert len(result) == 1, "one shopfront listed in 3 directories is one site"
    assert result[0]["directories"] == years


def test_sands_keeps_distinct_business_types_at_one_address(monkeypatch):
    features = _sands_yearly_clones([1896, 1930])
    features[1]["properties"]["business_type"] = "Dry Cleaners"
    install_responses(monkeypatch, [FakeResponse(_sands_payload(features))])
    base = features[0]["geometry"]["coordinates"]
    result = vic_wfs.sands_near(base[1], base[0], 200)
    assert result is not None
    assert len(result) == 2
    assert {r["business_type"] for r in result} == {
        features[0]["properties"]["business_type"], "Dry Cleaners"}


def test_sands_never_returns_business_name(monkeypatch):
    """Privacy red line: a sole trader's business name is a person's name, and
    the raw address column embeds it too."""
    features = _sands_yearly_clones([1896])
    features[0]["properties"]["business_name"] = "Bacon, Naomi"
    features[0]["properties"]["address"] = "Bacon, Naomi, 174 Russell St"
    install_responses(monkeypatch, [FakeResponse(_sands_payload(features))])
    base = features[0]["geometry"]["coordinates"]
    result = vic_wfs.sands_near(base[1], base[0], 200)
    assert result and "business_name" not in result[0]
    assert "Bacon" not in json.dumps(result)


def test_sands_empty_bbox_is_a_list(monkeypatch):
    install_responses(monkeypatch, [FakeResponse(_sands_payload([]))])
    assert vic_wfs.sands_near(*MELBOURNE, 200) == []


def test_sands_wfs_exception_body_fails_closed(monkeypatch):
    install_responses(monkeypatch, [
        FakeResponse({"exceptions": [{"exceptionCode": "NoApplicableCode"}]})])
    assert vic_wfs.sands_near(*MELBOURNE, 200) is None


def test_sands_refuses_a_bbox_over_the_feature_ceiling(monkeypatch):
    """The real captured CBD response reports numberMatched 66,293. A partial
    read of a register has not read the register."""
    payload = load_fixture("vic_sands_melbourne_cbd.json")
    assert payload["numberMatched"] > vic_wfs._SANDS_MAX_FEATURES
    calls = install_responses(monkeypatch, [FakeResponse(payload)])
    assert vic_wfs.sands_near(*MELBOURNE, 200) is None
    # and it must bail on the first response rather than paging its way to
    # the ceiling, otherwise an oversized bbox costs eight round trips
    assert len(calls) == 1


def test_sands_paging_sends_sortby_and_startindex(monkeypatch):
    """GeoServer rejects a paged read of this view without an explicit sort."""
    features = _sands_yearly_clones([1896, 1930])
    page1 = _sands_payload(features)
    page1["numberMatched"] = 4
    page2 = _sands_payload(_sands_yearly_clones([1955, 1974]))
    page2["numberMatched"] = 4
    monkeypatch.setattr(vic_wfs, "_SANDS_PAGE", 2)
    calls = install_responses(monkeypatch, [FakeResponse(page1), FakeResponse(page2)])
    base = features[0]["geometry"]["coordinates"]
    result = vic_wfs.sands_near(base[1], base[0], 200)
    assert result is not None and len(result) == 1
    assert result[0]["directories"] == [1896, 1930, 1955, 1974]
    assert calls[0]["params"]["sortBy"] == vic_wfs._SANDS_SORT_BY
    assert calls[1]["params"]["startIndex"] == 2


def test_sands_empty_intermediate_page_fails_closed(monkeypatch):
    features = _sands_yearly_clones([1896, 1930])
    page1 = _sands_payload(features)
    page1["numberMatched"] = 4
    page2 = _sands_payload([])
    page2["numberMatched"] = 4
    monkeypatch.setattr(vic_wfs, "_SANDS_PAGE", 2)
    install_responses(monkeypatch, [FakeResponse(page1), FakeResponse(page2)])
    base = features[0]["geometry"]["coordinates"]
    assert vic_wfs.sands_near(base[1], base[0], 200) is None


# ---------------------------------------------------------------------------
# VIC WFS: Landfill Register and GQRUZ
# ---------------------------------------------------------------------------

def test_vlr_parses_points_and_polygons(monkeypatch):
    poly = load_fixture("vic_vlr_polygon_melbourne.json")
    point = load_fixture("vic_vlr_point_melbourne.json")
    install_responses_by_layer(monkeypatch, {
        vic_wfs.LAYER_VLR_POLYGON: FakeResponse(poly),
        vic_wfs.LAYER_VLR_POINT: FakeResponse(point),
    })
    sites = vic_wfs.landfills_near(*MELBOURNE, 20000)
    assert sites is not None and sites
    assert {s["geom"] for s in sites} == {"polygon", "point"}
    assert [s["distance_m"] for s in sites] == sorted(s["distance_m"] for s in sites)
    assert any(s["operating_status"] == "Closed" for s in sites)


def test_vlr_folds_not_available_sentinel_to_none(monkeypatch):
    """Upstream writes the literal string 'Not available' instead of null."""
    point = load_fixture("vic_vlr_point_melbourne.json")
    raw = [f["properties"]["landfill_name"] for f in point["features"]]
    assert "Not available" in raw, "fixture lost the trap"
    install_responses_by_layer(monkeypatch, {
        vic_wfs.LAYER_VLR_POLYGON: FakeResponse({"features": []}),
        vic_wfs.LAYER_VLR_POINT: FakeResponse(point),
    })
    sites = vic_wfs.landfills_near(*MELBOURNE, 20000)
    assert sites is not None
    assert all(s["address"] != "Not available" for s in sites)
    assert all(s["name"] != "Not available" for s in sites)
    assert any(s["address"] is None or s["estimated_year_of_closure"] is None
               for s in sites)


def test_vlr_empty_is_a_list_and_failure_is_none(monkeypatch):
    install_responses(monkeypatch, [
        FakeResponse({"features": []}), FakeResponse({"features": []})])
    assert vic_wfs.landfills_near(*MELBOURNE, 2000) == []
    install_responses(monkeypatch, [FakeResponse({}, status_code=502)])
    assert vic_wfs.landfills_near(*MELBOURNE, 2000) is None


def test_gqruz_returns_site_history_and_split_restricted_uses(monkeypatch):
    payload = load_fixture("vic_gqruz_polygon_melbourne.json")
    install_responses(monkeypatch, [FakeResponse(payload)])
    zones = vic_wfs.gqruz_near(-37.82867, 144.94601, 20000)
    assert zones is not None and zones
    target = [z for z in zones if z["reference_number"] == "GQR001042"]
    assert target, "expected the South Melbourne zone from the fixture"
    zone = target[0]
    assert zone["site_history"] == "Commercial/Industrial"
    assert isinstance(zone["restricted_uses"], list)
    assert "Drinking water (desirable quality)" in zone["restricted_uses"]
    assert all(";" not in use for use in zone["restricted_uses"])


def test_gqruz_flags_a_point_inside_the_zone(monkeypatch):
    payload = load_fixture("vic_gqruz_polygon_melbourne.json")
    install_responses(monkeypatch, [FakeResponse(payload)])
    inside = vic_wfs.gqruz_near(-37.82867, 144.94601, 20000)
    hit = [z for z in inside if z["reference_number"] == "GQR001042"][0]
    assert hit["inside"] is True and hit["distance_m"] == 0


def test_gqruz_empty_and_failure_stay_distinct(monkeypatch):
    install_responses(monkeypatch, [FakeResponse({"features": []})])
    assert vic_wfs.gqruz_near(*MELBOURNE, 500) == []
    install_responses(monkeypatch, [FakeResponse({"exceptions": ["boom"]})])
    assert vic_wfs.gqruz_near(*MELBOURNE, 500) is None


# ---------------------------------------------------------------------------
# SA Groundwater Prohibition Areas
# ---------------------------------------------------------------------------

def _write_gpa_cache(tmp_path, payload):
    path = tmp_path / "sa_gpa.geojson"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_sa_point_in_polygon_hit(tmp_path):
    payload = load_fixture("sa_gpa_edwardstown.json")
    path = _write_gpa_cache(tmp_path, payload)
    hits = sa_gpa.areas_near(*EDWARDSTOWN, 0, cache_path=path)
    assert hits is not None and len(hits) == 1
    assert hits[0]["site"] == "Edwardstown"
    assert hits[0]["inside"] is True and hits[0]["distance_m"] == 0
    assert hits[0]["depth"] == "Up to 26m below ground level"


def test_sa_point_outside_every_polygon_is_empty(tmp_path):
    payload = load_fixture("sa_gpa_edwardstown.json")
    path = _write_gpa_cache(tmp_path, payload)
    assert sa_gpa.areas_near(*MELBOURNE, 500, cache_path=path) == []


def test_sa_nearby_but_outside_is_reported_as_not_inside(tmp_path):
    payload = load_fixture("sa_gpa_edwardstown.json")
    path = _write_gpa_cache(tmp_path, payload)
    hits = sa_gpa.areas_near(-34.9700, 138.5700, 1000, cache_path=path)
    assert hits is not None and hits
    assert all(h["distance_m"] > 0 for h in hits)
    assert all(h["inside"] is False for h in hits)


def test_sa_download_fills_the_cache(monkeypatch, tmp_path):
    payload = load_fixture("sa_gpa_edwardstown.json")
    path = str(tmp_path / "nested" / "sa_gpa.geojson")
    calls = install_responses(monkeypatch, [FakeResponse(payload)])
    hits = sa_gpa.areas_near(*EDWARDSTOWN, 0, cache_path=path)
    assert hits and hits[0]["site"] == "Edwardstown"
    assert len(calls) == 1
    assert os.path.exists(path)


def test_sa_download_failure_without_cache_fails_closed(monkeypatch, tmp_path):
    path = str(tmp_path / "sa_gpa.geojson")
    install_responses(monkeypatch, [FakeResponse(b"", status_code=404)])
    assert sa_gpa.areas_near(*EDWARDSTOWN, 0, cache_path=path) is None
    assert not os.path.exists(path)


def test_sa_download_failure_with_cache_serves_the_cache(monkeypatch, tmp_path):
    payload = load_fixture("sa_gpa_edwardstown.json")
    path = _write_gpa_cache(tmp_path, payload)
    os.utime(path, (0, 0))  # force the TTL to look expired
    install_responses(monkeypatch, [FakeResponse(b"", status_code=500)])
    hits = sa_gpa.areas_near(*EDWARDSTOWN, 0, cache_path=path)
    assert hits is not None and hits[0]["site"] == "Edwardstown"


def test_sa_unrecognised_structure_fails_closed(tmp_path):
    path = _write_gpa_cache(tmp_path, {"type": "FeatureCollection",
                                       "features": "not a list"})
    assert sa_gpa.areas_near(*EDWARDSTOWN, 0, cache_path=path) is None


def test_sa_broken_geometry_fails_closed(tmp_path):
    payload = copy.deepcopy(load_fixture("sa_gpa_edwardstown.json"))
    payload["features"][0]["geometry"]["coordinates"] = "junk"
    path = _write_gpa_cache(tmp_path, payload)
    assert sa_gpa.areas_near(*EDWARDSTOWN, 0, cache_path=path) is None


# ---------------------------------------------------------------------------
# GA Waste Management Facilities
# ---------------------------------------------------------------------------

def test_ga_parses_landfills(monkeypatch):
    payload = load_fixture("ga_waste_landfill_botany.json")
    install_responses(monkeypatch, [FakeResponse(payload)])
    sites = ga_waste.landfills_near(*BOTANY, 20000)
    assert sites is not None and len(sites) == 2
    assert [s["distance_m"] for s in sites] == sorted(s["distance_m"] for s in sites)
    assert set(sites[0]) >= {"name", "type", "status", "distance_m"}
    assert all("LANDFILL" in s["type"].upper() for s in sites)
    assert sites[0]["status"] == "OPERATIONAL"
    assert all("owner" not in site for site in sites)


def test_ga_where_clause_uses_like_not_exact_match(monkeypatch):
    """The infrastructure type values carry an EN DASH
    ('LANDFILL - PUTRESCIBLE' with U+2013), so equality matching silently
    drops every landfill in the country."""
    payload = load_fixture("ga_waste_landfill_botany.json")
    calls = install_responses(monkeypatch, [FakeResponse(payload)])
    ga_waste.landfills_near(*BOTANY, 2000)
    where = calls[0]["params"]["where"]
    assert "LIKE" in where.upper()
    assert "%LANDFILL%" in where
    assert "facility_owner" not in calls[0]["params"]["outFields"]
    types = [f["attributes"]["facility_infrastructure_type"]
             for f in payload["features"]]
    assert any("–" in t for t in types), "fixture lost the en dash trap"


def test_ga_filters_out_non_landfill_records(monkeypatch):
    """Belt and braces: 1,620 of the 6,453 records are supermarket soft
    plastics bins. If the server-side WHERE were ever dropped they must still
    not reach the score."""
    payload = load_fixture("ga_waste_melbourne.json")
    assert any("LANDFILL" not in
               (f["attributes"]["facility_infrastructure_type"] or "").upper()
               for f in payload["features"])
    install_responses(monkeypatch, [FakeResponse(payload)])
    sites = ga_waste.landfills_near(*MELBOURNE, 200000)
    assert sites is not None
    assert all("LANDFILL" in s["type"].upper() for s in sites)


def test_ga_zero_results_is_a_list(monkeypatch):
    payload = load_fixture("ga_waste_landfill_melbourne.json")
    assert payload["features"] == []
    install_responses(monkeypatch, [FakeResponse(payload)])
    assert ga_waste.landfills_near(*MELBOURNE, 2000) == []


def test_ga_error_body_under_http_200_fails_closed(monkeypatch):
    install_responses(monkeypatch, [
        FakeResponse({"error": {"code": 400, "message": "Invalid where"}})])
    assert ga_waste.landfills_near(*BOTANY, 2000) is None


def test_ga_non_200_fails_closed(monkeypatch):
    install_responses(monkeypatch, [FakeResponse({}, status_code=500)])
    assert ga_waste.landfills_near(*BOTANY, 2000) is None


def test_ga_zero_results_and_failure_are_distinguishable(monkeypatch):
    install_responses(monkeypatch, [
        FakeResponse(load_fixture("ga_waste_landfill_melbourne.json"))])
    empty = ga_waste.landfills_near(*MELBOURNE, 2000)
    install_responses(monkeypatch, [FakeResponse({}, status_code=500)])
    failed = ga_waste.landfills_near(*MELBOURNE, 2000)
    assert empty == [] and failed is None


def test_ga_missing_geometry_fails_closed(monkeypatch):
    payload = copy.deepcopy(load_fixture("ga_waste_landfill_botany.json"))
    payload["features"][0].pop("geometry")
    install_responses(monkeypatch, [FakeResponse(payload)])
    assert ga_waste.landfills_near(*BOTANY, 20000) is None
