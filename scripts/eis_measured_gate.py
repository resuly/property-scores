"""The MEASURED gate: model vs real EIS noise-logger readings, on a fixed set.

Every other harness in this repo scores the model against SoundPLAN, which is
itself a model. This one scores it against instruments. It exists because that
distinction was invisible: the SoundPLAN gate says MAE 3.4 dB while the measured
one says 7.7 dB with a systematic over-read.

Differences from the older eis_noise_compare.py, which this supersedes:

  * NO live geocoding. That script called a public API per address, so a
    rate-limited or flaky run silently dropped points -- two consecutive runs
    scored 199 and 109 addresses, which makes any A/B between models
    meaningless. This reads the on-disk geocode cache and FAILS if coverage
    drops below a threshold, so the point set is fixed and comparable.
  * Takes a model id, so v1 and v2 are scored on identical points.
  * Reports LAeq and LA10 rows separately, because the LA10->LAeq conversion is
    a fixed -3 dB assumption and should not be allowed to hide inside a pooled
    average.

Honest caveats, unchanged from the original and worth repeating in any report:
  * Addresses geocode to a building centroid, losing the logger's facade
    position. A logger at the road-facing wall is louder than the centroid.
  * day/night -> Lden is approximate.
  * QLD "snapshot" rows are attended short readings, not continuous logging.

    .venv/bin/python scripts/eis_measured_gate.py
    NOISE_MODEL_ID=eu-transfer-v2-physics .venv/bin/python scripts/eis_measured_gate.py
"""
import argparse
import csv
import json
import math
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CORPUS = "data/eis_noise/measured_corpus_v2.csv"
CACHE = "data/eis_noise/_geocode_cache.json"
# LA10(18h) -> LAeq. Was 3.0, inherited from the older script with no source.
# Measured instead, by scripts/derive_la10_to_laeq.py, from the two Victorian
# reports on disk that publish both quantities from the same logger at the same
# site:
#
#   West Gate Tunnel, per site, freeway corridor   n=32   mean +0.84  sd 0.81
#   Mordialloc Freeway, per logging day, suburban  n=114  mean -0.28  median -0.40
#   pooled                                         n=146  mean -0.03  median 0.00
#
# The two corridors bracket zero and the pooled median is zero, so the offset is
# set to zero: as these reports publish them, LA10(18h) and LAeq are the same
# number within measurement noise. The 3 dB rule of thumb is for free-flowing
# traffic; on steady high-volume corridors the level distribution compresses and
# the gap closes.
#
# This mattered: 71 of the 79 Victorian rows are LA10, so subtracting 3 dB
# pushed those measurements ~3 dB low and showed up as model over-read that was
# not in the model. Victoria's apparent bias was inflated by roughly that much.
#
# Mordialloc's spread is wide (sd 2.73, range -9.4..+8.0) because the first and
# last day of each logging run are partial; the median is unaffected and is what
# this follows. The residual ~1-2 dB is real measurement scatter, now an
# acknowledged uncertainty in the corpus rather than a hidden systematic.
L10_TO_LEQ = 0.0
MIN_COVERAGE = 0.90


def meas_lden(day, night, metric):
    day_leq = day - L10_TO_LEQ if metric == "LA10" else day
    return 10 * math.log10((15 * 10 ** (day_leq / 10)
                            + 9 * 10 ** ((night + 10) / 10)) / 24)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=CORPUS)
    ap.add_argument("--drop-snapshot", action="store_true",
                    help="exclude QLD attended short readings")
    a = ap.parse_args()

    from property_scores.noise.score import noise_score
    from property_scores.noise import model_registry as mr
    from property_scores.noise import score as ns
    ns._cache_get = lambda *x, **k: None   # never replay a cached score here
    ns._cache_put = lambda *x, **k: None

    cache = json.load(open(CACHE))
    rows = list(csv.DictReader(open(a.corpus)))
    if a.drop_snapshot:
        rows = [r for r in rows if r.get("metric") != "snapshot"]

    by_state = defaultdict(list)
    by_metric = defaultdict(list)
    missing = 0
    for row in rows:
        key = f"{row['state']}|{row['address']}"
        hit = cache.get(key)
        if not hit:
            missing += 1
            continue
        lat, lng = float(hit[0]), float(hit[1])
        r = noise_score(lat, lng)
        mod = r.get("lden_db")
        if mod is None:
            missing += 1
            continue
        ml = meas_lden(float(row["meas_day"]), float(row["meas_night"]),
                       row.get("metric", "LAeq"))
        d = mod - ml
        by_state[row["state"]].append(d)
        by_metric[row.get("metric", "LAeq")].append(d)

    scored = sum(len(v) for v in by_state.values())
    cov = scored / max(len(rows), 1)
    print(f"model: {mr.describe()}")
    print(f"scored {scored}/{len(rows)} ({cov:.0%} of the corpus)\n")
    if cov < MIN_COVERAGE:
        print(f"REFUSING to report: coverage {cov:.0%} below {MIN_COVERAGE:.0%}. "
              f"A shifting point set makes model comparisons meaningless.")
        return 2

    def block(title, groups):
        print(f"===== {title} (model minus measured Lden) =====")
        for k in sorted(groups):
            ds = groups[k]
            print(f"  {k:<12} n={len(ds):>3}  bias={sum(ds)/len(ds):+6.1f}  "
                  f"MAE={sum(abs(x) for x in ds)/len(ds):5.2f}")

    block("per state", by_state)
    print()
    block("per measurement type", by_metric)
    alld = [d for v in by_state.values() for d in v]
    print(f"\n  {'ALL':<12} n={len(alld):>3}  bias={sum(alld)/len(alld):+6.1f}  "
          f"MAE={sum(abs(x) for x in alld)/len(alld):5.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
