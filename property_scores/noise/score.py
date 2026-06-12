"""
Multi-source noise score (v2) — AADT-calibrated.

Data hierarchy:
1. VicRoads AADT (ground truth, 14k+ monitored segments in VIC)
2. Overture speed_limit → calibrated AADT estimate
3. Overture road class → fallback AADT estimate

Sources: road traffic, rail/tram (from Overture rail subtype), aircraft (VicPlan overlays).
Propagation: CRTN L10 + duty-cycle correction + urban excess attenuation.

Score 0-100 where 100 = quietest.
"""

import logging
import math
import os

from property_scores.common.overture import get_db, roads_near, rail_near, aadt_near, nfdh_near, gtfs_rail_near
from property_scores.common.au_state import detect_state
from property_scores.noise.buildings import buildings_in_radius, barrier_attenuation, buildings_to_arrays
from property_scores.noise.aircraft import aircraft_noise_penalty
from property_scores.noise.terrain import terrain_attenuation

logger = logging.getLogger(__name__)

# Bump whenever the score formula, calibration, or any Lden-affecting logic
# changes. precompute_noise.py stamps this into every cached grid row and
# cache.py refuses to serve a cache built by a different version — so a stale
# precompute fails safe to live-compute instead of silently shadowing the live
# model (the overlay choropleth reads cached `score`). Keep it human-dated.
# AADT volume adjustment is opt-in. The transfer RF has road CLASS but no traffic
# VOLUME, so it over-reads suburban arterials whose measured AADT sits below their
# class implies (validated on 199 EIS measured points: residual vs log(actual/
# class-expected AADT) r=-0.42). When on, we pull the transfer road Lden DOWN
# (only) by K*log(measured_AADT / class_expected_AADT) where the road is quieter
# than its class — targeted, protects the genuine-loud end, leaves well-calibrated
# states untouched. K=4 chosen on the measured set (5-fold held-out MAE 7.93->7.43).
_AADT_ADJUST_ENABLED = os.environ.get("NOISE_AADT_ADJUST", "0") == "1"
_AADT_ADJUST_K = float(os.environ.get("NOISE_AADT_ADJUST_K", "4.0"))
_AADT_ADJUST_MAX_DB = 12.0   # clamp the pull-down so a tiny class-expected can't explode
_AADT_DOM_RADIUS_M = 150     # dominant source = max AADT within this radius

# Quiet-end recalibration is opt-in (NOISE_QUIET_RECAL=1). NSW's affine (fit on
# SoundPLAN urban facades) over-reads set-back suburban homes by ~+11 dB against
# clean Class-1 LAeq truth (NorthConnex 2026-06-10). transfer.quiet_relief pulls
# the affine back toward the RF raw at dwelling receptors, context-gated so the
# loud/kerbside anchor (Lake Macquarie ±1) is untouched. Read here only for the
# cache-version suffix; the relief itself lives on the transfer path.
_QUIET_RECAL_ENABLED = os.environ.get("NOISE_QUIET_RECAL", "0") == "1"

# Rail recalibration is opt-in (NOISE_RAIL_RECAL=1). The GTFS SEL rail model
# assumes flat-ground propagation, but Sydney's suburban heavy-rail corridors
# run in cuttings / behind noise barriers the model cannot see (and building
# screening is scaled down for rail, so close receptors get ~none). Against
# Class-1 LAeq truth (NorthConnex ONAR, 2026-06-11 analysis) the rail COMPONENT
# alone exceeds the measured TOTAL at cutting-adjacent homes — a hard physical
# bound on the over-read: 6 Duffy Ave (69 m) >= +12.9 dB, 18 Woniora Ave
# (116 m) >= +15.4 dB. The relief subtracts a flat, capped pull-down from the
# GTFS heavy-rail (train/vline) component in NSW only: spatially smooth (a
# constant on one component cannot flip overlay cells), pull-down only, trams
# and the no-GTFS fallback untouched. Default 8 dB sits well below both hard
# bounds — deliberately conservative because the corpus has no exposed
# at-grade/viaduct rail-side truth point to anchor the loud-rail end.
_RAIL_RECAL_ENABLED = os.environ.get("NOISE_RAIL_RECAL", "0") == "1"
_RAIL_RECAL_MAX_DB = 12.0  # never pull more than this, whatever the env says
_RAIL_RECAL_DB = max(0.0, min(_RAIL_RECAL_MAX_DB,
                              float(os.environ.get("NOISE_RAIL_RECAL_DB", "8.0"))))
_RAIL_RECAL_STATES = {"NSW"}
_RAIL_RECAL_TYPES = ("train", "vline")

# Bump on any scoring change. precompute_noise.py stamps this into every cached
# grid row and cache.py refuses to serve a cache built by a different version — so
# a stale precompute fails safe to live-compute instead of silently shadowing the
# live model (the overlay choropleth reads cached `score`). The AADT / quiet-recal
# suffixes only appear when their flag is ON, so enabling either invalidates the
# old cache (forcing regen) while default-OFF keeps the existing prod cache valid.
NOISE_MODEL_VERSION = ("2026-06-09-quincunx"
                       + ("-aadt" if _AADT_ADJUST_ENABLED else "")
                       + ("-nswquiet" if _QUIET_RECAL_ENABLED else "")
                       + ("-nswrail" if _RAIL_RECAL_ENABLED else ""))

# ML residual correction is opt-in (see noise_score). The shipped model was
# trained on the pre-fix physics and regresses/inverts against the corrected
# physics, so it stays off until retrained.
_ML_CORRECTION_ENABLED = os.environ.get("NOISE_ML_CORRECTION", "0") == "1"

# EU->AU transfer RF + per-state affine calibration is opt-in. When enabled it
# replaces the physics Lden with the transfer prediction (aircraft re-mixed in,
# since the RF is road/geometry only) and skips the ML residual block. Falls back
# to physics on any failure or when DEM/landcover coverage is missing.
_TRANSFER_ENABLED = os.environ.get("NOISE_TRANSFER", "0") == "1"

# Quiet-end physics anchor: the per-state affine is fit on urban facade samples
# (~46-78 dB Lden) and extrapolates badly below that support, lifting genuinely
# quiet low-traffic areas by ~10 dB. Where the motor-road network is sparse AND
# transfer reads louder than physics, blend toward physics. road_count gates out
# dense inner-city false-quiet (occlusion-suppressed but road-dense -> no trigger).
# Quiet-end physics anchor. The per-state affine is fit on urban facade samples
# (~46-78 dB) and over-predicts below that support. The correction weights physics
# in by how far the RF's OWN raw (EU-scale, pre-affine) sits into its quiet range
# — a smooth, geometry-averaged signal, so neighbouring overlay cells transition
# smoothly instead of flipping on an integer road count. road_count is only a
# binary veto, excluding road-dense occlusion-suppressed false-quiet (which must
# keep the transfer lift, e.g. Southbank). City raw ~60-66, rural raw ~47-49.
_QUIET_RAW_FLOOR = 58.0    # RF raw below which it is in its quiet, extrapolation-prone range
_QUIET_RAW_RANGE = 14.0    # ramp width from floor to full weight
_QUIET_W_MAX = 0.7         # max physics weight (deepest into the quiet range)
_QUIET_VETO_ROAD = 150     # road-dense => occlusion false-quiet, veto the correction
_NON_MOTOR = ("footway", "path", "steps", "cycleway", "pedestrian", "track")
_MAJOR_CLASSES = ("motorway", "trunk", "primary", "secondary", "tertiary")

