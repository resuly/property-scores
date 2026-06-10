"""Flood score unit tests — the HAND physics gate on JRC satellite evidence.

Anchor case (2026-06-10, Simon Kean verification): Esk St North Wahroonga,
hand 51.9 m / drainage 129.1 m, JRC false-water (dark forest) gave jrc_score 55
-> final 65 "Moderate Risk" on a hilltop that physically cannot flood.
"""
import pytest

from property_scores.flood.score import _hand_discounted_jrc, _jrc_to_score


def _hand(hand_m, drainage=50.0, uncertain=False):
    return {"hand_m": hand_m, "drainage_elev_m": drainage, "uncertain": uncertain}


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


def test_jrc_to_score_no_water_safe():
    assert _jrc_to_score({"nearest_water_m": None, "flood_cells": 0, "wet_cells": 0}) == 95


def test_jrc_to_score_floodplain_risky():
    s = _jrc_to_score({"nearest_water_m": 50, "flood_cells": 20, "wet_cells": 25})
    assert s <= 15
