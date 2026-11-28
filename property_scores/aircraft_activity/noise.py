"""Aircraft activity inside the noise result: a counts block and a dB estimate.

Two separate things, kept separate on purpose:

1. ``activity_block(lat, lng)`` -- what was observed overhead: flights per day
   and per night, split by type, typical height, the range across the four
   quarters, the coverage label and the attribution. It never changes a dB or
   a score. Its shape is the stable API contract (docs/aircraft-activity.md).

2. ``estimate_lden(cell)`` -- an aircraft Lden from the FIXED-WING counts
   (jets and light aircraft; helicopters are counted but never converted to
   dB). Only with NOISE_AIRCRAFT_ACTIVITY=estimate, and only when
   ``estimate_for_level`` passes it (coverage ok, Lden >= APPLY_FLOOR_DB),
   noise_score mixes it into the level: where no official ANEF contour
   applies, or for every point of a request that excludes the official
   contours (exclude_aircraft). That second case is what keeps a withheld
   contour from being traced: the decision then reads overflight data only.

Estimate method (one paragraph, because it has to be explainable):

  Every counted pass is one noise event. Its sound exposure level (SEL, the
  whole event's A-weighted energy squeezed into one second) is taken from a
  two-row table: the level directly beneath an aircraft at 1,000 ft
  (SEL_AT_1000FT_DB, by class), falling 20 dB per tenfold distance. A pass is
  counted when it comes within 1,000 m sideways at <= 3,000 ft, so the slant
  distance is averaged over a sideways offset spread evenly from 0 to 1,000 m
  at the cell's median height (closed form below). Energy per hour = passes in
  that hour x 10^(SEL/10); Lden weights evening (19-23) by +5 dB and night
  (23-07) by +10 dB and divides by the 86,400 s of a day. The same energy
  per period gives day/evening/night Leqs, which is how the estimate enters
  the level (the ANEF term instead uses one Leq in every period).

  Deliberately conservative in three places, each pushing the estimate LOW:
  an even sideways spread under-weights the many cells that sit right under a
  corridor; passes above 3,000 ft (most of an approach beyond ~15 km) are not
  counted at all; and the reference SELs sit at the quiet end of the published
  range for each class. The calibration against official ANEF bands is in
  docs/aircraft-activity.md; scripts/aircraft_activity_calibrate.py
  reproduces it.

The hourly profile in the data is for all aircraft together; each class is
given the same profile scaled to its daily count (no per-class hourly array
exists). Helicopters are subtracted pro rata the same way.
"""
from __future__ import annotations

import logging
import math

from property_scores.aircraft_activity import store as aas

logger = logging.getLogger(__name__)

# SEL (dBA) directly beneath the aircraft at 1,000 ft. Jet: narrow-body jets
# (A320/737 class) sit around 88 on approach and 93-95 on departure at that
# height. Light: everything ADS-B calls A1/A2/unknown, i.e. piston trainers
# (~75) up to regional turboprops (~84). Both were first set at 88 / 76 and then
# lowered 2 dB after the ANEF check (docs/aircraft-activity.md): at 88/76 the
# cells under active Perth corridors read up to +4.7 dB above ANEF+35, and ANEF
# is a forecast that current traffic should not exceed. Lean low, never high.
SEL_AT_1000FT_DB = {"jet": 86.0, "light": 74.0}
REF_M = 304.8
SIDE_RADIUS_M = 1000.0
MIN_HEIGHT_FT = 200.0  # below this the median is mostly ground roll / runway
DAY_S = 86400.0
# Lden period weights by local hour (07-19 day, 19-23 evening, 23-07 night).
HOUR_WEIGHT = [10.0 if (h >= 23 or h < 7) else (10 ** 0.5 if h >= 19 else 1.0)
               for h in range(24)]
PERIOD_OF_HOUR = [2 if (h >= 23 or h < 7) else (1 if h >= 19 else 0) for h in range(24)]
PERIOD_S = (12 * 3600.0, 4 * 3600.0, 8 * 3600.0)
# An aircraft Lden below this is not mixed into the level: at that size it moves
# a typical suburban level by well under half a dB, and the counts behind it are
# a handful of passes a day whose height and spread the estimate cannot resolve.
APPLY_FLOOR_DB = 45.0
# Coverage labels whose estimate may enter a level. Only ok: in a
# large_aircraft_only city (Brisbane, Newcastle, Launceston) light aircraft and
# helicopters are not reliably received, so a jets-only figure would read as an
# assessed, low aircraft level beside a training field it cannot see
# (Archerfield). estimate_lden still computes jets-only there for calibration.
APPLY_COVERAGE = frozenset({aas.COVERAGE_OK})
# Lden minus the Leq of a level held flat through day, evening and night:
# Lden = L + 10*log10((12 + 4*10^0.5 + 8*10)/24) = L + 6.39. That is how the
# engine's ANEF term behaves (one Leq in all periods); the calibration script
# uses it to put the engine's ANEF bands on the Lden scale. The estimate itself
# enters the level with its own day / evening / night Leqs.
LDEN_MINUS_FLAT_LEQ = 10 * math.log10((12 + 4 * 10 ** 0.5 + 8 * 10) / 24)