# Human-friendly labels for dominant_source so the frontend never shows a bare
# Overture class ("tertiary") or a leaked numeric AADT station id ("05610").
_ROAD_CLASS_LABEL = {
    "motorway": "motorway", "trunk": "highway", "primary": "main road",
    "secondary": "main road", "tertiary": "local road",
    "residential": "residential street", "living_street": "residential street",
    "service": "minor road", "unclassified": "minor road", "track": "minor road",
    "pedestrian": "pedestrian way",
}
_RAIL_TYPE_LABEL = {"train": "railway", "tram": "tram", "vline": "regional railway", "rail": "railway"}


def _road_label(info: dict) -> str:
    """Real street name if present, else a readable class label. Suppresses
    numeric station ids that leak from some state AADT parquets into road_name."""
    name = info.get("road_name")
    if name and not str(name).strip().isdigit():
        return str(name)
    return _ROAD_CLASS_LABEL.get(info.get("class"), "road")


class _MLDisabled(Exception):
    """Internal sentinel to skip the ML block without logging a warning."""


# --- Calibrated AADT mappings (from VicRoads 2019 ground truth) ---
# VicRoads monitors arterials/highways, so these are MEDIAN values for
# monitored roads. True quiet residential streets are NOT in the dataset.
SPEED_TO_AADT: dict[int, int] = {
    110: 70_000,
    100: 53_000,  # VicRoads median
    90:  35_000,
    80:  16_000,  # VicRoads median 15,880
    70:  12_000,  # VicRoads median 12,411
    60:  8_000,   # VicRoads median 8,291
    50:  6_000,   # VicRoads median 5,982
    40:  7_000,   # VicRoads median 6,933
    30:  4_000,
    20:  2_000,
    10:  500,
    5:   100,
}

# For roads without speed limit AND without VicRoads match.
# Conservative: assumes these are the quieter roads VicRoads doesn't monitor.
CLASS_TO_AADT: dict[str, int] = {
    "motorway":     50_000,
    "trunk":        19_000,
    "primary":      11_000,
    "secondary":    6_000,
    "tertiary":     3_000,
    "residential":  400,    # true quiet back street, not VicRoads-monitored
    "unclassified": 300,
    "living_street": 100,
    "service":      150,
}

# Rail noise — SEL-based with actual frequency (PTV GTFS)
# (L_peak at ref_dist, ref_dist_m, pass_by_duration_s)
RAIL_EMISSION: dict[str, tuple[float, float, float]] = {
    "train":  (90.0, 25.0, 15.0),
    "vline":  (92.0, 25.0, 30.0),
    "tram":   (80.0, 7.5, 10.0),
}
# Fallback for Overture rail (no PTV match)
RAIL_NOISE_FALLBACK: dict[str, tuple[float, float]] = {
    "standard_gauge": (72.0, 25.0),
    "narrow_gauge":   (68.0, 25.0),
    "tram":           (65.0, 7.5),
}

GROUND_ABSORPTION_DB = 3.0
MIN_DISTANCE_M = 10.0
AMBIENT_DB = 35.0
EXCESS_ATTENUATION_DB_PER_M = 0.06
CONTINUOUS_FLOW_AADT = 15_000

ADAPTIVE_THRESHOLD_DB = 6  # include sources within 6 dB of loudest (>25% energy)
MAX_ROAD_SOURCES = 8
MAX_RAIL_SOURCES = 4
DEFAULT_SPEED_KMH = 60
NUM_FACADE_SECTORS = 8  # 45° each

# L10 → Leq: CRTN predicts L10(18h); Lden and validation use Leq
L10_TO_LEQ_DB = 3.0

# Austroads standard temporal traffic profile (urban arterial)
TRAFFIC_DAY_FRAC = 0.80    # 07:00-19:00 (12h)
TRAFFIC_EVE_FRAC = 0.12    # 19:00-23:00 (4h)
TRAFFIC_NIGHT_FRAC = 0.08  # 23:00-07:00 (8h)
_DAY_ADJ = 10 * math.log10(TRAFFIC_DAY_FRAC * 24 / 12)    # +2.04 dB
_EVE_ADJ = 10 * math.log10(TRAFFIC_EVE_FRAC * 24 / 4)     # -1.43 dB
_NIGHT_ADJ = 10 * math.log10(TRAFFIC_NIGHT_FRAC * 24 / 8)  # -6.20 dB


_CLASS_SPEED_AADT: dict[tuple[str, int], int] = {
    # (class, speed_bucket) → AADT — joint estimation
    ("motorway", 100): 70_000, ("motorway", 80): 50_000, ("motorway", 60): 30_000,
    ("trunk", 80): 30_000, ("trunk", 70): 20_000, ("trunk", 60): 15_000,
    ("primary", 70): 18_000, ("primary", 60): 13_000, ("primary", 50): 9_000,
    ("secondary", 70): 14_000, ("secondary", 60): 10_000, ("secondary", 50): 7_000,
    ("tertiary", 60): 5_000, ("tertiary", 50): 3_500, ("tertiary", 40): 2_500,
    ("residential", 50): 800, ("residential", 40): 500, ("residential", 30): 300,
}


def _estimate_aadt(road_class: str, speed_kmh: float | None) -> int:
    if speed_kmh is not None and speed_kmh > 0:
        bucket = int(round(speed_kmh / 10) * 10)
        joint = _CLASS_SPEED_AADT.get((road_class, bucket))
        if joint:
            return joint
        best_key = min(SPEED_TO_AADT.keys(), key=lambda k: abs(k - speed_kmh))
        return SPEED_TO_AADT[best_key]
    return CLASS_TO_AADT.get(road_class, 400)


def _crtn_noise(aadt: int, distance_m: float,
                hv_pct: float = 0.0, speed_kmh: float = 0.0) -> float:
    if aadt <= 0:
        return 0.0
    l10_ref = 42.2 + 10 * math.log10(aadt)
    if hv_pct > 0 and speed_kmh > 0:
        l10_ref += 10 * math.log10(1 + 5 * hv_pct / speed_kmh)
    if distance_m < MIN_DISTANCE_M:
        distance_m = MIN_DISTANCE_M
    geometric = 10 * math.log10(distance_m / 13.5)
    excess = max(0, (distance_m - 50)) * EXCESS_ATTENUATION_DB_PER_M
    duty_cycle = min(1.0, aadt / CONTINUOUS_FLOW_AADT)
    duty_correction = 10 * math.log10(duty_cycle) if duty_cycle > 0 else -30
    return max(l10_ref - geometric - GROUND_ABSORPTION_DB - excess + duty_correction, 0.0)


def _adaptive_select(levels: list[tuple[float, dict]],
                     max_n: int = MAX_ROAD_SOURCES) -> list[tuple[float, dict]]:
    if not levels:
        return []
    sorted_levels = sorted(levels, key=lambda x: x[0], reverse=True)
    peak = sorted_levels[0][0]
    filtered = [(l, d) for l, d in sorted_levels if l >= peak - ADAPTIVE_THRESHOLD_DB]
    return filtered[:max_n]


RAIL_EXCESS_ATTEN_DB_PER_M = 0.04  # ground/atmospheric beyond 50m


