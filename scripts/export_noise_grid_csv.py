#!/usr/bin/env python3
"""Export a noise-score grid around a point as CSV, for one-off external requests.

This is the SEVENTH delivery surface for score data (the others are live API,
90-day parcel cache, static sample, OpenAPI example, report pipeline, webhook).
Unlike those, it does not pass through `_strip_unlicensed_fields` -- so the
field whitelist lives here, in `PUBLIC_FIELDS`, and must be kept in step with
the delivery rules in `reference_daleads_data_rights`.

Usage:
  python scripts/export_noise_grid_csv.py \
      --lat -37.7711004 --lng 144.95633056 \
      --radius-m 1000 --spacing-m 100 \
      --out ../limon-ops/exports/rmit-brunswick-noise \
      --place "25-29 Dawson Street, Brunswick VIC 3056"

Writes three files into --out:
  noise_grid.csv    one row per grid point inside the radius
  README.md         column definitions, model limits, usage conditions
  ATTRIBUTION.txt   upstream data sources that must be carried downstream
"""
from __future__ import annotations

import argparse
import collections
import csv
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from property_scores.noise.score import (  # noqa: E402
    noise_score, NOISE_MODEL_VERSION, _RAIL_TYPE_LABEL, AMBIENT_DB,
    _DAY_ADJ, _EVE_ADJ,
)
from property_scores.noise import measured_validation as mv  # noqa: E402

# `_lden_to_score` changes gradient at this Lden (score.py); it is a literal
# there with no constant to import, so `verify_model_constants` asserts it.
_SCORE_BREAK_DB = 70.0

# `dominant_source` returns a real street name when the dominant road has one
# and a category label otherwise (`_road_label`), so the column mixes two kinds
# of value and cannot be grouped on. Derive a column that is always a category.
# Exact equality against the closed rail label set is safe: a road named
# "Tram Road" comes back as "Tram Road", never as the bare token "tram".
_RAIL_LABELS = set(_RAIL_TYPE_LABEL.values())


def dominant_source_type(dominant_source: str | None) -> str | None:
    if not dominant_source:
        return None
    if dominant_source in _RAIL_LABELS:
        return "rail"
    if dominant_source == "aircraft":
        return "aircraft"
    return "road"

# Fields we publish, in column order. Anything not listed here is dropped.
#
# Deliberately EXCLUDED, do not add without re-reading the data-rights rules:
#   screening_db, db_raw   model-internal quantities; road and rail define them
#                          differently so a recipient's arithmetic cannot close
#   transfer_raw,
#   physics_lden_db,
#   lden_source            intermediate values of the model, not facts. NOTE:
#                          on the physics fallback `lden_db` IS `physics_lden_db`
#                          to the digit, so on that path the exclusion protects
#                          nothing; do not describe it as a safeguard. `preflight`
#                          exists to stop that path reaching a recipient at all.
#   road_db / rail_db /    per-source levels in mixed acoustic domains (road is
#   aircraft_db            a CRTN L10, the others are Leq) -- misreadable alone.
#                          Was partially recoverable from leq_day_db on road-only
#                          rows; that column is no longer published, so it is not.
#   leq_day_db,            the period levels come off the physics chain while
#   leq_night_db           `lden_db` is replaced by the ML transfer model, so the
#                          two do not reconcile: on the Brunswick extract 198 of
#                          317 rows had an Lden more than 3 dB from what their own
#                          day/night levels imply, worst +17.3 dB. Shipping both
#                          hands the recipient columns that contradict each other.
#                          `_assert_periods_reconcile` blocks re-adding them blind.
#   leq_db (24 h)          the model energy-sums rail at its full day level over
#                          all 24 h while the period columns drop rail in the
#                          evening and overnight, so on rail-affected rows it is
#                          up to ~2.3 dB above what its own day/evening/night
#                          figures reconstruct. Withheld rather than explained.
#   dominant_road,         the full source rows, which carry screening_db among
#   dominant_rail          other internals. `dominant_source` does publish the
#                          dominant street's NAME where there is one. Which
#                          dataset those names come from is measured per run
#                          (`facts["name_sources"]`) and written into
#                          ATTRIBUTION.txt from that, never assumed: Overture's
#                          road records carry no name column at all, so in
#                          practice they come from the state AADT parquet.
#   road_count, aadt_segments, nfdh_stations, roads_with_speed_limit
#                          model input counts, not measurements of the place
PUBLIC_FIELDS = [
    ("lat", "lat"),
    ("lng", "lng"),
    ("distance_from_centre_m", "distance_from_centre_m"),
    ("quiet_score", "score"),
    ("quiet_label", "label"),
    ("lden_db", "lden_db"),
    ("lden_db_low", "estimated_db_low"),
    ("lden_db_high", "estimated_db_high"),
    ("confidence_range_db", "confidence_range_db"),
    ("dominant_source", "dominant_source"),
    ("dominant_source_type", None),  # derived below, not a model output
    ("state", "state"),
]

