"""Aircraft ANEF: the right airfield, at the right level.

This component had no regression cover at all, which is how the 2026-07-25 bug
survived: the upstream Defence KML hangs Amberley's contours under Townsville's
placemark and Williamtown's under Amberley's, so a property near Brisbane was
reported as "RAAF Base Townsville - ANEF 40+". Wrong base, and a level Amberley
does not even reach, on a field that reads as "commercial or industrial only"
in a planning report.

It went unnoticed for a second reason: none of the twelve sandbox addresses a
customer evaluates sits inside any contour, so aircraft_noise never produced a
result on the demo set and a totally broken layer would have looked identical
to a working one. Foundit said as much on 2026-07-16 ("aircraft-noise LGA, zone
code and ANEF values ... null on every one of the 12").

Every contour coordinate below was verified against production on 2026-07-25/26
after the converter was re-run. `defence_anef.geojson` is a gitignored build
product, so data-backed contour tests skip where it is absent rather than
failing a clean checkout. Pure query-assembly contract tests still run.
"""
import pytest

from property_scores.common.config import data_path
from property_scores.noise import aircraft

requires_anef_data = pytest.mark.skipif(
    not data_path("defence_anef.geojson").exists(),
    reason="defence_anef.geojson is a gitignored build product "
           "(scripts/convert_defence_anef.py)")


def _score(lat, lng):
    return aircraft.aircraft_noise_penalty(lat, lng)


# (label, lat, lng, expected airfield substring, expected zone_code)
INSIDE_CONTOUR = [
    # The two the 2026-07-25 converter fix re-attributed by geometry. Amberley
    # was reported as Townsville and Williamtown as Amberley.
    ("RAAF Amberley", -27.63433, 152.70720, "Amberley", "ANEF 40+"),
    ("RAAF Williamtown", -32.79319, 151.83262, "Williamtown", "ANEF 40+"),
    # Sydney Kingsford Smith used to sit here (Sydenham, "ANEF 35-40", from a
    # contour digitised off the 2033 Master Plan). That source was removed on
    # 2026-10-22 (no redistribution licence, superseded by the 2045 ANEF); see
    # the Sydney honesty-gate tests below.
]


@pytest.mark.parametrize("label,lat,lng,airfield,zone", INSIDE_CONTOUR)
@requires_anef_data
def test_contour_reports_the_airfield_it_is_actually_near(label, lat, lng,
                                                          airfield, zone):
    r = _score(lat, lng)
    assert r["zone_code"] == zone, f"{label}: {r}"
    assert airfield in (r.get("zone_desc") or ""), (
        f"{label}: attributed to the wrong airfield: {r.get('zone_desc')!r}")
    assert (r.get("penalty_db") or 0) > 0, f"{label} inside a contour scored no penalty"


@requires_anef_data
def test_amberley_is_not_townsville():
    """The exact regression. Townsville is ~1,100 km from this point."""
    desc = _score(-27.63433, 152.70720).get("zone_desc") or ""
    assert "Townsville" not in desc, desc


@requires_anef_data
def test_williamtown_spelling_reaches_the_customer_correctly():
    """The upstream KML spells it 'Williamown'; zone_desc is customer-visible."""
    desc = _score(-32.79319, 151.83262).get("zone_desc") or ""
    assert "Williamown" not in desc, desc


@requires_anef_data
def test_outside_any_contour_says_so_without_inventing_a_level():
    """Melbourne CBD. A miss must not read as a quiet ANEF band."""
    r = _score(-37.8136, 144.9631)
    assert r.get("assessment") in ("no_overlay", "not_assessed"), r
    assert r.get("zone_code") is None, r
    assert (r.get("penalty_db") or 0) == 0, r


def test_lga_comes_only_from_the_victorian_source(monkeypatch, request):
    """Foundit asked why `lga` was always null. It is populated by the VicPlan
    query alone, so it stays null elsewhere even inside a contour. Documenting
    that here stops it being read as a bug again. Stub every external/data
    query so this contract test never depends on the live VicPlan service."""
    monkeypatch.setattr(aircraft, "_query_vic", lambda lat, lng: {
        "anef_min": 20,
        "zone_code": "MAEO2",
        "airfield": "Melbourne Airport",
        "source": "vicplan",
        "lga": "HUME",
    })
    monkeypatch.setattr(aircraft, "_query_nsw", lambda lat, lng: {
        "anef_min": 25,
        "zone_code": "25 - 30",
        "airfield": "Western Sydney Airport",
        "source": "nsw_sepp",
    })
    monkeypatch.setattr(aircraft, "_query_gold_coast", lambda lat, lng: None)
    monkeypatch.setattr(aircraft, "_query_defence", lambda lat, lng: None)
    aircraft._aircraft_cached.cache_clear()
    request.addfinalizer(aircraft._aircraft_cached.cache_clear)

    vic = _score(-37.68776811, 144.87140045)
    assert vic.get("zone_code") == "MAEO2", vic
    assert vic.get("lga") == "HUME", vic
    assert vic.get("source") == "vicplan", vic

    # Badgerys Creek, beside Western Sydney Airport, outside the SYD box.
    nsw = _score(-33.88, 150.72)
    assert nsw.get("zone_code"), "expected a contour hit to compare against"
    assert nsw.get("source") == "nsw_sepp", nsw
    assert nsw.get("lga") is None, nsw


