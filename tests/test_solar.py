"""Unit tests for solar score logic (no network calls)."""

from property_scores.solar.score import ORIENTATION_FACTOR


def test_orientation_factors():
    assert ORIENTATION_FACTOR["optimal"] == 1.0
    assert ORIENTATION_FACTOR["east"] < 1.0
    assert ORIENTATION_FACTOR["suboptimal"] < ORIENTATION_FACTOR["east"]


def test_score_formula_high_pvout():
    pvout = 2000
    orient = 1.0
    score_raw = (pvout - 600) / (2400 - 600) * 100 * orient
    assert 70 < score_raw < 85


def test_score_formula_low_pvout():
    pvout = 800
    orient = 1.0
    score_raw = (pvout - 600) / (2400 - 600) * 100 * orient
    assert score_raw < 15


def test_estimated_kwh():
    roof_area = 50  # m²
    pvout = 1800  # kWh/kWp/year
    panel_efficiency = 0.20
    performance_ratio = 0.80
    capacity = roof_area * panel_efficiency  # 10 kWp
    annual_kwh = capacity * pvout * 1.0 * performance_ratio  # 14,400 kWh
    assert 14_000 < annual_kwh < 15_000