M_PER_DEG_LAT = 111_320.0


def preflight(allow_physics_only: bool) -> dict:
    """Refuse to export numbers the live product would not produce.

    `NOISE_TRANSFER` defaults to "0" and `transfer._load()` swallows every
    exception, so a machine simply missing rasterio silently produces the
    physics fallback instead of the ML path production runs. That failure is
    invisible in the output: the rows look completely normal. It was caught
    only by re-scoring sampled points under the production environment, where
    12 of 12 disagreed, by up to 29 score points and 10 dB.

    An export is a snapshot of the product. If it cannot reproduce the
    product's configuration, it must say so rather than ship a shadow model.
    """
    from property_scores.noise import score as _s
    enabled = _s._TRANSFER_ENABLED
    loaded = False
    detail = ""
    try:
        from property_scores.noise import transfer
        loaded = bool(transfer._load())
        if not loaded:
            detail = "transfer._load() returned False (model artefacts or rasterio missing)"
    except Exception as e:  # noqa: BLE001 - reported, not swallowed
        detail = f"{type(e).__name__}: {e}"

    state = {"transfer_enabled": enabled, "transfer_loaded": loaded, "detail": detail}
    if enabled and loaded:
        return state
    if allow_physics_only:
        print(f"WARNING: exporting the physics fallback, not the live model path ({state})")
        return state
    raise RuntimeError(
        "This environment cannot reproduce the live model path, so the export would "
        "contain numbers the product does not serve.\n"
        f"  NOISE_TRANSFER enabled: {enabled}\n"
        f"  transfer model loaded:  {loaded}\n"
        f"  {detail}\n"
        "Run on the server that serves the product, with its environment:\n"
        "  DATA_DIR=... NOISE_TRANSFER=1 NOISE_QUIET_RECAL=1 NOISE_RAIL_RECAL=1\n"
        "or pass --allow-physics-only if a physics-path export is genuinely wanted.")

# The physics path clamps Lden with a literal `min(lden, 82.0)` (score.py
# over-call guard) and exposes no constant to import. The ML path recomputes
# uncapped, so a ceiling may or may not be present depending on which ran. The
# README therefore states the ceiling only when rows actually sit on it, which
# is measured from the output; this constant only names the value to look for,
# and `verify_model_constants` checks it still matches the model.
LDEN_CAP_DB = 82.0

# Vintage of the upstream inputs. Not derivable from our parquets (the VIC
# AADT build drops the source's YR column), so it is recorded per state and
# verified against the publisher. VIC: queried the DTP Traffic_Volume
# FeatureServer over this bbox on 2026-08-03, 71 of 73 segments carry
# YR=2020, 2 carry none. Transit: PTV GTFS parquets built 2026-04-22/23.
# An unregistered state refuses rather than shipping a vague claim.
_VINTAGE = {
    "VIC": ("Two notes on the vintage of the inputs. The traffic counts are 2020 "
            "surveys, and they do **not** feed the levels in this file: they supply the "
            "street names in `dominant_source` and widen the confidence interval where "
            "no count station is nearby. Public transport service frequency, which does "
            "affect the rail and tram levels, was extracted from PTV timetable data in "
            "April 2026."),
}



def _assert_uniform_model_path(lden_sources) -> None:
    """Every row must come from the same model path, and it must be the live one.

    `preflight` checks the *process*, which is not enough: noise_score caches on
    "lat:lng:radius:source" with no environment or model version in the key and a
    24 h TTL, so a row scored earlier under a different configuration is served
    back regardless of how this process is configured. That is not theoretical --
    an env-less CLI probe of the centre point (run as a "does this reproduce?"
    check) poisoned exactly that row, and it shipped: physics score 23 / Lden 67.0
    where the live path gives 21 / 67.8.

    Per-point fallback is silent too (score.py catches a raster miss and drops
    that one point to physics with only a log line), so this is also the only
    thing standing between a partial fallback and the recipient.
    """
    paths = dict(lden_sources)
    if set(paths) != {"transfer"}:
        raise RuntimeError(
            f"rows did not all come from the live model path: {paths}. "
            "Either a raster miss dropped points to physics, or a stale cache entry "
            "written under another configuration was served (the cache key carries "
            "no environment or model version). Purge the affected keys from "
            "data/noise_result_cache.sqlite and re-run.")


