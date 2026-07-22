"""Flood score unit tests — the HAND physics gate on JRC satellite evidence.

Anchor case (2026-06-10, Simon Kean verification): Esk St North Wahroonga,
hand 51.9 m / drainage 129.1 m, JRC false-water (dark forest) gave jrc_score 55
-> final 65 "Moderate Risk" on a hilltop that physically cannot flood.
"""
import pytest

from property_scores.flood.score import _hand_discounted_jrc, _jrc_to_score


def _hand(hand_m, drainage=50.0, uncertain=False, source=None, point_elev=None):
    return {
        "hand_m": hand_m,
        "drainage_elev_m": drainage,
        "uncertain": uncertain,
        "source": source,
        "point_elev_m": point_elev,
    }


def test_no_hand_passthrough():
    assert _hand_discounted_jrc(55, None) == 55


def test_none_jrc_passthrough():
    assert _hand_discounted_jrc(None, _hand(30)) is None


def test_uncertain_dem_passthrough():
    assert _hand_discounted_jrc(55, _hand(30, uncertain=True)) == 55


def test_coastal_zero_drainage_guard():
    # sea-level drainage artefact must not neutralise JRC at the waterfront
    assert _hand_discounted_jrc(55, _hand(30, drainage=0.0)) == 55


def test_floodplain_keeps_full_evidence():
    # hand below 10 m: genuine flood terrain, JRC untouched
    assert _hand_discounted_jrc(15, _hand(3.0)) == 15
    assert _hand_discounted_jrc(55, _hand(9.9)) == 55


def test_ramp_midpoint():
    # hand 15 m: halfway between 55 and neutral 95 -> 75
    assert _hand_discounted_jrc(55, _hand(15.0)) == 75


def test_hilltop_fully_neutral():
    # the North Wahroonga anchor: hand 51.9 m neutralises the false water
    assert _hand_discounted_jrc(55, _hand(51.9)) == 95
    assert _hand_discounted_jrc(15, _hand(20.0)) == 95


def test_monotone_in_hand():
    vals = [_hand_discounted_jrc(55, _hand(h)) for h in (5, 12, 15, 18, 25)]
    assert vals == sorted(vals)


def test_skirving_lidar_vertical_separation_materially_reduces_jrc_risk():
    # Skirving St anchor: 58% occurrence / 137 m proximity produced JRC score
    # 20 and final 26 High Risk despite survey-grade HAND 10.8 m. LiDAR makes
    # that vertical separation strong enough to move the JRC signal to 50.
    assert _hand_discounted_jrc(20, _hand(10.8, source="lidar_5m")) == 50


@pytest.mark.parametrize("name,hand_m", [
    ("Parramatta riverbank", 1.0),
    ("Elwood floodplain", 0.9),
    ("North Ryde", 4.1),
])
def test_low_hand_lidar_regression_anchors_keep_full_jrc_evidence(name, hand_m):
    assert _hand_discounted_jrc(20, _hand(hand_m, source="lidar_5m")) == 20, name


def test_medium_confidence_skirving_height_keeps_conservative_ramp():
    # A 10.8 m coarse DEM read is not strong enough to use the LiDAR curve.
    assert _hand_discounted_jrc(20, _hand(10.8, source="dem_relief")) == 26


def test_jrc_to_score_no_water_safe():
    assert _jrc_to_score({"nearest_water_m": None, "flood_cells": 0, "wet_cells": 0}) == 95


def test_jrc_to_score_floodplain_risky():
    s = _jrc_to_score({"nearest_water_m": 50, "flood_cells": 20, "wet_cells": 25})
    assert s <= 15


# --- LiDAR / DEM-H resolution-aware HAND + elevation_confidence (P2) ---

from property_scores.flood import score as _score
from property_scores.flood.score import (
    _ELEV_CONFIDENCE, _hand_from_elev, _hand_local,
)


def _bowl(pt_lat, pt_lng, pt_elev, drop):
    """Elevation sampler: the query point sits `drop` m above its ring (the ring
    holds the local drainage), so HAND = drop."""
    def elev(lat, lng):
        near = abs(lat - pt_lat) < 1e-4 and abs(lng - pt_lng) < 1e-4
        return pt_elev if near else pt_elev - drop
    return elev


