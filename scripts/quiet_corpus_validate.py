"""Dual-anchor validation harness: local noise model vs measured_quiet_corpus.csv.

Runs the LOCAL model (property_scores.noise.score) at every corpus point and
reports residuals (model - measured) per role. The two anchors that gate any
quiet-end recalibration (2026-06-10 handoff):

  residential anchor  quiet_residential rows with quality=exact (the NorthConnex
                      n=9 set that measured the +10.9 over-read)
  loud anchor         loud_roadside rows (Lake Macquarie Pacific Hwy/Charlestown,
                      the set the live model already matches at ~+/-1)

PASS requires: residential mean residual in single digits (< +10, target much
lower) AND loud mean residual within +/-2 of its 2026-06-10 baseline (+0.6).
Exact gates are enforced in tests/test_quiet_corpus.py; this script prints them.

Env: NOISE_TRANSFER defaults to 1 here (the deployed path). Set recalibration
flags (e.g. NOISE_QUIET_RECAL=1) in the environment to measure an "after" run:
  NOISE_QUIET_RECAL=1 .venv/bin/python scripts/quiet_corpus_validate.py

Per-point columns include the transfer chain reconstruction (affine -> quiet
blend) so the over-read splits into "calibration" vs "RF reads the motorway hot"
without touching production code.
"""
import argparse
import csv
import os
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("NOISE_TRANSFER", "1")

CORPUS = os.path.join(ROOT, "data", "eis_noise", "measured_quiet_corpus.csv")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=CORPUS)
    ap.add_argument("--roles", default="quiet_residential,arterial,loud_roadside",
                    help="comma-separated roles to evaluate")
    args = ap.parse_args()

    from property_scores.noise.score import noise_score
    from property_scores.noise import transfer as tr

    rows = list(csv.DictReader(open(args.corpus)))
    roles = set(args.roles.split(","))
    rows = [r for r in rows if r["role"] in roles]

    print(f"NOISE_TRANSFER={os.environ.get('NOISE_TRANSFER')} "
          f"NOISE_QUIET_RECAL={os.environ.get('NOISE_QUIET_RECAL', '0')} "
          f"(corpus: {len(rows)} rows)")
    hdr = (f"{'site':44} {'role':>17} {'qual':>7} {'meas':>5} {'model':>6} "
           f"{'resid':>6} {'raw':>5} {'affine':>6} {'phys':>5} {'road':>5} "
           f"{'rail':>5} {'src':>9}")
    print(hdr)

    tr._load()
    cal_nsw = tr._CALIB["states"].get("NSW") or tr._CALIB["global_affine"]

    res = {}
    for r in rows:
        meas = float(r["meas_lden"])
        out = noise_score(float(r["lat"]), float(r["lng"]))
        model = out["lden_db"]
        resid = model - meas
        raw = out.get("transfer_raw")
        affine = (cal_nsw["slope"] * raw + cal_nsw["intercept"]
                  ) if raw is not None and r["state"] == "NSW" else None
        key = (r["role"], r["quality"])
        res.setdefault(key, []).append(resid)
        print(f"{r['site'][:44]:44} {r['role']:>17} {r['quality']:>7} "
              f"{meas:>5.1f} {model:>6.1f} {resid:>+6.1f} "
              f"{raw if raw is not None else '-':>5} "
              f"{f'{affine:.1f}' if affine is not None else '-':>6} "
              f"{out.get('physics_lden_db', '-'):>5} "
              f"{out.get('road_db', '-'):>5} {out.get('rail_db') or '-':>5} "
              f"{out.get('lden_source', '-'):>9}")

    def stat(name, vals):
        if not vals:
            return
        print(f"  {name:46} n={len(vals):>2} mean {statistics.mean(vals):+6.2f} "
              f"MAE {statistics.mean(abs(v) for v in vals):5.2f} "
              f"max {max(vals, key=abs):+6.1f}")

    print("\n== anchors ==")
    resid_anchor = res.get(("quiet_residential", "exact"), [])
    loud_anchor = res.get(("loud_roadside", "sensor"), [])
    stat("RESIDENTIAL anchor (quiet_residential/exact)", resid_anchor)
    stat("LOUD anchor (loud_roadside)", loud_anchor)
    print("== context (not gated) ==")
    for (role, qual), vals in sorted(res.items()):
        if (role, qual) not in (("quiet_residential", "exact"),
                                ("loud_roadside", "sensor")):
            stat(f"{role}/{qual}", vals)

    if resid_anchor and loud_anchor:
        rm, lm = statistics.mean(resid_anchor), statistics.mean(loud_anchor)
        ok_res = rm < 10.0
        ok_loud = abs(lm - 0.6) <= 2.0
        print(f"\nGATES: residential mean {rm:+.2f} (<+10 {'PASS' if ok_res else 'FAIL'})"
              f" | loud mean {lm:+.2f} (within ±2 of +0.6 baseline "
              f"{'PASS' if ok_loud else 'FAIL'})")


if __name__ == "__main__":
    main()
