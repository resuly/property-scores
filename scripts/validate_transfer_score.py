"""Validate the EU->AU transfer branch wired into noise/score.py.

For each of the 7 SoundPLAN cities, sample ~30 ground-truth building points and
compare physics_lden vs transfer_lden vs truth (lden of sp_rd_max_d/e/n). Reports
per-city + pooled MAE for transfer-vs-truth and physics-vs-truth. Sentinel
truths (lden < 30) are dropped.

Run with NOISE_TRANSFER unset; this harness calls transfer_lden directly so the
physics path stays the default in score().
"""
import csv
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from property_scores.common.overture import get_db
from property_scores.common.au_state import detect_state
from property_scores.noise.score import noise_score
from property_scores.noise.transfer import transfer_lden

CITIES = ["melbourne", "sydney", "adelaide", "perth", "hobart", "canberra", "darwin"]
N_PER_CITY = 30


def truth_lden(d, e, n):
    if max(d, e, n) <= 0:
        return 0.0
    return 10 * math.log10(
        (12 * 10 ** (d / 10) + 4 * 10 ** ((e + 5) / 10) + 8 * 10 ** ((n + 10) / 10)) / 24
    )


def load_points(city, n):
    fn = f"data/ambient_sample/antn_{city}_buildings_.csv"
    if not os.path.exists(fn):
        return []
    rows = list(csv.DictReader(open(fn)))
    if len(rows) > n:
        rows = rows[:: max(len(rows) // n, 1)][:n]
    pts = []
    for r in rows:
        m = re.search(r"POINT \(([-\d.]+) ([-\d.]+)\)", r["geometry"])
        if not m:
            continue
        la, lo = float(m.group(1)), float(m.group(2))  # POINT (lat lng)
        t = truth_lden(float(r["sp_rd_max_d"]), float(r["sp_rd_max_e"]), float(r["sp_rd_max_n"]))
        if t >= 30.0:  # drop sentinels
            pts.append((la, lo, t))
    return pts


def main():
    db = get_db()
    all_phys_err, all_trans_err = [], []
    print(f"{'city':<10} {'lat':>9} {'lng':>10} {'state':>5} "
          f"{'phys':>6} {'trans':>6} {'truth':>6} {'score':>5} {'src':>8}")
    print("-" * 80)
    for city in CITIES:
        pts = load_points(city, N_PER_CITY)
        cp, ct = [], []
        for la, lo, truth in pts:
            state = detect_state(la, lo)
            # transfer prediction (direct, includes affine; no aircraft remix here
            # since these are road-only SoundPLAN samples)
            t_lden, t_raw, raster_ok = transfer_lden(db, la, lo, state)
            # full physics + score (default path)
            res = noise_score(la, lo)
            phys = res["physics_lden_db"]
            score = res["score"]
            src = "trans" if raster_ok else "MISS"
            print(f"{city:<10} {la:9.4f} {lo:10.4f} {str(state):>5} "
                  f"{phys:6.1f} {t_lden:6.1f} {truth:6.1f} {score:5d} {src:>8}")
            cp.append(abs(phys - truth))
            ct.append(abs(t_lden - truth))
        if cp:
            print(f"  >> {city}: phys MAE={sum(cp)/len(cp):.2f}  "
                  f"transfer MAE={sum(ct)/len(ct):.2f}  (n={len(cp)})")
            print("-" * 80)
            all_phys_err += cp
            all_trans_err += ct
    if all_phys_err:
        print(f"\nPOOLED n={len(all_phys_err)}  "
              f"physics MAE={sum(all_phys_err)/len(all_phys_err):.2f}  "
              f"transfer MAE={sum(all_trans_err)/len(all_trans_err):.2f}")


if __name__ == "__main__":
    raise SystemExit(main())