# ---------------------------------------------------------------------------
# Sydney Kingsford Smith: source removed 2026-10-22, honesty gate in its place
# ---------------------------------------------------------------------------

def _stub_all_sources(monkeypatch, request, *, nsw=None, gc=None, defence=None,
                      vic=None):
    """Every external/data query stubbed, so these run on a clean checkout
    with no network and no gitignored data. Any attribute the old SYD path
    used is NOT stubbed: if someone restores it, it runs for real."""
    monkeypatch.setattr(aircraft, "_query_vic", lambda lat, lng: vic)
    monkeypatch.setattr(aircraft, "_query_nsw", lambda lat, lng: nsw)
    monkeypatch.setattr(aircraft, "_query_qld", lambda lat, lng: None)
    monkeypatch.setattr(aircraft, "_query_wa", lambda lat, lng: None)
    monkeypatch.setattr(aircraft, "_query_gold_coast", lambda lat, lng: gc)
    monkeypatch.setattr(aircraft, "_query_defence", lambda lat, lng: defence)
    aircraft._aircraft_cached.cache_clear()
    request.addfinalizer(aircraft._aircraft_cached.cache_clear)


SYD_GATED = [
    # Both sat inside the old digitised ANEF 35-40 / 30-35 bands.
    ("Sydenham", -33.9152, 151.1688),
    ("St Peters", -33.913, 151.178),
    ("Tempe", -33.9236, 151.1600),
]


@pytest.mark.parametrize("label,lat,lng", SYD_GATED)
def test_sydney_basin_reports_not_assessed_not_zero(monkeypatch, request,
                                                    label, lat, lng):
    _stub_all_sources(monkeypatch, request)
    r = _score(lat, lng)
    assert r["penalty_db"] == 0, f"{label}: {r}"
    assert r["assessment"] == "not_assessed", f"{label}: {r}"
    assert r["zone_desc"] == "Sydney Kingsford Smith ANEF not available", r
    assert "not available under our data licences" in r["impact"], r
    assert "not as zero" in r["impact"], r
    assert r["source"] == "national", r
    assert r["zone_code"] is None and r["anef_min"] is None, r
    assert "syd_masterplan_2033" not in repr(r), r


@pytest.mark.parametrize("label,lat,lng", SYD_GATED)
def test_sydney_basin_gate_holds_with_real_data_present(label, lat, lng, request):
    """Same points, no stubs: the real Defence layer (when built) and the live
    NSW/Gold Coast services. Whatever is on disk, syd_anef.geojson included,
    must not produce a Kingsford Smith contour any more."""
    aircraft._aircraft_cached.cache_clear()
    request.addfinalizer(aircraft._aircraft_cached.cache_clear)
    r = _score(lat, lng)
    assert "syd_masterplan_2033" not in repr(r), r
    assert "Kingsford Smith" not in (r.get("zone_code") or ""), r
    assert r["penalty_db"] == 0, f"{label}: {r}"
    assert r["assessment"] == "not_assessed", f"{label}: {r}"


def test_sydney_module_no_longer_reads_the_digitised_contour():
    assert not hasattr(aircraft, "_query_syd")
    assert not hasattr(aircraft, "_load_syd")
    import inspect
    assert "syd_anef.geojson" not in inspect.getsource(aircraft)


def test_any_real_hit_inside_the_sydney_box_wins_over_the_gate(monkeypatch,
                                                                request):
    """The gate is for a MISS only. A Defence or SEPP hit in the box is a real
    contour and must be reported as one."""
    _stub_all_sources(monkeypatch, request, defence={
        "anef_min": 25, "zone_code": "ANEF 25+",
        "airfield": "Test Defence Field", "source": "defence"})
    r = _score(-33.9152, 151.1688)
    assert r["source"] == "defence", r
    assert r["penalty_db"] > 0, r
    assert r.get("assessment") != "not_assessed", r


def test_rest_of_nsw_keeps_its_confident_zero(monkeypatch, request):
    """Outside the box nothing changed: a NSW miss is still no_overlay."""
    _stub_all_sources(monkeypatch, request)
    for lat, lng in ((-33.88, 150.72),      # Badgerys Creek, west of the box
                     (-33.75, 151.20),      # north of the box (lat > -33.8)
                     (-32.93, 151.78)):     # Newcastle
        r = _score(lat, lng)
        assert r["assessment"] == "no_overlay", (lat, lng, r)
        assert r["penalty_db"] == 0, r


