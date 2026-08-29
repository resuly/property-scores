"""Solar Resource contract tests. Every network input is stubbed."""

import pytest
from fastapi.testclient import TestClient

from property_scores.solar import score as ss


@pytest.fixture(autouse=True)
def _clear_cache():
    ss._cache.clear()
    yield
    ss._cache.clear()


def _solar(pvout=1500.0):
    return {
        "ghi_kwh_m2": 1600.0,
        "dni_kwh_m2": 1700.0,
        "gti_kwh_m2": 1800.0,
        "pvout_kwh_kwp": pvout,
        "optimal_tilt_deg": 31,
        "temp_avg_c": 15.2,
        "elevation_m": 42,
        "source_metadata": {
            "retrieved_at_ms": 1_788_012_279_987,
            "dataset_version": "1.7",
            "layers": {
                "GHI": {"period": {"from": "satregion", "to": "2025"},
                        "updated": "2026-04-01", "version": "2.2.68"},
                "DNI": {"period": {"from": "satregion", "to": "2025"},
                        "updated": "2026-04-01", "version": "2.2.68"},
                "PVOUT_csi": {"period": {"from": "satregion", "to": "2025"},
                              "updated": "2026-04-01", "version": "2.2.68"},
                "OPTA": {"period": {"from": "satregion", "to": "2025"},
                         "updated": "2026-04-01", "version": "2.2.68"},
            },
        },
    }


def test_resource_contract_separates_field_resolutions(monkeypatch):
    monkeypatch.setattr(ss, "_fetch_solar_data", lambda *_: _solar())

    out = ss.solar_score(-37.81, 144.96)

    assert out["product"] == "solar_resource"
    assert out["assessment_level"] == "regional_resource"
    assert out["open_horizon"] is True
    assert out["roof_model"] == {
        "roof_planes_modelled": False,
        "usable_area_modelled": False,
        "shading_modelled": False,
    }
    assert out["spatial_resolution_m"]["ghi_kwh_m2_year"] == 250
    assert out["spatial_resolution_m"]["pvout_kwh_kwp_year"] == 1000
    assert out["spatial_resolution_m"]["optimal_tilt_deg"] == 4000
    assert out["source"] == "Global Solar Atlas 2.0"
    assert out["licence"] == "CC BY 4.0"
    assert out["source_metadata"]["vintage"]["pvout"]["period_to"] == "2025"


def test_score_is_resource_only_and_orientation_changes_scenario_not_score(monkeypatch):
    monkeypatch.setattr(ss, "_fetch_solar_data", lambda *_: _solar(1750.0))

    optimal = ss.solar_score(-37.81, 144.96, roof_area_m2=50,
                             orientation="optimal")
    east = ss.solar_score(-37.81, 144.96, roof_area_m2=50,
                          orientation="east")

    assert optimal["score"] == east["score"] == 80
    assert optimal["estimated_annual_kwh"] == 17_500
    assert east["estimated_annual_kwh"] == 14_875
    assert east["generation_scenario"]["status"] == \
        "gross_open_horizon_scenario"
    assert "not validated usable roof area" in \
        east["generation_scenario"]["area_semantics"]
    assert "tree shading" in east["generation_scenario"]["not_modelled"]


@pytest.mark.parametrize(
    ("pvout", "expected_score", "expected_label"),
    [
        (2000, 100, "Excellent Solar Potential"),
        (1500, 60, "Good Solar Potential"),
        (1250, 40, "Moderate Solar Potential"),
        (1000, 20, "Low Solar Potential"),
        (750, 0, "Poor Solar Potential"),
    ],
)
def test_current_score_anchors(monkeypatch, pvout, expected_score, expected_label):
    monkeypatch.setattr(ss, "_fetch_solar_data", lambda *_: _solar(pvout))
    out = ss.solar_score(-37.81, 144.96)
    assert out["score"] == expected_score
    assert out["label"] == expected_label


@pytest.mark.parametrize("area", [0, -1, "50"])
def test_generation_scenario_rejects_invalid_area_before_fetch(monkeypatch, area):
    monkeypatch.setattr(ss, "_fetch_solar_data", lambda *_: _solar())
    with pytest.raises(ValueError, match="positive number"):
        ss.solar_score(-37.81, 144.96, roof_area_m2=area)


def test_unknown_orientation_is_not_silently_treated_as_east(monkeypatch):
    monkeypatch.setattr(ss, "_fetch_solar_data", lambda *_: _solar())
    with pytest.raises(ValueError, match="orientation must be"):
        ss.solar_score(-37.81, 144.96, orientation="north-east")


def test_upstream_failure_still_returns_rights_and_level(monkeypatch):
    monkeypatch.setattr(ss, "_fetch_solar_data", lambda *_: None)
    out = ss.solar_score(-37.81, 144.96)
    assert out["score"] is None
    assert out["assessment_level"] == "regional_resource"
    assert out["licence"] == "CC BY 4.0"


def test_batch_footprint_is_context_not_fake_usable_roof(monkeypatch):
    from property_scores.api import main

    monkeypatch.setattr(main, "building_footprint_m2", lambda *_: 123.4)
    monkeypatch.setattr(main, "get_db", lambda: object())
    monkeypatch.setattr(main, "solar_score", lambda *_args, **kwargs: {
        "score": 50,
        "estimated_annual_kwh": None,
        "received_kwargs": kwargs,
    })

    out = main._solar_with_footprint(-37.81, 144.96)

    assert out["received_kwargs"] == {}
    assert out["estimated_annual_kwh"] is None
    assert out["building_context"]["building_footprint_m2"] == 123
    assert out["building_context"]["used_in_generation_estimate"] is False
    assert "not a per-unit share or usable roof area" in \
        out["building_context"]["semantics"]


@pytest.mark.parametrize(
    "query",
    [
        "orientation=north-east",
        "roof_area=0",
        "roof_area=-1",
        "roof_area=nan",
    ],
)
def test_solar_route_rejects_invalid_scenario_inputs_without_500(query):
    from property_scores.api.main import app

    response = TestClient(app).get(
        f"/scores/solar?lat=-37.81&lng=144.96&{query}")

    assert response.status_code == 422


def test_malformed_gsa_annual_block_degrades_to_unavailable(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"annual": None}

    monkeypatch.setattr(ss.requests, "get", lambda *_args, **_kwargs: Response())

    out = ss.solar_score(-37.81, 144.96)

    assert out["score"] is None
    assert out["label"] == "Data unavailable"


@pytest.mark.parametrize(
    ("path", "target"),
    [
        ("/solar", "https://daleads.com.au/property-scores/solar/"),
        ("/heat-island",
         "https://daleads.com.au/property-scores/heat-island/"),
        ("/landscape-openness",
         "https://daleads.com.au/property-scores/view-quality/"),
    ],
)
def test_retired_embedded_pages_redirect_to_canonical_truth(path, target):
    from property_scores.api.main import app

    response = TestClient(app).get(path, follow_redirects=False)

    assert response.status_code == 308
    assert response.headers["location"] == target


def test_embedded_score_index_uses_current_public_boundaries():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1]
              / "property_scores/api/static/index.html").read_text(
                  encoding="utf-8")

    assert "Solar Resource" in source
    assert "Neighbourhood Heat" in source
    assert "Landscape Openness" in source
    assert "ERA5 Summer Mean" not in source
    assert "Est. Annual (50m" not in source
    assert "View Quality" not in source
