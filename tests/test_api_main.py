"""Tests for the FastAPI batch assembly layer."""

from property_scores.api import main


_DEBUG_PAYLOAD = {
    "score": {"score": 71, "estimated_db": 58.2},
    "sources": {
        "aadt": [{"road_name": "Example Road", "lat": -37.8, "lng": 145.0, "db": 56.1}],
        "rail": [{"route": "Route 96", "lat": -37.81, "lng": 145.01, "db": 49.4}],
        "rail_shapes": [{"shape_id": "96", "coords": [[-37.81, 145.01]]}],
    },
    "terrain_source": {"name": "Example Road", "db": 56.1},
}


def test_batch_noise_detail_merges_sources(monkeypatch):
    seen = {}

    def fake_debug(lat, lng, radius, include_overture_roads=True):
        seen["include_overture_roads"] = include_overture_roads
        return _DEBUG_PAYLOAD

    monkeypatch.setattr(main, "noise_debug", fake_debug)

    out = main._noise_for_batch(-37.8, 145.0, detail=True)

    assert out["score"] == 71
    assert out["sources"]["aadt"][0]["road_name"] == "Example Road"
    assert out["sources"]["rail_shapes"][0]["shape_id"] == "96"
    assert out["terrain_source"]["name"] == "Example Road"
    # The only consumer strips ODbL segments, so never pay to compute them.
    assert seen["include_overture_roads"] is False


def test_batch_noise_without_detail_skips_debug(monkeypatch):
    monkeypatch.setattr(main, "noise_score",
                        lambda lat, lng, source=None: {"score": 71})

    def explode(*args, **kwargs):
        raise AssertionError("noise_debug must not run without detail=True")

    monkeypatch.setattr(main, "noise_debug", explode)

    assert main._noise_for_batch(-37.8, 145.0)["score"] == 71