METHOD = "adsb-overflight-sel-v1"


def _slant_energy_factor(height_m: float) -> float:
    """Mean of (REF/d)^2 over a sideways offset x ~ U(0, R) at height h.

    (1/R) * integral_0^R dx / (h^2 + x^2) = atan(R/h) / (R h).
    """
    h = max(height_m, 1.0)
    return REF_M ** 2 * math.atan(SIDE_RADIUS_M / h) / (SIDE_RADIUS_M * h)


def estimate_lden(cell: dict, coverage: str) -> dict | None:
    """Aircraft Lden (dB) from one cell's raw values, or None when not estimable.

    `cell` is CityGrid.cell_values(): metrics, hourly, quarters. Coverage
    decides which classes are used: ok -> jet + light; large_aircraft_only ->
    jet only; anything else -> None.
    """
    if coverage == aas.COVERAGE_OK:
        classes = ("jet", "light")
    elif coverage == aas.COVERAGE_LARGE_ONLY:
        classes = ("jet",)
    else:
        return None
    m = cell["metrics"]
    hourly = cell.get("hourly")
    total = m.get("passes_per_day")
    height_ft = m.get("median_agl_ft")
    if not hourly or total is None or not (total > 0):
        return None
    if height_ft is None or not math.isfinite(height_ft):
        return None
    h_ft = max(height_ft, MIN_HEIGHT_FT)
    factor = _slant_energy_factor(h_ft * 0.3048)
    share = [hourly[h] / total for h in range(24)]
    weighted_hour_share = sum(share[h] * HOUR_WEIGHT[h] for h in range(24))
    energy = 0.0          # Lden-weighted SEL energy per day
    period_e = [0.0, 0.0, 0.0]  # unweighted energy in day / evening / night
    per_class = {}
    for cls in classes:
        n = m.get(f"{cls}_per_day") or 0.0
        if n <= 0:
            continue
        sel = SEL_AT_1000FT_DB[cls] + 10 * math.log10(factor)
        e_event = 10 ** (sel / 10)
        e = n * weighted_hour_share * e_event
        per_class[cls] = round(10 * math.log10(e / DAY_S), 1)
        energy += e
        for h in range(24):
            period_e[PERIOD_OF_HOUR[h]] += n * share[h] * e_event
    if energy <= 0:
        return None
    lden = 10 * math.log10(energy / DAY_S)
    leqs = [10 * math.log10(e / s) if e > 0 else 0.0
            for e, s in zip(period_e, PERIOD_S)]
    return {
        "lden_db": round(lden, 1),
        # Unrounded: these feed the level arithmetic; display rounds them.
        "leq_day_db": leqs[0],
        "leq_evening_db": leqs[1],
        "leq_night_db": leqs[2],
        "leq_24h_db": 10 * math.log10(sum(period_e) / DAY_S),
        "lden_db_by_class": per_class,
        "classes": list(classes),
        "height_ft_used": round(h_ft),
        "method": METHOD,
    }


def estimate_for_level(block: dict | None, est: dict | None) -> dict | None:
    """The estimate if it may enter a noise level, else None.

    Reads the overflight data only (the counts block's coverage label and the
    estimate's own Lden), never an official contour: whether an official term
    exists is the caller's separate question, so this answer is the same on
    both sides of a contour edge.
    """
    if not est or not isinstance(block, dict):
        return None
    if block.get("coverage") not in APPLY_COVERAGE:
        return None
    if not est["lden_db"] >= APPLY_FLOOR_DB:
        return None
    return est


def _r(x, nd=2):
    if x is None:
        return None
    x = float(x)
    return round(x, nd) if math.isfinite(x) else None


def _int(x):
    x = _r(x, 0)
    return int(x) if x is not None else None