def test_box_edges_are_inclusive_and_nsw_only(monkeypatch, request):
    _stub_all_sources(monkeypatch, request)
    lat_min, lat_max, lng_min, lng_max = aircraft._SYD_BBOX
    assert (lat_min, lat_max, lng_min, lng_max) == (-34.1, -33.8, 151.0, 151.3)
    assert _score(lat_max, lng_min)["assessment"] == "not_assessed"
    assert _score(lat_min, lng_max)["assessment"] == "not_assessed"
    assert _score(lat_max + 0.01, 151.15)["assessment"] == "no_overlay"


@pytest.mark.parametrize("label,lat,lng", [
    ("Western Sydney", -33.88, 150.72),
    ("RAAF Amberley", -27.63433, 152.70720),
    ("RAAF Williamtown", -32.79319, 151.83262),
])
def test_hits_outside_the_box_are_reported_unchanged(monkeypatch, request,
                                                     label, lat, lng):
    hit = {"anef_min": 30, "zone_code": "Z", "airfield": label,
           "source": "nsw_sepp" if label == "Western Sydney" else "defence"}
    if label == "Western Sydney":
        _stub_all_sources(monkeypatch, request, nsw=hit)
    else:
        _stub_all_sources(monkeypatch, request, defence=hit)
    r = _score(lat, lng)
    assert r["penalty_db"] == 30.0, r
    assert r["source"] == hit["source"], r
    assert r["zone_desc"].startswith(label), r


# ---------------------------------------------------------------------------
# Melbourne-inner grid: why _MODEL_DATE was not bumped (see score.py)
# ---------------------------------------------------------------------------

def _melbourne_inner_region():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parent.parent / "scripts" / "precompute_noise.py"
    spec = importlib.util.spec_from_file_location("_precompute_noise", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.REGIONS["melbourne-inner"]


def test_melbourne_inner_grid_does_not_touch_the_sydney_gate():
    """The only grid production serves is melbourne-inner. If it ever reached
    the SYD box, the 2026-10-22 removal would have changed baked numbers and
    the date would have had to move."""
    from property_scores.common.au_state import detect_state
    g = _melbourne_inner_region()
    lat_min, lat_max, lng_min, lng_max = aircraft._SYD_BBOX
    # Pad by a full cell: quincunx samples spread within one step of a centre.
    pad = g["step"]
    disjoint = (g["lat_max"] + pad < lat_min or g["lat_min"] - pad > lat_max
                or g["lng_max"] + pad < lng_min or g["lng_min"] - pad > lng_max)
    assert disjoint, (g, aircraft._SYD_BBOX)
    for lat in (g["lat_min"], g["lat_max"]):
        for lng in (g["lng_min"], g["lng_max"]):
            assert detect_state(lat, lng) == "VIC", (lat, lng)


def test_melbourne_inner_cells_score_identically_without_syd(monkeypatch,
                                                             request):
    """Every melbourne-inner sample comes back exactly as the VicPlan / Defence
    stubs say, through the same code path as before the removal: the NSW branch
    and the Sydney gate never run in VIC."""
    import numpy as np
    g = _melbourne_inner_region()
    vic_hit = {"anef_min": 20, "zone_code": "MAEO2",
               "airfield": "Melbourne Airport", "source": "vicplan",
               "lga": "MORELAND"}
    calls = []
    real_gate = aircraft._in_syd_bbox
    monkeypatch.setattr(aircraft, "_in_syd_bbox",
                        lambda lat, lng: calls.append((lat, lng)) or real_gate(lat, lng))
    lats = np.arange(g["lat_min"], g["lat_max"], g["step"] * 7)
    lngs = np.arange(g["lng_min"], g["lng_max"], g["step"] * 7)
    for hit in (None, vic_hit):
        _stub_all_sources(monkeypatch, request, vic=hit)
        for lat in lats:
            for lng in lngs:
                r = _score(float(lat), float(lng))
                if hit is None:
                    assert r == {
                        "penalty_db": 0.0, "assessment": "no_overlay",
                        "zone_code": None,
                        "zone_desc": "Outside airport noise overlay",
                        "anef_min": None, "anef_max": None, "lga": None,
                        "impact": "No aircraft noise overlay applies to this location.",
                        "source": "national", "airport_type": None}, r
                else:
                    assert r["penalty_db"] == 20.0 and r["source"] == "vicplan", r
                    assert r["lga"] == "MORELAND", r
    assert calls == [], "the Sydney gate was consulted for a Melbourne point"