def test_hand_from_elev_ring_math():
    h = _hand_from_elev(-33.8, 151.0, _bowl(-33.8, 151.0, 8.0, 6.0),
                        "lidar_5m", 1.0)
    assert h["hand_m"] == 6.0 and h["source"] == "lidar_5m"
    assert h["relief_m"] == 6.0 and h["uncertain"] is False


def test_hand_from_elev_source_aware_uncertainty():
    # 3 m relief: within DEM-H's ~5 m noise (uncertain) but trusted by LiDAR.
    elev = _bowl(-33.8, 151.0, 8.0, 3.0)
    assert _hand_from_elev(-33.8, 151.0, elev, "dem_relief", 5.0)["uncertain"] is True
    assert _hand_from_elev(-33.8, 151.0, elev, "lidar_5m", 1.0)["uncertain"] is False


def test_hand_from_elev_none_outside_coverage():
    assert _hand_from_elev(-33.8, 151.0, lambda la, ln: None, "lidar_5m", 1.0) is None


def test_elevation_confidence_mapping():
    assert _ELEV_CONFIDENCE["lidar_5m"] == "high"
    assert _ELEV_CONFIDENCE["dem_relief"] == "medium"
    assert _ELEV_CONFIDENCE.get("proxy", "low") == "low"


class _FakeWindow:
    source = "lidar_5m"
    uncertain_thresh = 1.0

    def __init__(self, pt_lat, pt_lng, pt_elev, drop):
        self._elev = _bowl(pt_lat, pt_lng, pt_elev, drop)
        self.closed = False

    def elev(self, lat, lng):
        return self._elev(lat, lng)

    def close(self):
        self.closed = True


def test_hand_local_prefers_lidar(monkeypatch):
    win = _FakeWindow(-33.8, 151.0, 8.0, 6.0)
    monkeypatch.setattr(_score, "_hand_local_proxy", lambda la, ln: {"hand_m": 99})
    import property_scores.flood.lidar as _lidar
    monkeypatch.setattr(_lidar, "covered", lambda s: s in ("NSW", "QLD"))
    monkeypatch.setattr(_lidar, "open_window", lambda la, ln, st: win)
    h = _hand_local(-33.8, 151.0, "NSW")
    assert h["source"] == "lidar_5m" and h["hand_m"] == 6.0
    assert win.closed is True  # window handle always released


def test_contour_window_tiers_by_interval():
    from property_scores.flood.lidar import ContourWindow
    # 1 m contour interval -> survey-grade high.
    fine = ContourWindow([(-37.8, 144.9, float(a)) for a in (10, 11, 12, 13)])
    assert fine.step == 1.0 and fine.source == "lidar_contour_1m"
    assert fine.uncertain_thresh == 1.0
    # 5 m interval -> better than DEM-H but labelled medium (provenance-neutral).
    coarse = ContourWindow([(-41.4, 147.1, float(a)) for a in (5, 10, 15, 20)])
    assert coarse.step == 5.0 and coarse.source == "contour_med"
    assert coarse.uncertain_thresh == 2.5


def test_contour_window_idw_interpolates_between_levels():
    from property_scores.flood.lidar import ContourWindow
    # A 10 m contour due west and a 20 m due east; a point between reads ~15 m.
    w = ContourWindow([(-37.80, 144.900, 10.0), (-37.80, 144.910, 20.0)])
    mid = w.elev(-37.80, 144.905)
    assert 13.0 < mid < 17.0


def test_hand_local_falls_back_to_demh(monkeypatch):
    # LiDAR unavailable (timeout / SRTM fill) -> DEM-H 30 m, medium confidence.
    import property_scores.flood.lidar as _lidar
    monkeypatch.setattr(_lidar, "covered", lambda s: s in ("NSW", "QLD"))
    monkeypatch.setattr(_lidar, "open_window", lambda la, ln, st: None)
    from property_scores.common import terrain
    monkeypatch.setattr(terrain, "available", lambda: True)
    monkeypatch.setattr(terrain, "elevation", _bowl(-33.8, 151.0, 8.0, 6.0))
    h = _hand_local(-33.8, 151.0, "NSW")
    assert h["source"] == "dem_relief" and _ELEV_CONFIDENCE[h["source"]] == "medium"