def _period(meta: dict) -> dict:
    return {
        "date_start": meta.get("date_start"),
        "date_end": meta.get("date_end"),
        "valid_days": meta.get("n_days"),
        "missing_days": len(meta.get("missing_days") or []),
    }


def _empty_block(status: str, message: str) -> dict:
    return {
        "status": status,
        "message": message,
        # Constant: this block never changes a dB or a score. Whether the level
        # used the overflight estimate is reported in noise.aircraft_estimate.
        "affects_score": False,
        "flights_per_day": None,
        "flights_per_night": None,
        "by_type_per_day": {"jet": None, "light": None, "helicopter": None},
        "helicopter_per_night": None,
        "typical_height_ft_agl": None,
        "quarterly_flights_per_day": None,
        "undercounted": [],
        "period": None,
        "definition": _definition(None),
        "coverage": None,
        "coverage_note": None,
        "city": None,
        **_rights(),
    }


def _definition(meta: dict | None) -> dict:
    meta = meta or {}
    return {
        "radius_m": meta.get("radius_m", 1000),
        "ceiling_ft_agl": meta.get("ceiling_ft_agl", 3000),
        "night_hours_local": "22:00-06:00",
        "cell_size_m": 250,
        "counting": ("one aircraft counts once per 10 minutes when it passes within "
                     "radius_m sideways at or below ceiling_ft_agl above the ground"),
    }


def _rights() -> dict:
    return {
        "source": aas.SOURCE,
        "licence": aas.LICENCE,
        "licence_url": aas.LICENCE_URL,
        "attribution": aas.ATTRIBUTION,
    }


def activity_at(lat: float, lng: float, store: "aas.Store | None" = None):
    """(block, estimate) for one point. Never raises."""
    try:
        st = store if store is not None else aas.get_store()
        if not st.available:
            return _empty_block("unavailable", aas.UNAVAILABLE_MESSAGE), None
        c = st.city_for(lat, lng)
        if c is None:
            return _empty_block("not_covered", aas.NOT_COVERED_MESSAGE), None
        meta = c.meta
        block = _empty_block(c.coverage, None)
        block.update({
            "period": _period(meta),
            "definition": _definition(meta),
            "coverage": c.coverage,
            "coverage_note": meta.get("coverage_note"),
            "city": c.city,
        })
        if c.coverage == aas.COVERAGE_INSUFFICIENT:
            block["message"] = aas.INSUFFICIENT_MESSAGE
            return block, None
        if c.coverage not in (aas.COVERAGE_OK, aas.COVERAGE_LARGE_ONLY):
            block["status"] = aas.COVERAGE_INSUFFICIENT
            block["message"] = aas.INSUFFICIENT_MESSAGE
            return block, None
        i, j = c.cell_index(lat, lng)
        cell = c.cell_values(i, j)
        m = cell["metrics"]
        q = cell["quarters"]
        if c.coverage == aas.COVERAGE_OK:
            block.update({
                "flights_per_day": _r(m.get("passes_per_day")),
                "flights_per_night": _r(m.get("night_per_night")),
                "by_type_per_day": {"jet": _r(m.get("jet_per_day")),
                                    "light": _r(m.get("light_per_day")),
                                    "helicopter": _r(m.get("heli_per_day"))},
                "helicopter_per_night": _r(m.get("heli_night_per_night")),
                "typical_height_ft_agl": _int(m.get("median_agl_ft")),
            })
            qr = c.quarter_range(q, "passes_per_day")
            basis = "all"
        else:
            block["message"] = aas.LARGE_ONLY_MESSAGE
            block["undercounted"] = ["light", "helicopter"]
            block["by_type_per_day"] = {"jet": _r(m.get("jet_per_day")),
                                        "light": None, "helicopter": None}
            qr = c.quarter_range(q, "jet_per_day")
            basis = "jet"
        if qr:
            block["quarterly_flights_per_day"] = {
                "basis": basis, "min": _r(qr["min"]), "max": _r(qr["max"]),
                "quarters": qr["n_quarters"]}
        return block, estimate_lden(cell, c.coverage)
    except Exception:
        logger.exception("aircraft activity lookup failed")
        return _empty_block("error", "Aircraft activity could not be read."), None


def data_token(store: "aas.Store | None" = None) -> str:
    """Short token naming the data release, for cache signatures."""
    try:
        st = store if store is not None else aas.get_store()
        ends = sorted({str(c.meta.get("date_end")) for c in st.cities.values()})
        return ends[-1] if ends else "none"
    except Exception:
        return "error"
