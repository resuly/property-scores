"""NaN/inf must never turn a score response into a 500.

Production logged 97 "Out of range float values are not JSON compliant: nan"
500s between 2026-09-10 and 2026-09-21. Two sources are covered here: the
batch /scores endpoint echoing a non-finite lat/lng, and a raster window whose
NaN gaps made the window mean NaN. The response class is the backstop for any
other component: non-finite floats become null and the rest of the payload
still arrives.
"""

import logging
import math

import numpy as np
import pytest
from fastapi.testclient import TestClient

from property_scores.api import main
from property_scores.noise import raster_sample


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=True)


def test_component_nan_serialises_as_null_not_500(client, monkeypatch, caplog):
    monkeypatch.setattr(main, "heat_island_score", lambda lat, lng: {
        "score": float("nan"),
        "label": "Moderate",
        "detail": {"uhi_delta_c": float("inf"), "night_lst_c": 21.5,
                   "series": [1.0, float("-inf")]},
    })
    with caplog.at_level(logging.WARNING, logger=main.logger.name):
        r = client.get("/scores/heat-island", params={"lat": -37.8, "lng": 145.0})
    assert r.status_code == 200
    body = r.json()
    assert body["score"] is None
    assert body["label"] == "Moderate"
    assert body["detail"] == {"uhi_delta_c": None, "night_lst_c": 21.5,
                              "series": [1.0, None]}
    warning = " ".join(rec.getMessage() for rec in caplog.records)
    assert ".score" in warning and ".detail.uhi_delta_c" in warning
    assert ".detail.series[1]" in warning


def test_finite_payload_is_untouched_and_not_logged(client, monkeypatch, caplog):
    monkeypatch.setattr(main, "heat_island_score",
                        lambda lat, lng: {"score": 42.5, "label": "Low"})
    with caplog.at_level(logging.WARNING, logger=main.logger.name):
        r = client.get("/scores/heat-island", params={"lat": -37.8, "lng": 145.0})
    assert r.status_code == 200
    assert r.json() == {"score": 42.5, "label": "Low"}
    assert not [rec for rec in caplog.records if "non-finite" in rec.getMessage()]


def _stub_batch(monkeypatch, heat):
    ok = lambda *a, **k: {"score": 50}  # noqa: E731
    for name in ("walkability_score", "flood_score", "bushfire_score",
                 "view_quality_score", "contamination_score",
                 "aircraft_noise_penalty"):
        monkeypatch.setattr(main, name, ok)
    monkeypatch.setattr(main, "_noise_for_batch", ok)
    monkeypatch.setattr(main, "_solar_with_footprint", ok)
    monkeypatch.setattr(main, "heat_island_score", heat)


def test_batch_component_nan_keeps_the_rest_of_the_payload(client, monkeypatch):
    _stub_batch(monkeypatch, lambda lat, lng: {"score": float("nan")})
    r = client.get("/scores", params={"lat": -37.8, "lng": 145.0})
    assert r.status_code == 200
    body = r.json()
    assert body["heat_island"]["score"] is None
    assert body["flood"]["score"] == 50
    assert body["lat"] == -37.8


@pytest.mark.parametrize("lat,lng", [("nan", "145.0"), ("-37.8", "nan"),
                                     ("inf", "145.0"), ("-37.8", "-inf")])
def test_batch_rejects_non_finite_coordinates(client, monkeypatch, lat, lng):
    calls = []
    _stub_batch(monkeypatch, lambda *a: calls.append(a) or {"score": 1})
    r = client.get("/scores", params={"lat": lat, "lng": lng})
    assert r.status_code == 422
    assert calls == []  # rejected before any component runs


def test_window_stats_ignores_nan_gaps(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin

    path = str(tmp_path / "gaps.tif")
    arr = np.full((21, 21), 30.0, dtype="float32")
    arr[::2, ::3] = np.nan
    arr[10, 10] = 40.0
    with rasterio.open(path, "w", driver="GTiff", width=21, height=21, count=1,
                       dtype="float32", crs="EPSG:4326", nodata=np.nan,
                       transform=from_origin(144.99, -37.79, 0.001, 0.001)) as dst:
        dst.write(arr, 1)

    stats = raster_sample.window_stats(path, -37.8, 145.0, radius_m=500)
    assert stats["count"] > 0
    assert math.isfinite(stats["mean"])
    assert 30.0 <= stats["mean"] <= 40.0
    assert stats["max"] == 40.0


_COORD_PARAMS = {"lat", "lng", "src_lat", "src_lng"}


def _coordinate_routes():
    """Every GET route with a coordinate query parameter, read from the app so
    a new endpoint is covered without editing this list."""
    from fastapi.routing import APIRoute
    out = []
    for route in main.app.routes:
        if not isinstance(route, APIRoute) or "GET" not in route.methods:
            continue
        names = {p.name for p in route.dependant.query_params}
        coords = sorted(names & _COORD_PARAMS)
        if coords:
            out.append((route.path, tuple(coords)))
    return out


_ROUTES = _coordinate_routes()


def test_coordinate_route_discovery_is_not_empty():
    paths = {path for path, _ in _ROUTES}
    assert {"/scores", "/scores/noise", "/scores/noise/terrain",
            "/scores/elevation/contours", "/scores/aircraft-noise"} <= paths


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf"])
@pytest.mark.parametrize("path,coords", _ROUTES,
                         ids=[f"{p}:{','.join(c)}" for p, c in _ROUTES])
def test_every_coordinate_param_rejects_non_finite(client, path, coords, bad):
    valid = {"lat": "-37.8", "lng": "145.0",
             "src_lat": "-37.8", "src_lng": "145.0"}
    for target in coords:
        params = {c: valid[c] for c in coords}
        params[target] = bad
        r = client.get(path, params=params)
        assert r.status_code == 422, (path, target, bad, r.status_code)
        locs = [tuple(e["loc"]) for e in r.json()["detail"]]
        assert ("query", target) in locs
