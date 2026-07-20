"""Indicative Bushfire Attack Level (BAL) pre-screen (AS 3959 Method 1).

A coordinate-in -> indicative BAL-out estimator for the QUOTING stage. NOT a
certified BAL assessment: a compliant BAL still requires a site assessment by an
accredited bushfire assessor. This module automates the three inputs that free
calculators make a designer hand-enter (vegetation class, distance, slope) and
returns an indicative band with full input transparency and a confidence range.

Reuses the property-scores bushfire pipeline (ESA WorldCover 10m vegetation,
local DEM/5m-LiDAR slope, official state planning overlays).

See docs/bal-prescreen.md for method, sources and known limitations.
"""

from property_scores.bal_prescreen.prescreen import bal_prescreen  # noqa: F401