def _require_single_state(states: set) -> None:
    """Accuracy figures are per state, so one state per file.

    `None` counts as a distinct value: a grid partly outside any boundary would
    otherwise pass this check and get one state's validation numbers stamped
    over rows whose own `state` column is blank.
    """
    if len(states) != 1 or None in states:
        raise RuntimeError(
            f"grid resolves to states {sorted(s or 'unknown' for s in states)}; the README's "
            "accuracy figures are per-state and would be wrong for some rows. Split the "
            "export by state, or extend write_docs to report per state.")


def _assert_periods_reconcile(rows: list[dict]) -> None:
    """If period levels are ever published again, they must agree with Lden.

    `lden_db` is produced by the ML transfer model while `leq_day_db` /
    `leq_night_db` come off the physics chain, so they can disagree wildly (198
    of 317 rows by more than 3 dB, worst +17.3 dB, on the first Brunswick run).
    Publishing both gives the recipient arithmetic that cannot close. This fires
    only if someone adds those columns back without fixing the underlying split.
    """
    published = {c for c, _ in PUBLIC_FIELDS}
    if not {"leq_day_db", "leq_night_db"} & published:
        return
    worst = 0.0
    for r in rows:
        d, n, L = float(r["leq_day_db"]), float(r["leq_night_db"]), float(r["lden_db"])
        # Reconstruct evening from day using the model's own traffic profile
        # rather than assuming leq_eve == leq_day: _DAY_ADJ is +2.04 dB and
        # _EVE_ADJ is -1.43 dB, so that assumption biased this by ~0.9 dB.
        e = d - _DAY_ADJ + _EVE_ADJ
        implied = 10 * math.log10(
            (12 * 10 ** (d / 10) + 4 * 10 ** ((e + 5) / 10) + 8 * 10 ** ((n + 10) / 10)) / 24)
        worst = max(worst, abs(L - implied))
    if worst > 3.0:
        raise RuntimeError(
            f"published period levels disagree with lden_db by up to {worst:.1f} dB. "
            "They come from different computation paths; do not ship them together.")


def verify_model_constants() -> None:
    """Fail if the model no longer behaves the way the README describes it.

    Every number this script writes into prose is either measured from the run
    or checked here. Two reviews of this file found six false statements and
    every one was prose about the model that nothing checked.
    """
    from property_scores.noise.score import _lden_to_score as f

    # Score endpoints, quoted verbatim in the column table.
    assert f(40.0) == 100, f"score at Lden 40 is {f(40.0)}, README says 100"
    assert f(87.4) == 0, f"score at Lden 87.4 is {f(87.4)}, README says 0"
    assert f(39.0) == 100 and f(88.0) == 0, "score no longer saturates past the endpoints"

    # Direction: higher score must mean quieter.
    assert f(50.0) > f(70.0), "score is no longer higher-is-quieter"

    # The piecewise break and the two gradients quoted in the curve note.
    steep = (f(50.0) - f(60.0)) / 10.0
    flat = (f(75.0) - f(85.0)) / 10.0
    assert 2.7 <= steep <= 3.1, f"gradient below the break is {steep:.2f}/dB, README says ~2.9"
    assert 0.6 <= flat <= 1.0, f"gradient above the break is {flat:.2f}/dB, README says ~0.8"
    assert f(_SCORE_BREAK_DB) == 14, (
        f"the curve no longer breaks at {_SCORE_BREAK_DB} dB "
        f"(score there is {f(_SCORE_BREAK_DB)}, both branches used to meet at 14)")

    # Ambient floor, quoted in the floor note.
    assert AMBIENT_DB == 35.0, f"AMBIENT_DB is {AMBIENT_DB}, README says 35.0"


def _metre_offsets_to_degrees(lat: float, north_m: float, east_m: float) -> tuple[float, float]:
    """Convert a metre offset to a degree offset at this latitude.

    Longitude degrees shrink by cos(lat), so a single 111320 m/deg constant on
    both axes stretches the grid east-west by ~21% at Melbourne's latitude.
    """
    dlat = north_m / M_PER_DEG_LAT
    dlng = east_m / (M_PER_DEG_LAT * max(math.cos(math.radians(lat)), 1e-6))
    return dlat, dlng


