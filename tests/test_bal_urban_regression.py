"""Regression: urban addresses read BAL-FZ in production (2026-11-18).

Replays the REAL intermediates recorded read-only on production for three
addresses (tests/fixtures/bal_urban_regression_20261118.json): the 150 m ESA
WorldCover 10m grid, every DEM elevation / resolution read the screen made,
the official overlay tuple and the site slope. Nothing here is synthetic.

  Darwin CBD apartment  NT, no official layer. Vegetated escarpment 45-100 m
                        NE (5 m LiDAR: ~10 m fall over 30 m). Ten pixels
                        read >20 deg under the vegetation; v1 turned each into
                        BAL-FZ regardless of distance (53-97 m).
  Campbell ACT unit     officially OUTSIDE the ACT Bushfire Prone Area; the
                        site pixel itself is WorldCover tree cover (street
                        trees/gardens), so v1 said Forest at 0 m -> BAL-FZ.
  Katoomba cliff edge   NSW Vegetation Buffer, continuous forest at the site:
                        BAL-FZ is the right screen and must not move.

Expected v2 values are worked from the pinned GA distance tables by hand
(see each test), not from the implementation.
"""

import json
import math
from pathlib import Path
from unittest import mock

import pytest

from property_scores.bal_prescreen.prescreen import _bal_rank, bal_prescreen
from property_scores.common import model_version

_FIXTURE = Path(__file__).parent / "fixtures" / "bal_urban_regression_20261118.json"
_ADDR = json.loads(_FIXTURE.read_text())["addresses"]


@pytest.fixture
def version(monkeypatch):
    def _set(v):
        if v is None:
            monkeypatch.delenv(model_version.ENV_VAR, raising=False)
        else:
            monkeypatch.setenv(model_version.ENV_VAR, v)
    return _set


def _replay(name, *, state=None, overlay=None):
    rec = _ADDR[name]

    def _lookup(table):
        def _f(la, ln):
            # a read the production run did not make means the code path
            # diverged from what was recorded: fail loudly, never guess
            return table[f"{la:.9f},{ln:.9f}"]
        return _f

    ov = overlay if overlay is not None else rec["overlay"]
    with mock.patch("property_scores.bushfire.score.landcover_grid",
                    return_value=rec["landcover_grid"]), \
         mock.patch("property_scores.common.terrain.elevation",
                    side_effect=_lookup(rec["elevation_samples"])), \
         mock.patch("property_scores.common.terrain.resolution_m",
                    side_effect=_lookup(rec["resolution_samples"])):
        return bal_prescreen(
            rec["lat"], rec["lng"], state=state or rec["state"],
            elevation=rec["slope"]["elevation_m"],
            slope_deg=rec["slope"]["mean_slope_deg"],
            overlay=(ov[0], ov[1], ov[2], ov[3], ov[4]),
        )


@pytest.mark.parametrize("name", sorted(_ADDR))
def test_v1_replay_reproduces_production_byte_for_byte(name, version):
    # v1 is Foundit's pinned model: the fix must not move any of its outputs,
    # and this also proves the fixture replays the production code path.
    version(None)
    r = _replay(name)
    want = _ADDR[name]["production_v1_result"]
    assert r["indicative_bal"] == want["indicative_bal"]
    assert r["bal_range"] == want["bal_range"]
    assert r["confidence"] == want["confidence"]
    assert "satellite_only_screen" not in r["inputs"]


def test_v2_darwin_steep_escarpment_is_not_fz_at_50_to_100_m(version):
    # FDI 40 (NT), Forest (A), 15-20 deg downslope row = (24, 31, 44, 59) m.
    # The nearest downslope pixels sit at 45.1-58.4 m, i.e. 44 <= d < 59
    # -> BAL-19. The >20 deg pixels (53.4-96.5 m) take that row too, never
    # BAL-FZ. Mild end: Woodland (B) flat (6, 9, 13, 19) at ~45 m -> BAL-12.5.
    # Harsh end stays BAL-FZ: Method 1 cannot bound a >20 deg slope.
    version("v2")
    r = _replay("darwin_cbd_apartment")
    assert r["indicative_bal"] == "BAL-19"
    assert r["bal_range"] == ["BAL-12.5", "BAL-FZ"]
    assert r["confidence"] == "low"
    assert any("Method 2 required" in a for a in r["assumptions"])


def test_v2_campbell_officially_outside_bpa_is_bal_low(version):
    version("v2")
    r = _replay("campbell_act_unit")
    assert r["official_overlay"]["status"] == "outside"
    assert r["indicative_bal"] == "BAL-LOW"
    assert r["bal_range"] == ["BAL-LOW", "BAL-12.5"]
    assert r["confidence"] == "moderate"
    # the satellite-only reading stays visible for diagnosis, not as the answer
    assert r["inputs"]["satellite_only_screen"]["bal"] == "BAL-FZ"


