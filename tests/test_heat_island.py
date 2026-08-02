"""Heat island caching: the result belongs to the address, not the kilometre.

Regression cover for the 2026-07-25 defect where `_cache_key` rounded to 2 dp
(~1.1 km) and cached the WHOLE result, so the first address computed in a cell
handed its `building_density` and `greenspace_factor` to every other address in
that cell for an hour. Production showed Royal Park parkland values being
served to terrace housing 450 m away.

These tests stub every input, so they need neither the network nor the MODIS /
WorldCover rasters.
"""
import pytest

from property_scores.heat_island import score as hs


@pytest.fixture(autouse=True)
def _clear_caches():
    hs._cache.clear()
    yield
    hs._cache.clear()


# Two coordinates inside ONE round(2) cell: both round to (-37.78, 144.95).
PARK = (-37.7845, 144.9530)
HOUSES = (-37.7801, 144.9548)


def _stub_inputs(monkeypatch, calls=None):
    """Local factors vary by coordinate; MODIS is constant (in coverage)."""
    def density(lat, lng):
        if calls is not None:
            calls.setdefault("density", []).append((lat, lng))
        return 0.47 if (lat, lng) == PARK else 1.0

    def green(lat, lng):
        if calls is not None:
            calls.setdefault("green", []).append((lat, lng))
        return 0.78 if (lat, lng) == PARK else 0.65

    monkeypatch.setattr(hs, "_building_density_proxy", density)
    monkeypatch.setattr(hs, "_greenspace_proxy", green)
    monkeypatch.setattr(hs, "_modis_lst", lambda lat, lng: {
        "point_lst_c": 33.4, "area_lst_c": 34.1,
        "uhi_delta_c": -0.7, "night_lst_c": 15.0, "samples": 1})


def test_neighbour_in_same_grid_cell_gets_its_own_local_factors(monkeypatch):
    """The defect: HOUSES was served PARK's row because they share a cell."""
    _stub_inputs(monkeypatch)
    park = hs.heat_island_score(*PARK)
    houses = hs.heat_island_score(*HOUSES)

    assert (round(PARK[0], 2), round(PARK[1], 2)) == (round(HOUSES[0], 2),
                                                      round(HOUSES[1], 2)), \
        "test points must share a round(2) cell or this proves nothing"

    assert park["building_density"] == 0.47
    assert houses["building_density"] == 1.0
    assert park["greenspace_factor"] == 0.78
    assert houses["greenspace_factor"] == 0.65
    assert not houses.get("cached")
    assert houses["score"] != park["score"]


def test_same_address_repeat_is_served_from_cache(monkeypatch):
    """Removing the smearing must not cost the exact-repeat fast path."""
    calls: dict = {}
    _stub_inputs(monkeypatch, calls)
    first = hs.heat_island_score(*PARK)
    second = hs.heat_island_score(*PARK)

    assert second.get("cached") is True
    assert second["score"] == first["score"]
    assert len(calls["density"]) == 1, "local factors recomputed on an exact repeat"


def test_no_modis_coverage_returns_data_unavailable(monkeypatch):
    """2026-08-02: the Open-Meteo ERA5 fallback was removed (DA Leads is a
    paid commercial product; that endpoint's free tier is non-commercial-use
    only). A MODIS miss must now degrade honestly to "Data unavailable"
    instead of silently estimating from a service we are not licensed to
    call for a paying customer."""
    monkeypatch.setattr(hs, "_building_density_proxy", lambda lat, lng: 1.0)
    monkeypatch.setattr(hs, "_greenspace_proxy", lambda lat, lng: 0.5)
    monkeypatch.setattr(hs, "_modis_lst", lambda lat, lng: None)

    result = hs.heat_island_score(*PARK)

    assert result["score"] is None
    assert result["label"] == "Data unavailable"


def test_module_has_no_open_meteo_dependency():
    """Regression guard: nothing in this module should be able to reach
    api.open-meteo.com / archive-api.open-meteo.com again by accident."""
    assert not hasattr(hs, "_fetch_summer_temp")
    assert not hasattr(hs, "_summer_temp_grid")
    assert not hasattr(hs, "_era5_cache")
    assert not hasattr(hs, "OPEN_METEO_HIST")