def build_grid(lat: float, lng: float, radius_m: float, spacing_m: float) -> list[tuple[float, float, float]]:
    """Grid points inside the radius, as (lat, lng, distance_from_centre_m)."""
    steps = int(math.floor(radius_m / spacing_m))
    points: list[tuple[float, float, float]] = []
    for i in range(-steps, steps + 1):
        for j in range(-steps, steps + 1):
            north_m = i * spacing_m
            east_m = j * spacing_m
            dist = math.hypot(north_m, east_m)
            if dist > radius_m:
                continue
            dlat, dlng = _metre_offsets_to_degrees(lat, north_m, east_m)
            points.append((lat + dlat, lng + dlng, dist))
    # North-to-south, west-to-east: readable when opened in a spreadsheet.
    points.sort(key=lambda p: (-p[0], p[1]))
    return points


def export(lat: float, lng: float, radius_m: float, spacing_m: float,
           out_dir: Path, place: str) -> dict:
    points = build_grid(lat, lng, radius_m, spacing_m)
    total = len(points)
    print(f"Centre: {place or f'{lat}, {lng}'}")
    print(f"Grid: {total} points inside {radius_m:.0f} m at {spacing_m:.0f} m spacing")

    csv_path = out_dir / "noise_grid.csv"

    started = time.time()
    rows = []
    states = set()
    # Facts the README asserts. Measured here rather than written by hand: the
    # first two reviews of this script found six false statements, every one of
    # them prose about the data rather than a defect in the data.
    facts = {
        "aircraft_points": 0,
        "terrain_points": 0,
        "name_sources": set(),
        "names": set(),
        "lden_sources": collections.Counter(),
        "named_rows": 0,
    }
    for n, (plat, plng, dist) in enumerate(points, 1):
        r = noise_score(plat, plng)
        if not r:
            raise RuntimeError(f"noise_score returned empty for {plat},{plng}")
        row = {}
        for col, key in PUBLIC_FIELDS:
            if col == "lat":
                row[col] = round(plat, 7)
            elif col == "lng":
                row[col] = round(plng, 7)
            elif col == "distance_from_centre_m":
                row[col] = round(dist)
            elif col == "dominant_source_type":
                row[col] = dominant_source_type(r.get("dominant_source"))
            else:
                row[col] = r.get(key)
        if row["quiet_score"] is None or row["lden_db"] is None:
            raise RuntimeError(f"missing score/lden at {plat},{plng}: {sorted(r)[:12]}")
        # `states` must record the absence of a state too, otherwise a grid half
        # outside any boundary passes the multi-state check and gets one state's
        # accuracy figures stamped over rows whose `state` column is blank.
        states.add(r.get("state") or None)
        if r.get("aircraft_db"):
            facts["aircraft_points"] += 1
        if r.get("terrain_screening_db"):
            facts["terrain_points"] += 1
        facts["lden_sources"][r.get("lden_source")] += 1
        dom_road = r.get("dominant_road") or {}
        if dom_road.get("road_name") and row["dominant_source"] == dom_road.get("road_name"):
            facts["named_rows"] += 1
            facts["names"].add(dom_road["road_name"])
            facts["name_sources"].add(dom_road.get("source"))
        rows.append(row)
        if n % 25 == 0 or n == total:
            rate = n / max(time.time() - started, 1e-6)
            print(f"  {n}/{total}  ({rate:.1f} pts/s)")

    # Validate before writing: a refused run must not leave a CSV on disk with
    # no README to explain it.
    _require_single_state(states)
    _assert_uniform_model_path(facts["lden_sources"])
    _assert_periods_reconcile(rows)

    out_dir.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=[c for c, _ in PUBLIC_FIELDS])
        w.writeheader()
        w.writerows(rows)

    print(f"Wrote {csv_path} ({len(rows)} rows, {time.time() - started:.0f}s)")
    return {"rows": rows, "csv_path": csv_path, "states": states,
            "count": len(rows), "facts": facts}