def _rail_noise_freq(rail_type: str, distance_m: float,
                     services_per_hour: float) -> float:
    """SEL-based rail noise using actual service frequency."""
    if rail_type not in RAIL_EMISSION or services_per_hour <= 0:
        return 0.0
    l_peak, ref_dist, duration = RAIL_EMISSION[rail_type]
    if distance_m < MIN_DISTANCE_M:
        distance_m = MIN_DISTANCE_M
    sel = l_peak + 10 * math.log10(duration)
    leq = sel + 10 * math.log10(services_per_hour / 3600)
    dist_atten = 10 * math.log10(distance_m / ref_dist)
    excess = max(0, (distance_m - 50)) * RAIL_EXCESS_ATTEN_DB_PER_M
    return max(leq - dist_atten - GROUND_ABSORPTION_DB - excess, 0.0)


def _rail_noise_fallback(rail_class: str, distance_m: float) -> float:
    """Fallback for Overture rail segments without PTV frequency data."""
    if rail_class not in RAIL_NOISE_FALLBACK:
        return 0.0
    l_ref, ref_dist = RAIL_NOISE_FALLBACK[rail_class]
    if distance_m < MIN_DISTANCE_M:
        distance_m = MIN_DISTANCE_M
    excess = max(0, (distance_m - 50)) * RAIL_EXCESS_ATTEN_DB_PER_M
    return max(l_ref - 10 * math.log10(distance_m / ref_dist) - GROUND_ABSORPTION_DB - excess, 0.0)


def _energy_sum(*levels: float) -> float:
    e = sum(10 ** (l / 10) for l in levels if l > 0)
    return 10 * math.log10(e) if e > 0 else 0.0


