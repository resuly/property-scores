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

Every coordinate below was verified against production on 2026-07-25/26 after
the converter was re-run. `defence_anef.geojson` is a gitignored build product,
so these skip where it is absent rather than failing a clean checkout.
"""
import pytest

from property_scores.common.config import data_path
from property_scores.noise import aircraft

pytestmark = pytest.mark.skipif(
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
    # Civilian digitised contour, unaffected by the Defence KML problem, and
    # now a sandbox fixture so a customer can see the component work.
    ("Sydney KSA (Sydenham)", -33.91518436, 151.16875651,
     "Kingsford Smith", "ANEF 35-40"),
]


@pytest.mark.parametrize("label,lat,lng,airfield,zone", INSIDE_CONTOUR)
def test_contour_reports_the_airfield_it_is_actually_near(label, lat, lng,
                                                          airfield, zone):
    r = _score(lat, lng)
    assert r["zone_code"] == zone, f"{label}: {r}"
    assert airfield in (r.get("zone_desc") or ""), (
        f"{label}: attributed to the wrong airfield: {r.get('zone_desc')!r}")
    assert (r.get("penalty_db") or 0) > 0, f"{label} inside a contour scored no penalty"


def test_amberley_is_not_townsville():
    """The exact regression. Townsville is ~1,100 km from this point."""
    desc = _score(-27.63433, 152.70720).get("zone_desc") or ""
    assert "Townsville" not in desc, desc


def test_williamtown_spelling_reaches_the_customer_correctly():
    """The upstream KML spells it 'Williamown'; zone_desc is customer-visible."""
    desc = _score(-32.79319, 151.83262).get("zone_desc") or ""
    assert "Williamown" not in desc, desc


def test_outside_any_contour_says_so_without_inventing_a_level():
    """Melbourne CBD. A miss must not read as a quiet ANEF band."""
    r = _score(-37.8136, 144.9631)
    assert r.get("assessment") in ("no_overlay", "not_assessed"), r
    assert r.get("zone_code") is None, r
    assert (r.get("penalty_db") or 0) == 0, r


def test_lga_comes_only_from_the_victorian_source():
    """Foundit asked why `lga` was always null. It is populated by the VicPlan
    query alone, so it stays null elsewhere even inside a contour. Documenting
    that here stops it being read as a bug again."""
    vic = _score(-37.68776811, 144.87140045)  # Melbourne Airport environs
    assert vic.get("zone_code") == "MAEO2", vic
    assert vic.get("lga") == "HUME", vic
    assert vic.get("source") == "vicplan", vic

    nsw = _score(-33.91518436, 151.16875651)  # inside a contour, not Victoria
    assert nsw.get("zone_code"), "expected a contour hit to compare against"
    assert nsw.get("lga") is None, nsw