def test_v2_katoomba_forest_at_site_stays_fz(version):
    version("v2")
    r = _replay("katoomba_cliff_edge")
    assert r["indicative_bal"] == "BAL-FZ"
    assert r["bal_range"] == ["BAL-FZ", "BAL-FZ"]
    assert r["confidence"] == "moderate"


def test_v2_orders_the_three_addresses_by_real_exposure(version):
    version("v2")
    rank = {n: _bal_rank(_replay(n)["indicative_bal"]) for n in _ADDR}
    assert (rank["katoomba_cliff_edge"] > rank["darwin_cbd_apartment"]
            > rank["campbell_act_unit"])


@pytest.mark.parametrize("state", ["VIC", "TAS", "QLD"])
def test_v2_outside_a_partial_layer_keeps_the_satellite_screen(state, version):
    # VIC BMO is a subset of the statutory BPA, TAS also counts unmapped land
    # near bushfire-prone vegetation, QLD is the 2017 QFD proxy: "not on the
    # layer" is not "outside prone land" there. Same real Campbell grid with
    # the forest pixel at the site (0 m): every FDI table puts 0 m in BAL-FZ.
    version("v2")
    r = _replay("campbell_act_unit", state=state)
    assert r["official_overlay"]["status"] == "outside"
    assert r["indicative_bal"] == "BAL-FZ"
    assert "satellite_only_screen" not in r["inputs"]
    assert not any("OUTSIDE" in a for a in r["assumptions"])
    assert r["confidence"] == "low"


@pytest.mark.parametrize("overlay", [
    (None, [], None, False, "state_service"),   # a layer query failed
    (None, [], None, True, None),               # no official layer at all
])
def test_v2_unknown_overlay_never_reads_as_officially_outside(overlay, version):
    version("v2")
    r = _replay("campbell_act_unit", overlay=overlay)
    assert r["official_overlay"]["status"] == "unavailable"
    assert r["indicative_bal"] != "BAL-LOW"
    assert "satellite_only_screen" not in r["inputs"]


def test_steep_pixel_uses_the_15_to_20_deg_row_in_v2_and_fz_in_v1(version):
    # One Forest pixel 60 m south on a 25 deg plane (site-to-veg and
    # under-veg both 25 deg -> ">20"). VIC FDI 100, Forest (A), GA rows:
    #   15-20 deg (50, 61, 78, 98): 50 <= 60 < 61 -> BAL-40
    #   10-15 deg (39, 49, 64, 82): 49 <= 60 < 64 -> BAL-29 (must NOT be used)
    # Mild end Woodland (B) flat (12, 16, 24, 33): 60 >= 33 -> BAL-12.5.
    # Overlay unavailable so no official gate is involved.
    site_lat, site_lng = -37.8, 145.0
    veg_lat = site_lat - 60.0 / 111320.0
    vegetation = {
        "distance_m": 60.0, "wc_class": 10, "in_vegetation": False,
        "patch_pixels": {10: 130, 20: 0, 30: 0}, "nearest_patch_pixels": 130,
        "min_patch_pixels": 98, "pixel_area_m2": 102.7,
        "veg_lat": veg_lat, "veg_lng": site_lng,
        "pixels": [{"wc_class": 10, "distance_m": 60.0,
                    "veg_lat": veg_lat, "veg_lng": site_lng}],
    }
    tan25 = math.tan(math.radians(25))

    def _elev(la, ln):
        return 100.0 + tan25 * (la - site_lat) * 111320.0

    def _run():
        with mock.patch(
                "property_scores.bal_prescreen.prescreen._nearest_vegetation",
                return_value=vegetation), \
             mock.patch("property_scores.common.terrain.elevation",
                        side_effect=_elev), \
             mock.patch("property_scores.common.terrain.resolution_m",
                        return_value=5.0):
            return bal_prescreen(
                site_lat, site_lng, state="VIC", elevation=100, slope_deg=25,
                overlay=(None, [], None, False, None))

    version("v2")
    r = _run()
    assert r["inputs"]["slope"]["band"] == ">20"
    assert r["indicative_bal"] == "BAL-40"
    assert r["bal_range"] == ["BAL-12.5", "BAL-FZ"]
    assert any("Method 2 required" in a for a in r["assumptions"])
    assert "-> BAL-FZ" not in r["inputs"]["slope"]["basis"]

    version(None)
    r1 = _run()
    assert r1["indicative_bal"] == "BAL-FZ"
    assert r1["bal_range"] == ["BAL-12.5", "BAL-FZ"]
    assert "Method 1 invalid -> BAL-FZ" in r1["inputs"]["slope"]["basis"]
    assert not any("Method 2 required" in a for a in r1["assumptions"])


def test_v2_method2_note_absent_when_statutory_outside_overrides(version):
    version("v2")
    r = _replay("darwin_cbd_apartment", state="NSW",
                overlay=(None, [], None, True, "local_library"))
    assert r["indicative_bal"] == "BAL-LOW"
    assert not any("Method 2 required" in a for a in r["assumptions"])