def _bearing(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    dx = (lng2 - lng1) * math.cos(math.radians((lat1 + lat2) / 2))
    dy = lat2 - lat1
    return math.atan2(dx, dy) % (2 * math.pi)


def _facade_lden(sources: list[tuple[float, float]], aircraft_db: float) -> dict:
    """Compute Lden per facade sector from directional sources.

    sources: list of (l10_db, bearing_rad) for road sources, or (leq_db, bearing_rad) for rail.
    Returns dict with lden_max, lden_min, and per-sector values.
    """
    sector_width = 2 * math.pi / NUM_FACADE_SECTORS
    sector_road: list[list[float]] = [[] for _ in range(NUM_FACADE_SECTORS)]
    sector_rail: list[list[float]] = [[] for _ in range(NUM_FACADE_SECTORS)]

    for db_val, bearing, is_rail in sources:
        if db_val <= 0:
            continue
        idx = int(bearing / sector_width) % NUM_FACADE_SECTORS
        if is_rail:
            sector_rail[idx].append(db_val)
        else:
            sector_road[idx].append(db_val)

    # Raw energy per sector (road + rail separately for temporal weighting)
    sector_road_e = []
    sector_rail_e = []
    for i in range(NUM_FACADE_SECTORS):
        sector_road_e.append(sum(10 ** (l / 10) for l in sector_road[i]))
        sector_rail_e.append(sum(10 ** (l / 10) for l in sector_rail[i]))

    # Diffraction spillover: each sector bleeds 10% energy to each neighbor
    SPILL = 0.10
    KEEP = 1.0 - 2 * SPILL  # 0.80
    n_sec = NUM_FACADE_SECTORS
    road_e_smooth = [
        KEEP * sector_road_e[i]
        + SPILL * sector_road_e[(i - 1) % n_sec]
        + SPILL * sector_road_e[(i + 1) % n_sec]
        for i in range(n_sec)
    ]
    rail_e_smooth = [
        KEEP * sector_rail_e[i]
        + SPILL * sector_rail_e[(i - 1) % n_sec]
        + SPILL * sector_rail_e[(i + 1) % n_sec]
        for i in range(n_sec)
    ]

    sector_ldens = []
    for i in range(n_sec):
        road_db = 10 * math.log10(road_e_smooth[i]) if road_e_smooth[i] > 0 else 0.0
        rail_db = 10 * math.log10(rail_e_smooth[i]) if rail_e_smooth[i] > 0 else 0.0

        road_leq = (road_db - L10_TO_LEQ_DB) if road_db > 0 else 0.0
        rail_leq = rail_db
        aircraft_leq = aircraft_db

        leq_d = max(_energy_sum(
            road_leq + _DAY_ADJ if road_leq > 0 else 0,
            rail_leq, aircraft_leq), AMBIENT_DB)
        leq_e = max(_energy_sum(
            road_leq + _EVE_ADJ if road_leq > 0 else 0,
            max(rail_leq - 5, 0) if rail_leq > 0 else 0,
            aircraft_leq), AMBIENT_DB)
        leq_n = max(_energy_sum(
            road_leq + _NIGHT_ADJ if road_leq > 0 else 0,
            max(rail_leq - 10, 0) if rail_leq > 0 else 0, aircraft_leq), AMBIENT_DB)

        sector_ldens.append(round(_lden(leq_d, leq_e, leq_n), 1))

    if not sector_ldens or max(sector_ldens) <= AMBIENT_DB:
        return {}

    labels = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    max_idx = sector_ldens.index(max(sector_ldens))
    min_idx = sector_ldens.index(min(sector_ldens))

    return {
        "lden_max_facade": max(sector_ldens),
        "lden_min_facade": min(sector_ldens),
        "max_facade_dir": labels[max_idx],
        "min_facade_dir": labels[min_idx],
        "facade_range_db": round(max(sector_ldens) - min(sector_ldens), 1),
        "facade_sectors": dict(zip(labels, sector_ldens)),
    }


def _lden(leq_day: float, leq_eve: float, leq_night: float) -> float:
    """EU/AU Lden from period Leq values."""
    return 10 * math.log10(
        (12 * 10 ** (leq_day / 10)
         + 4 * 10 ** ((leq_eve + 5) / 10)
         + 8 * 10 ** ((leq_night + 10) / 10)) / 24
    )


def _lden_to_score(lden: float) -> int:
    """Map an Lden (dB) to the 0-100 quiet score with the loud-end re-anchor.
    Keeps the calibrated 40-70 dB mapping exactly (where the property mass sits)
    but spreads the >70 dB tail so 75 vs 88 dB rank-order instead of all
    collapsing to 0. Continuous at 70 dB (both branches = 14). Single source of
    truth for the score curve — used by noise_score, physics_score, and the
    quincunx cell aggregator."""
    if lden <= 70:
        return max(0, min(100, round((75 - lden) / 35 * 100)))
    return max(0, min(14, round(14 * (88 - lden) / 18)))


def _score_label(score: int) -> str:
    """Quiet-score band label. Single source of truth so the overlay cell
    aggregator and the live score never disagree (a cell must not show
    score=31 with label='Quiet')."""
    if score >= 80:
        return "Very Quiet"
    if score >= 60:
        return "Quiet"
    if score >= 40:
        return "Moderate"
    if score >= 20:
        return "Loud"
    return "Very Loud"


def _rail_recal_db(rail_type: str, state: str | None) -> float:
    """Bounded pull-down (dB) for one GTFS rail source. 0 unless the flag is on,
    the state is covered (NSW) and the type is heavy rail (train/vline). Flat in
    space (smooth by construction), pull-down only, capped at _RAIL_RECAL_MAX_DB."""
    if not _RAIL_RECAL_ENABLED or state not in _RAIL_RECAL_STATES:
        return 0.0
    if rail_type not in _RAIL_RECAL_TYPES:
        return 0.0
    return _RAIL_RECAL_DB


def _aadt_adjustment(dom_aadt: float, exp_aadt: float,
                     k: float | None = None, max_db: float | None = None) -> float:
    """Pull-down (<=0) dB for the transfer road Lden when the dominant nearby
    road's MEASURED AADT is below what its OSM class implies. The transfer RF
    sees road class but no traffic volume, so it over-reads quiet suburban
    arterials; this corrects only that. Returns 0 for roads at/above class
    expectation (the RF is not over-reading those, e.g. genuine freeways), and is
    clamped to [-max_db, 0] so a tiny class-expected AADT can't explode. Validated
    on 199 EIS measured points (K=4, 5-fold held-out MAE 7.93->7.43)."""
    k = _AADT_ADJUST_K if k is None else k
    max_db = _AADT_ADJUST_MAX_DB if max_db is None else max_db
    if dom_aadt <= 0:
        return 0.0
    lr = math.log(dom_aadt / max(exp_aadt, 1.0))
    return max(-max_db, min(0.0, k * lr))


def cell_score(lat: float, lng: float, cell_m: float = 220.0,
               *, source: str | None = None) -> dict:
    """Overlay-cell score via a quincunx (centre + 4 corners) energy-mean.

    A 220 m choropleth cell coloured by a single centre point flips colour on
    whether that one point landed on or off a road. Sampling 5 points spread
    across the cell and energy-averaging their Lden makes the cell reflect its
    whole area. This is 5x noise_score so it is for the PRECOMPUTE only
    (scripts/precompute_noise.py); the live overlay path stays single-point
    noise_score() because 5x ~543 ms/cell is too slow interactively.

    Returns the centre point's full dict with `estimated_db`/`score` replaced by
    the quincunx energy-mean, plus `cell_quincunx`/`cell_n`.
    """
    off = cell_m * 0.354  # cell_m/(2√2) per axis -> sample corners at cell_m/2 diagonal (interior, ~70% toward each corner)
    dlat = off / 111_320.0
    dlng = off / (111_320.0 * max(math.cos(math.radians(lat)), 1e-6))
    offsets = [(0.0, 0.0), (dlat, dlng), (dlat, -dlng), (-dlat, dlng), (-dlat, -dlng)]
    center = None
    ldens = []
    for i, (oa, ob) in enumerate(offsets):
        r = noise_score(lat + oa, lng + ob, source=source)
        if i == 0:
            center = r
        ld = r.get("lden_db")
        if ld is None:
            ld = r.get("estimated_db")
        if ld is not None:
            ldens.append(ld)
    if center is None:
        return {}
    if not ldens:
        return center
    mean_lden = 10 * math.log10(sum(10 ** (l / 10) for l in ldens) / len(ldens))
    out = dict(center)
    out["estimated_db"] = round(mean_lden, 1)
    out["lden_db"] = round(mean_lden, 1)
    out["score"] = _lden_to_score(mean_lden)
    # Re-derive every band/CI field from the quincunx mean so the cached cell is
    # self-consistent. Without this the row keeps the CENTRE point's label/CI
    # (e.g. score=31 'Loud' with label='Quiet'). dominant_source/road_db stay the
    # centre point's — they describe the representative point, not the area mean.
    out["label"] = _score_label(out["score"])
    ci = center.get("confidence_range_db", 5.0)
    out["estimated_db_low"] = round(max(mean_lden - ci, AMBIENT_DB), 1)
    out["estimated_db_high"] = round(mean_lden + ci, 1)
    out["cell_quincunx"] = True
    out["cell_n"] = len(ldens)
    return out


def noise_score(lat: float, lng: float, radius_m: int = 500,
                *, source: str | None = None) -> dict:
    db = get_db()
    state = detect_state(lat, lng)

    # --- Pre-fetch buildings once for screening calculations ---
    nearby_buildings = buildings_in_radius(db, lat, lng, radius_m)
    _bldg_arrays = buildings_to_arrays(nearby_buildings)

    # Collect all sources with bearing for facade analysis: (db, bearing, is_rail)
    _all_directional_sources: list[tuple[float, float, bool]] = []

    # --- Measured AADT: per-state aadt_*.parquet ---
    aadt_segments_raw = aadt_near(db, lat, lng, radius_m)
    # Collapse to ONE source per road, keeping the NEAREST point. A road must
    # contribute once (from its closest approach), not once per sample point —
    # otherwise dense point datasets (e.g. QLD has a point every ~10 m along the
    # same road) get summed as many independent sources and inflate the level by
    # 10-20 dB. Roads without a name fall back to a coarse location bucket.
    _seen: dict[tuple, tuple] = {}
    for row in aadt_segments_raw:
        aadt_val, _, road_name, dist_m, near_lng, near_lat = row
        if road_name:
            key = ("name", road_name)
        else:
            key = ("loc", round(near_lng, 3), round(near_lat, 3))
        cur = _seen.get(key)
        if cur is None or dist_m < cur[3]:  # keep nearest approach of this road
            _seen[key] = row
    aadt_segments = list(_seen.values())

    aadt_levels: list[tuple[float, dict]] = []
    building_screening_total = 0.0
    for aadt, hv_pct, road_name, dist_m, src_lng, src_lat in aadt_segments:
        hv_val = (hv_pct * 100) if hv_pct else 0.0
        l_db = _crtn_noise(int(aadt), dist_m, hv_pct=hv_val, speed_kmh=DEFAULT_SPEED_KMH)
        if l_db > 0:
            screening = barrier_attenuation(nearby_buildings, src_lng, src_lat, lng, lat, dist_m, _arrays=_bldg_arrays)
            l_db_screened = max(l_db - screening, 0.0)
            if screening > building_screening_total:
                building_screening_total = screening
            aadt_levels.append((l_db_screened, {
                "source": "vicroads",
                "road_name": road_name,
                "aadt": int(aadt),
                "hv_pct": round(hv_val),
                "distance_m": round(dist_m, 0),
                "db": round(l_db_screened, 1),
                "screening_db": round(screening, 1),
                "src_lng": src_lng,
                "src_lat": src_lat,
            }))
            _all_directional_sources.append((l_db_screened, _bearing(lat, lng, src_lat, src_lng), False))

    # NFDH national traffic counts (complements VicRoads outside VIC)
    nfdh_stations = nfdh_near(db, lat, lng, radius_m)
    for aadt, hv_pct, road_name, dist_m, src_lng, src_lat in nfdh_stations:
        if any(abs(dist_m - d) < 80 for _, _, _, d, _, _ in aadt_segments):
            continue
        hv_val = max(hv_pct or 0, 0)
        l_db = _crtn_noise(int(aadt), dist_m, hv_pct=hv_val, speed_kmh=DEFAULT_SPEED_KMH)
        if l_db > 0:
            screening = barrier_attenuation(nearby_buildings, src_lng, src_lat, lng, lat, dist_m, _arrays=_bldg_arrays)
            l_db_screened = max(l_db - screening, 0.0)
            if screening > building_screening_total:
                building_screening_total = screening
            aadt_levels.append((l_db_screened, {
                "source": "nfdh",
                "road_name": road_name,
                "aadt": int(aadt),
                "hv_pct": round(hv_val),
                "distance_m": round(dist_m, 0),
                "db": round(l_db_screened, 1),
                "screening_db": round(screening, 1),
                "src_lng": src_lng,
                "src_lat": src_lat,
            }))
            _all_directional_sources.append((l_db_screened, _bearing(lat, lng, src_lat, src_lng), False))

    # --- Overture roads (fill gaps: residential streets not in measured AADT) ---
    # Dedup: skip Overture major roads within 80m of any measured AADT source
    measured_distances = ([d for _, _, _, d, _, _ in aadt_segments]
                         + [d for _, _, _, d, _, _ in nfdh_stations])
    roads = roads_near(db, lat, lng, radius_m, source=source)
    overture_levels: list[tuple[float, dict]] = []
    roads_with_speed = 0

    for road_class, dist_m, speed_kmh, src_lng, src_lat in roads:
        if road_class in ("footway", "path", "steps", "cycleway", "pedestrian", "track"):
            continue
        if speed_kmh:
            roads_with_speed += 1
        if road_class in ("motorway", "trunk", "primary", "secondary"):
            if any(abs(dist_m - vd) < 80 for vd in measured_distances):
                continue
        aadt_est = CLASS_TO_AADT.get(road_class, 400)
        l_db = _crtn_noise(aadt_est, dist_m)
        if l_db > 0:
            screening = barrier_attenuation(nearby_buildings, src_lng, src_lat, lng, lat, dist_m, _arrays=_bldg_arrays)
            l_db = max(l_db - screening, 0.0)
            if screening > building_screening_total:
                building_screening_total = screening
            if l_db <= 0:
                continue
            overture_levels.append((l_db, {
                "source": "overture",
                "class": road_class,
                "speed_kmh": speed_kmh,
                "aadt_est": aadt_est,
                "distance_m": round(dist_m, 0),
                "db": round(l_db, 1),
                "src_lng": src_lng,
                "src_lat": src_lat,
            }))
            _all_directional_sources.append((l_db, _bearing(lat, lng, src_lat, src_lng), False))

    # Merge: prefer measured AADT for loud sources, add Overture for minor roads
    all_road_levels = aadt_levels + overture_levels
    top_roads = _adaptive_select(all_road_levels)
    road_energy = sum(10 ** (l / 10) for l, _ in top_roads)
    road_db = 10 * math.log10(road_energy) if road_energy > 0 else 0.0

    # --- Rail/tram (PTV GTFS with real frequencies) ---
    gtfs_routes = gtfs_rail_near(db, lat, lng, radius_m)
    gtfs_found = len(gtfs_routes) > 0
    # Aggregate GTFS fragments by physical route before classifying/summing.
    # GTFS splits one line into many sub-shapes (express, short-workings); each
    # carries only a fraction of the true frequency, so a per-fragment peak_svc
    # would (a) mis-classify a busy line as low-frequency 'vline' off one quiet
    # fragment and (b) double-count the same line when rail sources are energy-
    # summed. Collapse to one entry per (route_name, route_type): nearest fragment
    # for distance/geometry, max frequency across fragments for classification.
    _agg: dict = {}
    for route_type, route_name, dist_m, peak_svc, offpeak_svc, src_lng, src_lat in gtfs_routes:
        key = (route_name, route_type)
        cur = _agg.get(key)
        if cur is None:
            _agg[key] = {"route_type": route_type, "route_name": route_name,
                         "dist_m": dist_m, "src_lng": src_lng, "src_lat": src_lat,
                         "peak": peak_svc, "offpeak": offpeak_svc}
        else:
            if dist_m < cur["dist_m"]:
                cur["dist_m"], cur["src_lng"], cur["src_lat"] = dist_m, src_lng, src_lat
            cur["peak"] = max(cur["peak"], peak_svc)
            cur["offpeak"] = max(cur["offpeak"], offpeak_svc)

    rail_levels: list[tuple[float, dict]] = []
    nearest_tram_m = None
    nearest_train_m = None

    for r in _agg.values():
        route_type, route_name, dist_m = r["route_type"], r["route_name"], r["dist_m"]
        peak_svc, offpeak_svc = r["peak"], r["offpeak"]
        src_lng, src_lat = r["src_lng"], r["src_lat"]
        if route_type == 0:
            rail_type = "tram"
            if nearest_tram_m is None or dist_m < nearest_tram_m:
                nearest_tram_m = dist_m
        else:
            rail_type = "vline" if peak_svc < 4 else "train"
            if nearest_train_m is None or dist_m < nearest_train_m:
                nearest_train_m = dist_m
        svc_per_hr = peak_svc * 0.4 + offpeak_svc * 0.6
        l_db = _rail_noise_freq(rail_type, dist_m, svc_per_hr)
        if l_db > 0:
            raw_screening = barrier_attenuation(nearby_buildings, src_lng, src_lat, lng, lat, dist_m, _arrays=_bldg_arrays)
            rail_scr_factor = min(dist_m / 500, 0.6)  # 0 at 0m → 0.6 at 500m
            screening = raw_screening * rail_scr_factor
            l_db_screened = max(l_db - screening, 0.0)
            # NSW heavy-rail recalibration (opt-in): subtract the bounded
            # cutting/barrier pull-down at the SOURCE level so everything
            # downstream (Lden mix, night level, facade sectors, dominant
            # source, reported rail_db) stays self-consistent.
            _recal = _rail_recal_db(rail_type, state)
            if _recal > 0:
                l_db_screened = max(l_db_screened - _recal, 0.0)
            if raw_screening > building_screening_total:
                building_screening_total = raw_screening
            if l_db_screened <= 0:
                continue
            rail_levels.append((l_db_screened, {
                "source": "gtfs",
                "type": rail_type,
                "route": route_name,
                "distance_m": round(dist_m, 0),
                "peak_svc_hr": round(peak_svc, 1),
                "offpeak_svc_hr": round(offpeak_svc, 1),
                "db": round(l_db_screened, 1),
                "screening_db": round(screening, 1),
            }))
            _all_directional_sources.append((l_db_screened, _bearing(lat, lng, src_lat, src_lng), True))

    if not gtfs_found:
        rails = rail_near(db, lat, lng, radius_m, source=source)
        for rail_class, dist_m in rails:
            l_db = _rail_noise_fallback(rail_class, dist_m)
            if l_db > 0:
                rail_levels.append((l_db, {
                    "source": "overture",
                    "class": rail_class,
                    "distance_m": round(dist_m, 0),
                    "db": round(l_db, 1),
                }))
            if rail_class == "tram" and (nearest_tram_m is None or dist_m < nearest_tram_m):
                nearest_tram_m = dist_m
            if rail_class in ("standard_gauge", "narrow_gauge") and (nearest_train_m is None or dist_m < nearest_train_m):
                nearest_train_m = dist_m

    top_rails = _adaptive_select(rail_levels, max_n=MAX_RAIL_SOURCES)
    rail_energy = sum(10 ** (l / 10) for l, _ in top_rails)
    rail_db = 10 * math.log10(rail_energy) if rail_energy > 0 else 0.0

    # --- Aircraft noise ---
    aircraft = aircraft_noise_penalty(lat, lng)
    aircraft_db = 0.0
    if aircraft["penalty_db"] > 0:
        aircraft_db = AMBIENT_DB + aircraft["penalty_db"]

    # --- Terrain screening (DEM-based, for dominant source >200m) ---
    terrain_screening = 0.0
    dominant_all = sorted(
        [(l, d) for l, d in all_road_levels + rail_levels],
        key=lambda x: x[0], reverse=True,
    )
    if dominant_all:
        dom_db, dom_info = dominant_all[0]
        dom_dist = dom_info.get("distance_m", 0)
        if dom_dist > 200:
            dom_src_lng = dom_info.get("near_lng") or dom_info.get("src_lng")
            dom_src_lat = dom_info.get("near_lat") or dom_info.get("src_lat")
            if dom_src_lng and dom_src_lat:
                terrain_screening = terrain_attenuation(
                    dom_src_lat, dom_src_lng, lat, lng, dom_dist,
                )

    if terrain_screening > 0:
        road_db = max(0, road_db - terrain_screening * 0.7)
        rail_db = max(0, rail_db - terrain_screening)

    # --- L10 → Leq + Lden (time-of-day) ---
    road_leq = (road_db - L10_TO_LEQ_DB) if road_db > 0 else 0.0
    rail_leq = rail_db
    aircraft_leq = aircraft_db
    # Night rail level by type: train/vline carry overnight freight (-10 dB);
    # trams largely stop overnight (-20 dB), so a tram-only facade isn't
    # over-credited at night. Built per source from the selected rail levels.
    _rn_e = sum(10 ** ((_l - (20 if _i.get("type") == "tram" else 10)) / 10)
                for _l, _i in top_rails)
    rail_night_leq = 10 * math.log10(_rn_e) if _rn_e > 0 else 0.0

    leq_24h = max(_energy_sum(road_leq, rail_leq, aircraft_leq), AMBIENT_DB)

    # Period Leq — road: Austroads temporal profile; rail: day+evening only
    leq_day_val = max(_energy_sum(
        road_leq + _DAY_ADJ if road_leq > 0 else 0,
        rail_leq,
        aircraft_leq), AMBIENT_DB)
    leq_eve_val = max(_energy_sum(
        road_leq + _EVE_ADJ if road_leq > 0 else 0,
        max(rail_leq - 5, 0) if rail_leq > 0 else 0,
        aircraft_leq), AMBIENT_DB)
    # Rail night uses rail_night_leq (per-type: train/vline freight-active,
    # trams mostly stop); Lden then applies its +10 dB night penalty.
    leq_night_val = max(_energy_sum(
        road_leq + _NIGHT_ADJ if road_leq > 0 else 0,
        rail_night_leq,
        aircraft_leq), AMBIENT_DB)

    lden = _lden(leq_day_val, leq_eve_val, leq_night_val)
    # Physics over-call guard: the CRTN energy-sum fallback can produce
    # implausible Lden in dense multi-arterial cells (>84 dB); real free-field
    # road Lden plateaus ~80-82. Cap so the saturated tail can't drive an absurd
    # fallback estimate (clips only clear artefacts; the loud truth decile <=78
    # is untouched). Mainly the NOISE_TRANSFER=0 / raster-miss path.
    lden = min(lden, 82.0)

    # --- EU->AU transfer RF + per-state affine calibration (opt-in) ---
    # Replaces the physics Lden with the geometry-trained transfer prediction.
    # The RF is road/geometry only (no aircraft), so any aircraft penalty is
    # re-mixed on top. Falls back to physics if the model is unavailable, the
    # point is outside DEM/landcover coverage, or anything raises.
    physics_lden = lden
    lden_source = "physics"
    transfer_raw = None
    _quiet_w = 0.0  # >0 when the quiet-end out-of-support correction is active
    if _TRANSFER_ENABLED:
        try:
            from property_scores.noise.transfer import transfer_lden
            # The RF predicts road-only Lden. Replace ONLY the road contribution and
            # keep rail/tram + aircraft on their exact physics time-of-day mix, so a
            # point near a railway/tram (or under a flight path) is not under-counted.
            t_road_lden, t_raw, raster_ok = transfer_lden(db, lat, lng, state)
            if not raster_ok:
                raise ValueError("raster miss -> physics")
            # Road-only physics Lden (blend anchor + transfer delta basis).
            rd_only_lden = _lden(
                road_leq + _DAY_ADJ, road_leq + _EVE_ADJ, road_leq + _NIGHT_ADJ,
            ) if road_leq > 0 else 0.0
            # Quiet-end physics anchor (smooth, out-of-support correction). Weight
            # physics in by how far the RF's own raw sits into its quiet range
            # (_QUIET_RAW_FLOOR/RANGE) — a continuous geometry-averaged signal, so
            # adjacent overlay cells transition smoothly. road_count is only a binary
            # veto excluding road-dense occlusion-suppressed false-quiet. Compared
            # road-vs-road so rail/aircraft are untouched. quiet_w>0 marks an
            # out-of-support extrapolation => low confidence (surfaced below).
            n_motor = sum(1 for r in roads if r[0] not in _NON_MOTOR)
            quiet_w = 0.0
            if n_motor < _QUIET_VETO_ROAD and t_road_lden > rd_only_lden:
                below = _QUIET_RAW_FLOOR - t_raw
                quiet_w = _QUIET_W_MAX * max(0.0, min(1.0, below / _QUIET_RAW_RANGE))
                if quiet_w > 0:
                    t_road_lden = (1.0 - quiet_w) * t_road_lden + quiet_w * rd_only_lden
            # AADT volume adjustment (opt-in): pull the road Lden DOWN where the
            # nearby road's MEASURED AADT is below what its OSM class implies (the
            # RF only sees class, so it over-reads quiet suburban arterials). Only
            # the road contribution is touched (rail/aircraft re-mixed below);
            # pull-down only, so genuine-loud roads (high AADT) are never lifted.
            if _AADT_ADJUST_ENABLED and aadt_segments and t_road_lden > 0:
                dom_aadt = max((seg[0] for seg in aadt_segments
                                if seg[3] <= _AADT_DOM_RADIUS_M), default=0)
                majors = [r for r in roads if r[0] in _MAJOR_CLASSES]
                if dom_aadt > 0 and majors:
                    nearest_major = min(majors, key=lambda r: r[1])[0]
                    adj = _aadt_adjustment(dom_aadt, _estimate_aadt(nearest_major, None))
                    if adj < 0:
                        t_road_lden = max(t_road_lden + adj, 0.0)
            if road_leq > 0:
                # Shift the road time-of-day Leqs by the transfer adjustment, then
                # recombine with the exact rail + aircraft mix (same period logic as
                # physics above), so rail/tram and aircraft are fully preserved.
                road_leq_t = road_leq + (t_road_lden - rd_only_lden)
                leq_day_t = max(_energy_sum(
                    road_leq_t + _DAY_ADJ, rail_leq, aircraft_leq), AMBIENT_DB)
                leq_eve_t = max(_energy_sum(
                    road_leq_t + _EVE_ADJ,
                    max(rail_leq - 5, 0) if rail_leq > 0 else 0,
                    aircraft_leq), AMBIENT_DB)
                leq_night_t = max(_energy_sum(
                    road_leq_t + _NIGHT_ADJ,
                    rail_night_leq,
                    aircraft_leq), AMBIENT_DB)
                lden = _lden(leq_day_t, leq_eve_t, leq_night_t)
            else:
                # No road contribution: rail + aircraft (+ ambient) only; the RF's
                # road prediction is moot, so keep the physics rail/aircraft mix.
                lden = physics_lden
            transfer_raw = round(t_raw, 1)
            _quiet_w = quiet_w
            lden_source = "transfer"
        except Exception:
            logger.warning("transfer fallback to physics", exc_info=True)
            lden = physics_lden  # explicit: keep physics value

    # --- ML residual correction ---
    # Disabled by default. The XGBoost residual model (noise_ml_model_la50.pkl)
    # was trained on the OLD physics outputs (no VicRoads AADT, 25 dB screening
    # cap). Against the corrected physics its residual is miscalibrated: it
    # regresses every location toward ~52-57 dB Lden, which flattens and even
    # inverts the city-vs-country ordering (validated 2026-06-06: separation gap
    # 30 -> ~0 with ML on). Production was already running raw physics (the live
    # service returned lden == physics_lden), so this keeps current behaviour and
    # blocks the broken correction from activating on restart. Re-enable with
    # NOISE_ML_CORRECTION=1 only after retraining the model on the corrected
    # physics + fresh field measurements.
    # Build feature dict from already-computed physics values for XGBoost
    ml_lden = lden  # raw physics Lden unless ML correction is enabled
    try:
        # Transfer and ML are mutually exclusive: if the transfer RF already
        # produced lden, skip the ML residual block entirely.
        if lden_source == "transfer" or not _ML_CORRECTION_ENABLED:
            raise _MLDisabled
        from property_scores.noise.ml_model import predict_correction
        import numpy as np

        motor_roads_ml = [r for r in roads if r[0] not in
                          ("footway", "path", "steps", "cycleway", "pedestrian", "track")]
        m_per_deg = 111_320 * math.cos(math.radians(lat))

        # Building features
        heights = [h for h, _, _ in nearby_buildings] if nearby_buildings else [0]
        inner_bldgs = [h for h, clng, clat in nearby_buildings
                       if math.sqrt(((clng - lng) * m_per_deg) ** 2 +
                                    ((clat - lat) * 111_320) ** 2) < 100]

        # Road energy features (unscreened + screened)
        road_energies_ml, screened_energies_ml = [], []
        max_screening_ml = 0
        for cls, dist_m, speed, slng, slat in motor_roads_ml:
            aadt_est = CLASS_TO_AADT.get(cls, 400)
            db_val = _crtn_noise(aadt_est, dist_m)
            if db_val > 0:
                road_energies_ml.append(db_val)
                scr = barrier_attenuation(nearby_buildings, slng, slat, lng, lat, dist_m, _arrays=_bldg_arrays)
                s_val = max(db_val - scr, 0)
                if s_val > 0:
                    screened_energies_ml.append(s_val)
                max_screening_ml = max(max_screening_ml, scr)

        # Directional sector features
        sector_energy = [0.0] * NUM_FACADE_SECTORS
        sector_width = 2 * math.pi / NUM_FACADE_SECTORS
        for cls, dist_m, speed, slng, slat in motor_roads_ml:
            aadt_est = CLASS_TO_AADT.get(cls, 400)
            db_val = _crtn_noise(aadt_est, dist_m)
            if db_val > 0:
                bearing = _bearing(lat, lng, slat, slng)
                sector_energy[int(bearing / sector_width) % NUM_FACADE_SECTORS] += 10 ** (db_val / 10)
        sector_db = [10 * math.log10(e) if e > 0 else 0 for e in sector_energy]
        active_sectors = [s for s in sector_db if s > 0]

        # Rail features
        rail_raw_ml = max((l for l, _ in rail_levels), default=0) if rail_levels else 0
        rail_scr_ml = max((l for l, _ in top_rails), default=0) if top_rails else 0

        # POI features
        poi_noise_count, poi_noise_min_dist, poi_total_count = 0, 500, 0
        try:
            from property_scores.common.overture import pois_near
            pois = pois_near(db, lat, lng, 500)
            poi_total_count = len(pois)
            noise_cats = {"bar", "nightclub", "pub", "restaurant", "cafe",
                          "construction", "factory", "industrial"}
            noise_pois = [p for p in pois if p[0] and any(c in p[0].lower() for c in noise_cats)]
            poi_noise_count = len(noise_pois)
            poi_noise_min_dist = min((p[1] for p in noise_pois), default=500)
        except Exception:
            pass

        # Major road distance
        major = ("motorway", "trunk", "primary", "secondary", "tertiary")
        nearest_major_dist = min((r[1] for r in motor_roads_ml if r[0] in major), default=radius_m)

        ml_features = {
            "building_count": len(nearby_buildings),
            "building_count_100m": len(inner_bldgs),
            "building_height_100m_mean": float(np.mean(inner_bldgs)) if inner_bldgs else 0,
            "building_height_max": max(heights),
            "building_height_mean": float(np.mean(heights)),
            "building_height_p75": float(np.percentile(heights, 75)) if len(heights) > 1 else heights[0],
            "canyon_ratio": (float(np.mean(inner_bldgs)) if inner_bldgs else float(np.mean(heights))) / max(nearest_major_dist, 5),
            "density_ratio": len(inner_bldgs) / max(len(nearby_buildings), 1),
            "max_screening_db": max_screening_ml,
            "nearest_major_dist": nearest_major_dist,
            "nfdh_count": len(nfdh_stations),
            "nfdh_max_aadt": max((n[0] for n in nfdh_stations), default=0),
            "nfdh_nearest_dist": min((n[3] for n in nfdh_stations), default=radius_m),
            "physics_lden": round(lden, 1),
            "physics_max_facade": round(lden, 1),
            "physics_min_facade": round(max(lden - (max(sector_db) - min(active_sectors) if active_sectors else 0), AMBIENT_DB), 1),
            "physics_rail_db": round(rail_db, 1),
            "physics_road_db": round(road_db, 1),
            "physics_score": _lden_to_score(lden),
            "poi_noise_count": poi_noise_count,
            "poi_noise_min_dist": poi_noise_min_dist,
            "poi_total_count": poi_total_count,
            "rail_raw_db_max": rail_raw_ml,
            "rail_route_count": len(gtfs_routes),
            "rail_screened_db_max": rail_scr_ml,
            "rail_screening_delta": rail_raw_ml - rail_scr_ml,
            "road_count": len(motor_roads_ml),
            "road_db_max": max(road_energies_ml) if road_energies_ml else 0,
            "road_db_mean": float(np.mean(road_energies_ml)) if road_energies_ml else 0,
            "road_db_sum_energy": 10 * math.log10(sum(10 ** (e / 10) for e in road_energies_ml)) if road_energies_ml else 0,
            "road_screened_max": max(screened_energies_ml) if screened_energies_ml else 0,
            "road_screened_sum": 10 * math.log10(sum(10 ** (e / 10) for e in sorted(screened_energies_ml, reverse=True)[:8])) if screened_energies_ml else 0,
            "roads_with_speed_pct": roads_with_speed / max(len(motor_roads_ml), 1),
            "sector_max_db": max(sector_db) if sector_db else 0,
            "sector_min_db": min(active_sectors) if active_sectors else 0,
            "sector_range_db": (max(sector_db) - min(active_sectors)) if active_sectors else 0,
            "sector_std_db": float(np.std(active_sectors)) if active_sectors else 0,
            "sectors_active": len(active_sectors),
            "tram_count": sum(1 for r in gtfs_routes if r[0] == 0),
            "tram_min_dist": nearest_tram_m if nearest_tram_m is not None else radius_m,
            "train_count": sum(1 for r in gtfs_routes if r[0] != 0),
            "train_max_peak_svc": max((r[3] for r in gtfs_routes if r[0] != 0), default=0),
            "train_min_dist": nearest_train_m if nearest_train_m is not None else radius_m,
        }
        # Per-class road features
        for cls in ["motorway", "trunk", "primary", "secondary", "tertiary", "residential", "service"]:
            cls_roads = [r for r in motor_roads_ml if r[0] == cls]
            ml_features[f"road_{cls}_count"] = len(cls_roads)
            ml_features[f"road_{cls}_min_dist"] = min((r[1] for r in cls_roads), default=radius_m)

        correction = predict_correction(ml_features)
        if correction is not None:
            ml_lden = lden + correction
    except _MLDisabled:
        pass
    except Exception:
        logger.warning("ML correction failed, falling back to raw physics", exc_info=True)

    # Score from the ML-corrected Lden (loud-end-re-anchored; see _lden_to_score).
    score = _lden_to_score(ml_lden)

    label = _score_label(score)

    # Confidence interval — tighter with ML model
    if ml_lden != lden:
        ci_db = 5.0  # ML model CV MAE ~4.85
    elif ml_lden < 50:
        ci_db = 8.0
    elif ml_lden < 60:
        ci_db = 5.0
    else:
        ci_db = 4.0
    if len(aadt_segments) == 0 and len(nfdh_stations) == 0 and ml_lden == lden:
        ci_db += 3.0

    # Low-confidence flags. NSW's per-state affine compresses dynamic range
    # (worst in-sample fit); the quiet-end correction means the model is
    # extrapolating below its calibrated urban support. Either widens the
    # interval and surfaces a note so the estimate is not presented as precise.
    low_confidence = False
    confidence_note = None
    try:
        from property_scores.noise.transfer import state_low_confidence as _slc
    except Exception:
        _slc = lambda s: False
    if lden_source == "transfer" and _slc(state):
        low_confidence = True
        confidence_note = (f"Lower model confidence in {state} (limited local "
                           "calibration data). Verify on site before relying on this estimate.")
        ci_db += 2.0
    if _quiet_w > 0:
        low_confidence = True
        confidence_note = ("Quiet, low-density area below the model's calibrated "
                           "range; this estimate is extrapolated. Verify on site.")
        ci_db += round(_quiet_w * 4.0, 1)

    # The stacked worst case (quiet + no count station + NSW) reached +-14 dB,
    # which a critic fairly read back as "a 28dB error range". The stack
    # predates measured validation: with the quiet-end recalibration active,
    # exactly this profile is held against Class 1 LAeq receivers
    # (residential anchor MAE 3.78 dB, max residual 7.1), so NSW caps at
    # +-10 dB. States without measured validation keep the full stack.
    if _QUIET_RECAL_ENABLED and state == "NSW" and lden_source == "transfer":
        ci_db = min(ci_db, 10.0)

    motor_roads = [r for r in roads if r[0] not in ("footway", "path", "steps", "cycleway", "pedestrian", "track")]

    result = {
        "score": score,
        "estimated_db": round(ml_lden, 1),
        "disclaimer": "Modelled estimate based on road/rail/aircraft data. Not a professional noise assessment. Actual noise varies with traffic, weather, and time of day.",
        "confidence_range_db": round(ci_db, 1),
        "estimated_db_low": round(max(ml_lden - ci_db, AMBIENT_DB), 1),
        "estimated_db_high": round(ml_lden + ci_db, 1),
        "leq_db": round(leq_24h, 1),
        "lden_db": round(ml_lden, 1),
        "physics_lden_db": round(physics_lden, 1),
        "lden_source": lden_source,
        "low_confidence": low_confidence,
        "confidence_note": confidence_note,
        "leq_day_db": round(leq_day_val, 1),
        "leq_night_db": round(leq_night_val, 1),
        "label": label,
        "state": state,
        "road_count": len(motor_roads),
        "aadt_segments": len(aadt_segments),
        "nfdh_stations": len(nfdh_stations),
        "roads_with_speed_limit": roads_with_speed,
        "road_db": round(road_db, 1),
    }

    if transfer_raw is not None:
        result["transfer_raw"] = transfer_raw

    if rail_db > 0:
        result["rail_db"] = round(rail_db, 1)
    if nearest_tram_m is not None:
        result["nearest_tram_m"] = round(nearest_tram_m, 0)
    if nearest_train_m is not None:
        result["nearest_train_m"] = round(nearest_train_m, 0)

    if gtfs_found:
        result["rail_source"] = "gtfs"
    if top_roads:
        result["dominant_road"] = top_roads[0][1]
    if top_rails:
        result["dominant_rail"] = top_rails[0][1]
    # dominant_source = the loudest of road / rail / aircraft, not always the road,
    # rendered as a human-friendly label (never a bare class or numeric station id).
    _dom = []
    if top_roads and road_db > 0:
        # Compare in one acoustic domain: road_db is a CRTN L10 level (~3 dB above
        # its Leq); rail_db / aircraft_db are Leq. Subtract L10_TO_LEQ_DB so a road
        # doesn't out-rank a louder aircraft/rail purely from the L10-vs-Leq gap.
        _dom.append((road_db - L10_TO_LEQ_DB, _road_label(top_roads[0][1])))
    if top_rails and rail_db > 0:
        _dom.append((rail_db, _RAIL_TYPE_LABEL.get(top_rails[0][1].get("type"), "railway")))
    if aircraft_db > 0:
        _dom.append((aircraft_db, "aircraft"))
    result["dominant_source"] = max(_dom, key=lambda x: x[0])[1] if _dom else None
    if building_screening_total > 0:
        result["max_building_screening_db"] = round(building_screening_total, 1)
    if terrain_screening > 0:
        result["terrain_screening_db"] = round(terrain_screening, 1)

    # Facade analysis: per-sector Lden
    facade = _facade_lden(_all_directional_sources, aircraft_db)
    if facade:
        result.update(facade)

    # Aircraft noise overlay
    if aircraft["zone_code"]:
        result["aircraft"] = {
            "zone_code": aircraft["zone_code"],
            "anef_min": aircraft["anef_min"],
            "anef_max": aircraft["anef_max"],
            "penalty_db": aircraft["penalty_db"],
            "impact": aircraft["impact"],
            "airport_type": aircraft["airport_type"],
            "lga": aircraft["lga"],
        }
        result["aircraft_db"] = round(aircraft_db, 1)

    # Ocean proximity (informational, does not affect score)
    try:
        from property_scores.common.overture import water_near
        ocean_classes = {"ocean", "sea", "bay"}
        ocean_hits = [w for w in water_near(db, lat, lng, radius_m=2000)
                      if w[0] in ocean_classes]
        if ocean_hits:
            nearest_ocean = ocean_hits[0][2]
            result["ocean_proximity_m"] = round(nearest_ocean)
            if nearest_ocean < 200:
                result["ocean_noise"] = "Surf noise likely dominant"
            elif nearest_ocean < 500:
                result["ocean_noise"] = "Surf noise audible"
            elif nearest_ocean < 1000:
                result["ocean_noise"] = "Surf noise faint"
    except Exception:
        pass

    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Compute multi-source noise score")
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lng", type=float, required=True)
    parser.add_argument("--radius", type=int, default=500)
    parser.add_argument("--source", type=str, default=None)
    args = parser.parse_args()

    result = noise_score(args.lat, args.lng, args.radius, source=args.source)
    print(f"Noise Score: {result['score']}/100 ({result['label']}) — {result.get('state', '?')}")
    print(f"Lden: {result['lden_db']} dB | Leq24h: {result['leq_db']} dB | Day: {result['leq_day_db']} | Night: {result['leq_night_db']}")
    print(f"Road: {result['road_db']} dB (L10)", end="")
    if result.get("rail_db"):
        print(f" | Rail: {result['rail_db']} dB", end="")
    if result.get("aircraft_db"):
        print(f" | Aircraft: {result['aircraft_db']} dB", end="")
    print(f"\nAADT: {result['aadt_segments']} VicRoads + {result['nfdh_stations']} NFDH | Overture roads: {result['road_count']}")
    if result.get("dominant_road"):
        d = result["dominant_road"]
        src = d.get("road_name", d.get("class", "?"))
        print(f"Dominant: {src} @ {d['distance_m']}m, AADT={d.get('aadt', d.get('aadt_est', '?'))}, {d['db']} dB")
    if result.get("aircraft"):
        a = result["aircraft"]
        print(f"Aircraft: {a['zone_code']} (ANEF {a['anef_min']}"
              + (f"-{a['anef_max']}" if a['anef_max'] else "+")
              + f") +{a['penalty_db']} dB — {a['impact']}")