def write_docs(out_dir: Path, lat: float, lng: float, radius_m: float,
               spacing_m: float, place: str, result: dict, generated_on: str) -> None:
    rows = result["rows"]
    scores = [r["quiet_score"] for r in rows]
    ldens = [r["lden_db"] for r in rows]
    _require_single_state(result["states"])
    state = next(iter(result["states"]))
    m = mv.for_state(state) if state else None

    facts = result["facts"]

    # Ceiling: assert from the data, never from a remembered constant. On the
    # physics path the model clamps at LDEN_CAP_DB; the ML path recomputes
    # uncapped, so whether a ceiling is even present depends on which path ran.
    n_cap = sum(1 for v in ldens if v >= LDEN_CAP_DB)
    if n_cap:
        cap_note = (
            f"\n**Lden is capped at {LDEN_CAP_DB:.1f} dB** in this configuration of the "
            f"model, because the road energy sum can produce implausible values in dense "
            f"multi-arterial cells. {n_cap} of the {len(rows)} points in this file "
            f"{'sits' if n_cap == 1 else 'sit'} at that ceiling and should be read as "
            f"\"at least this loud\" rather than as an exact level.\n")
    else:
        cap_note = ""

    # Floor: AMBIENT_DB applies per column, not to the file as a whole. Report
    # which published columns actually sit on it rather than claiming none do.
    floored = {col: sum(1 for r in rows if r[col] is not None and float(r[col]) <= AMBIENT_DB)
               for col in ("lden_db", "lden_db_low")}
    hit = {c: n for c, n in floored.items() if n}
    if hit:
        floor_note = (
            f"\nLevels have an ambient floor of {AMBIENT_DB:.1f} dB, and some values in this "
            f"file sit on it: "
            + ", ".join(f"{n} row{'s' if n != 1 else ''} of `{c}`" for c, n in hit.items())
            + ". Those are floored, not measured, so the true level there is at or below "
              f"{AMBIENT_DB:.1f} dB.")
    else:
        floor_note = (f"\nLevels have an ambient floor of {AMBIENT_DB:.1f} dB. No published "
                      f"value in this file sits on it.")

    # Aircraft and terrain: state what actually happened in this extract.
    n_air = facts["aircraft_points"]
    # Terrain is deliberately not reported: like building screening it enters the
    # physics chain and cancels out of the road term, so a count of points where
    # it "applies" tells the reader nothing about the numbers they have.
    if not n_air:
        contrib_note = ("No point in this extract falls inside an aircraft noise contour, "
                        "so aircraft makes no contribution anywhere in this file.")
    else:
        contrib_note = (f"Aircraft noise contributes at {n_air} of the {len(rows)} points.")

    if state not in _VINTAGE:
        raise RuntimeError(
            f"no input vintage recorded for {state}. Verify the upstream survey years "
            "with the publisher and add them to _VINTAGE before shipping a file that "
            "someone may cite.")
    vintage_note = _VINTAGE[state]

    if m and m.get("instrument_points"):
        direction = "higher" if m["bias_db"] > 0 else "lower"
        accuracy = (
            f"Against {m['instrument_points']} noise-logger readings in {state}, this model "
            f"reads **{m['bias_db']:+.1f} dB** on average, with a mean absolute error of "
            f"**{m['mae_db']:.1f} dB** (validation corpus as at {m['as_at']}: {m['corpus']}). "
            f"The sign means the model reads {direction} than the instruments on average "
            f"in {state}.")
    else:
        accuracy = ("No instrument validation figures resolved for this area. Treat every value "
                    "as an unvalidated model output.")

    # What the data can and cannot support. Deliberately does NOT quote a
    # "share of neighbouring pairs within the error bar" statistic: the interval
    # published here is an ABSOLUTE accuracy figure floored at the state's MAE
    # against instruments, and it carries a common bias that cancels when two
    # points from the same model are subtracted. It is not an estimate of the
    # error on a difference, and we do not have one, so any such percentage
    # would be a number dressed up as a finding.
    import statistics as _st
    by_xy = {(round(r["lat"], 6), round(r["lng"], 6)): r["lden_db"] for r in rows}
    _lats = sorted({round(r["lat"], 6) for r in rows})
    _lngs = sorted({round(r["lng"], 6) for r in rows})
    diffs = []
    for i, la in enumerate(_lats):
        for j, ln in enumerate(_lngs):
            a_v = by_xy.get((la, ln))
            if a_v is None:
                continue
            for nb in ((_lats[i + 1], ln) if i + 1 < len(_lats) else None,
                       (la, _lngs[j + 1]) if j + 1 < len(_lngs) else None):
                b_v = by_xy.get(nb) if nb else None
                if b_v is not None:
                    diffs.append(abs(a_v - b_v))

    # Corridor-scale contrast, measured: the loudest named arterial against the
    # residential-street rows. This is the comparison the data does support.
    by_src = {}
    for r in rows:
        by_src.setdefault(r["dominant_source"], []).append(r["lden_db"])
    named = {k: v for k, v in by_src.items() if len(v) >= 5}
    loudest = max(named, key=lambda k: _st.median(named[k])) if named else None
    quietest = min(named, key=lambda k: _st.median(named[k])) if named else None

    if not diffs or loudest is None or loudest == quietest:
        resolution_note = ""
    else:
        contrast = _st.median(named[loudest]) - _st.median(named[quietest])
        resolution_note = (
            f"**Before you interpret a pattern.** The contrasts this data supports are "
            f"corridor-scale ones. Rows where {loudest} dominates run "
            f"{_st.median(named[loudest]):.1f} dB at the median against "
            f"{_st.median(named[quietest]):.1f} dB where {quietest} dominates, a gap of "
            f"{contrast:.1f} dB. Neighbouring points {spacing_m:.0f} m apart, by contrast, "
            f"differ by a median of only {_st.median(diffs):.1f} dB.\n\n"
            f"Treat cell-to-cell differences of a few decibels as texture rather than as "
            f"findings. The interval in `lden_db_low` / `lden_db_high` is an absolute "
            f"accuracy figure against instruments, not an error bar on the difference "
            f"between two of these points, so it cannot be used to decide whether a local "
            f"gradient is real. We do not publish an error estimate for differences. Draw "
            f"conclusions at the scale of corridors and blocks, and say which scale you "
            f"are working at."
        )

    # Score curve: the mapping is piecewise, and a reader who linearly inverts
    # the two endpoints is wrong by up to ~9 dB on the compressed branch.
    from property_scores.noise.score import _lden_to_score as _f
    _steep = (_f(50.0) - _f(60.0)) / 10.0
    _flat = (_f(75.0) - _f(85.0)) / 10.0
    n_steep = sum(1 for v in ldens if v > _SCORE_BREAK_DB)
    curve_note = (
        f"The mapping from Lden to score is **not linear**. Below {_SCORE_BREAK_DB:.0f} dB "
        f"it moves about {_steep:.1f} points per dB; above {_SCORE_BREAK_DB:.0f} dB it is "
        f"compressed to about {_flat:.1f} points per dB so that the loud tail still ranks. "
        f"{n_steep} of the {len(rows)} points in this file "
        f"{'is' if n_steep == 1 else 'are'} on the compressed branch. Do not convert scores "
        f"back to decibels by interpolating between the endpoints; use `lden_db`.")

    readme = f"""# Noise grid: {place}

Generated {generated_on} by DA Leads (https://daleads.com.au) for Ethan McCarthy,
RMIT, for use in a university coursework project.

- Centre: {lat}, {lng} ({place})
- Radius: {radius_m:.0f} m
- Grid spacing: {spacing_m:.0f} m
- Points: {result['count']}
- Model version: {NOISE_MODEL_VERSION}
  (an internal build identifier that concatenates the names of the model
  components switched on. Some of them are state-specific and their names do
  not always match the state they apply to, so read it as a build number and
  ask us if you need to know exactly what produced a given figure.)
- Quiet score range in this file: {min(scores)} to {max(scores)}
- Lden range in this file: {min(ldens):.1f} to {max(ldens):.1f} dB

## Read this first

**These are modelled estimates, not measurements.** No microphone was placed at
any of these points.

Road levels come from a machine-learning model calibrated against measured
noise levels. Its inputs are the road network (hierarchy, density and distance
to roads of each class), built form, land cover and terrain. **It does not use
measured traffic volumes**, so two streets of the same class carry the same
road signal even where their actual traffic differs. A physics chain derived
from the UK CRTN road-traffic-noise formula sits behind it as a fallback and
supplies the rail, tram and aircraft terms; it is not an implementation of CRTN
or of any other published standard, so do not cite it as one.

Rail and tram levels come from alignments and timetabled service frequency.
The day, evening and night levels are combined into Lden with the standard
+5 dB evening and +10 dB night penalties.

One thing not to read into these numbers: they do not show buildings sheltering
the cells behind them from road noise. Screening is present in the model but
does not move the published road level.

{contrib_note}

{vintage_note}

{accuracy}
{cap_note}{floor_note}

{curve_note}

{resolution_note}

Two consequences for how you use this:

1. Quote values as modelled estimates and state the error range alongside any
   figure you publish. The `lden_db_low` / `lden_db_high` columns give you a
   per-point interval floored at the measured error for this state.
2. This is not a professional acoustic assessment and must not be used as one.
   It is not suitable as evidence in a planning application, a noise complaint,
   or any compliance process.

Each row is a **point estimate at that exact coordinate**, not an average over
the surrounding 100 m cell. A point that happens to land on a road reads higher
than its neighbours 100 m away. That is real model behaviour rather than an
error, but if you are drawing a smoothed surface you may want to interpolate
across neighbouring points rather than colour each one as a discrete cell.

## Columns

| Column | Meaning |
|---|---|
| `lat`, `lng` | WGS84 coordinates of the grid point |
| `distance_from_centre_m` | Straight-line distance from {place} |
| `quiet_score` | 0-100 quiet score, derived from `lden_db`. **Higher is quieter.** 100 is anything at or below Lden 40 dB, 0 is anything at or above Lden 87.4 dB. It is a re-expression of the same number, not extra information, and the mapping is piecewise, see the note above. If you want a continuous variable to map, use `lden_db` |
| `quiet_label` | Band label for the score: Very Quiet / Quiet / Moderate / Loud / Very Loud |
| `lden_db` | Day-evening-night level (Lden) in dB. The headline noise figure, with the standard +5 dB evening and +10 dB night penalties applied. Any ceiling that applies is described above |
| `lden_db_low`, `lden_db_high` | Confidence interval around `lden_db`, floored at the measured error for this state |
| `confidence_range_db` | Half-width of that interval, in dB |
| `dominant_source` | Which source contributes most at this point. This is the street name where the dominant road has one (for example SYDNEY RD), and a category otherwise (for example local road, railway, tram). Blank if the model resolves no source at all |
| `dominant_source_type` | The same thing reduced to road / rail / aircraft. Group on this column rather than on `dominant_source`, which mixes names and categories |
| `state` | State resolved for the point, which determines the validation figures above |

Some model quantities are deliberately not included: the per-source levels
before and after screening, the screening terms themselves, and a 24-hour Leq.
The first three are steps inside the model rather than facts about the place,
and road and rail define them differently, so they do not add up the way a
reader would reasonably expect. The 24-hour Leq is withheld because the model
sums rail at its full daytime level across all 24 hours while the day, evening
and night figures reduce rail after hours, so on rail-affected points it does
not reconcile with its own period levels. If your project needs a per-source
breakdown, ask and we will discuss what can be provided in a form that is not
misleading.

## Conditions of use

This file is provided free of charge for your RMIT coursework project, on
these conditions:

1. **Attribution.** Any map, figure, table or report that uses this data must
   carry: `Noise data: DA Leads (daleads.com.au), modelled estimates`. Carry
   the upstream attributions in ATTRIBUTION.txt as well.
2. **Academic, non-commercial use only**, limited to this project.
3. **No redistribution.** Do not publish this file or any derivative dataset,
   or upload it to a public repository. Submitting your coursework is fine:
   you may include the data, and maps made from it, in work you hand to your
   lecturer, markers or examiners, with the attribution above. What we are
   asking you not to do is pass the file on as a dataset for others to reuse.
   If a classmate needs it, point them at us and we will send them their own
   copy.
4. **Describe it accurately.** Modelled estimates, with the error range stated.

Questions about any column, or a different area or spacing, to
bo@daleads.com.au.
"""
    (out_dir / "README.md").write_text(readme, encoding="utf-8")

    # Which dataset the published street names came from is measured, not
    # assumed: crediting the wrong licensor was one of the defects found in
    # review, and the honest answer differs by area.
    # Each AADT dataset has its own licensor. Crediting the wrong one was a
    # defect found in review, so the credit block is selected by what was
    # actually used, and an unrecognised source refuses rather than guesses.
    _AADT_LICENSOR = {
        "vicroads": ("VicRoads traffic volume data, Department of Transport and\n"
                     "      Planning Victoria. Licensed under Creative Commons\n"
                     "      Attribution (CC BY 4.0)."),
        "mrwa": ("Traffic volume data, Main Roads Western Australia.\n"
                 "      Licensed under Creative Commons Attribution (CC BY 4.0)."),
        "nfdh": ("Harmonised traffic counts, National Freight Data Hub,\n"
                 "      Australian Government. Licensed under Creative Commons\n"
                 "      Attribution (CC BY 4.0)."),
    }
    _AADT_CREDIT = set(_AADT_LICENSOR)

    name_src = {s for s in facts["name_sources"] if s}
    used_src = name_src or {"vicroads"}
    unknown = used_src - set(_AADT_LICENSOR)
    if unknown:
        raise RuntimeError(
            f"street names came from {sorted(unknown)}, which has no attribution block. "
            "Add its licensor to _AADT_LICENSOR before shipping this file.")
    aadt_credit = "\n  ".join(_AADT_LICENSOR[s] for s in sorted(used_src))
    if not facts["named_rows"]:
        name_credit = "the traffic volumes behind the modelled levels"
    elif name_src <= _AADT_CREDIT:
        n_names = len(facts["names"])
        name_credit = (f"all {n_names} street name{'s' if n_names != 1 else ''} appearing in "
                       f"dominant_source ({facts['named_rows']} rows)")
    else:
        name_credit = (f"the traffic volumes behind the modelled levels (street names in "
                       f"dominant_source come from {sorted(name_src)}, check their terms "
                       f"before publishing)")

    # Credit every input the model reads for these levels, not only the ones
    # measured to have moved a number. Deciding "did this contribute?" is what
    # caused terrain to be dropped from the credits while it was in fact used.
    # Over-crediting an unused input is harmless; under-crediting a used one is not.
    attribution = f"""Attribution for noise grid: {place}
Generated {generated_on}

Noise estimates
  DA Leads (https://daleads.com.au), modelled estimates, model version
  {NOISE_MODEL_VERSION}. Required credit line for any published figure:
      Noise data: DA Leads (daleads.com.au), modelled estimates

Upstream data sources used to produce this extract
  Traffic volumes, and {name_credit}
      {aadt_credit}
  Public transport alignments and service frequency
      Public Transport Victoria GTFS timetable and geographic data,
      via data.vic.gov.au. Licensed under Creative Commons Attribution
      (CC BY 4.0).
  Road network geometry and classes, and building footprints used for
  screening
      Overture Maps Foundation. These themes aggregate several upstream
      datasets, including OpenStreetMap contributors, Microsoft ML Buildings
      and TomTom. Distributed under the Open Database License (ODbL) v1.0.
      https://www.openstreetmap.org/copyright
      https://opendatacommons.org/licenses/odbl/1-0/
      These feed the modelled levels and the generic road class labels in
      dominant_source. They are not the source of the street names.
  Terrain
      Elevation from the Copernicus DEM (GLO-30), produced using Copernicus WorldDEM-30
      (c) DLR e.V. 2010-2014 and (c) Airbus Defence and Space GmbH 2014-2018
      provided under COPERNICUS by the European Union and ESA, and from
      LiDAR-derived national elevation models published by Geoscience Australia
      (CC BY 4.0) inside their coverage.

If you publish work using this file, reproduce this attribution list alongside
the DA Leads credit line.
"""
    (out_dir / "ATTRIBUTION.txt").write_text(attribution, encoding="utf-8")
    print(f"Wrote {out_dir / 'README.md'} and {out_dir / 'ATTRIBUTION.txt'}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--lat", type=float, required=True)
    p.add_argument("--lng", type=float, required=True)
    p.add_argument("--radius-m", type=float, default=1000.0)
    p.add_argument("--spacing-m", type=float, default=100.0)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--place", default="")
    p.add_argument("--generated-on", required=True,
                   help="Date to stamp into README/ATTRIBUTION, e.g. 3 August 2026")
    p.add_argument("--dry-run", action="store_true",
                   help="Report the grid size and exit without scoring")
    p.add_argument("--allow-physics-only", action="store_true",
                   help="Export even though this environment cannot run the live "
                        "model path. The numbers will not match the product.")
    args = p.parse_args()

    if args.spacing_m <= 0 or args.radius_m <= 0:
        p.error("--radius-m and --spacing-m must be positive")

    if args.dry_run:
        pts = build_grid(args.lat, args.lng, args.radius_m, args.spacing_m)
        print(f"{len(pts)} points inside {args.radius_m:.0f} m at {args.spacing_m:.0f} m spacing")
        return

    verify_model_constants()
    cfg = preflight(args.allow_physics_only)
    print(f"Model path: transfer_enabled={cfg['transfer_enabled']} "
          f"transfer_loaded={cfg['transfer_loaded']}")

    result = export(args.lat, args.lng, args.radius_m, args.spacing_m,
                    args.out, args.place)
    print(f"lden_source distribution: {dict(result['facts']['lden_sources'])}")
    write_docs(args.out, args.lat, args.lng, args.radius_m, args.spacing_m,
               args.place, result, args.generated_on)


if __name__ == "__main__":
    main()
