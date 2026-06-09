"""Fit + evaluate the AADT post-adjustment on the 199 measured points.

Form (targeted, NOT a blanket shift):
    lr = log(dom_aadt / class_expected_aadt)     # <0 = road quieter than class
    adjusted = model_lden + beta * lr            # beta<0 -> lr<0 lowers, lr>0 raises
clamped to [LO, HI] dB so a tiny class-expected (residential=400) can't explode.

beta is fit by least-squares residual ~ lr on AADT-hit points. We DO NOT remove
the intercept (the baseline +8 over-read = SoundPLAN/facade confound, decision A
says don't chase it with a global shift -> would damage genuine-loud).

Key checks: overall + per-state MAE/bias before/after, AND the genuine-loud
subset (measured>=72) must NOT be pushed down (the whole point of AADT vs slope).
Points with no AADT hit (TAS/ACT/NT + misses) keep the raw model unchanged.
"""
import csv
import math
import statistics

JOINED = "data/eis_noise/residuals_aadt.csv"


def load():
    rows = []
    for r in csv.DictReader(open(JOINED)):
        d = {"state": r["state"], "model": float(r["model"]), "meas": float(r["meas"]),
             "res": float(r["res"]), "exp_aadt": float(r["exp_aadt"])}
        d["dom_aadt"] = float(r["dom_aadt"]) if r["dom_aadt"] else None
        rows.append(d)
    return rows


def fit_beta(hit):
    lr = [math.log(r["dom_aadt"] / max(r["exp_aadt"], 1)) for r in hit]
    res = [r["res"] for r in hit]
    n = len(lr)
    mx, my = statistics.mean(lr), statistics.mean(res)
    cov = sum((a - mx) * (b - my) for a, b in zip(lr, res))
    var = sum((a - mx) ** 2 for a in lr)
    beta = cov / var if var else 0.0
    return beta


def mae(rows, key="adj"):
    return statistics.mean(abs(r[key] - r["meas"]) for r in rows)


def bias(rows, key="adj"):
    return statistics.mean(r[key] - r["meas"] for r in rows)


def main():
    rows = load()
    hit = [r for r in rows if r["dom_aadt"] and r["dom_aadt"] > 0]
    beta = fit_beta(hit)
    print(f"n={len(rows)}  AADT-hit={len(hit)}  fitted beta={beta:+.2f} (per ln-unit of AADT/expected)\n")

    # correction = K*lr with K = -beta (so quiet road lr<0 -> negative -> lowers).
    K = -beta
    # variants: (label, K_used, lo, hi)  clamp [lo,hi] on the correction (dB)
    variants = [
        (f"K={K:.2f} (LS), clamp[-12,4]", K, -12, 4),
        (f"K={K:.2f} pull-down only [-12,0]", K, -12, 0),
        ("K=4 pull-down only [-12,0]", 4.0, -12, 0),
        ("K=6 pull-down only [-12,0]", 6.0, -12, 0),
    ]
    for label, k, lo, hi_c in variants:
        for r in rows:
            if r["dom_aadt"] and r["dom_aadt"] > 0:
                lr = math.log(r["dom_aadt"] / max(r["exp_aadt"], 1))
                corr = max(lo, min(hi_c, k * lr))
                r["adj"] = r["model"] + corr
            else:
                r["adj"] = r["model"]  # no AADT -> unchanged
        print(f"=== {label} ===")
        print(f"  overall   MAE {mae(rows,'model'):.2f} -> {mae(rows,'adj'):.2f}   bias {bias(rows,'model'):+.2f} -> {bias(rows,'adj'):+.2f}")
        # genuine-loud preservation
        loud = [r for r in rows if r["meas"] >= 72]
        print(f"  loud(>=72) n={len(loud)}: bias {bias(loud,'model'):+.2f} -> {bias(loud,'adj'):+.2f}  MAE {mae(loud,'model'):.2f} -> {mae(loud,'adj'):.2f}  (must NOT worsen)")
        # per state
        from collections import defaultdict
        bys = defaultdict(list)
        for r in rows:
            bys[r["state"]].append(r)
        for s in sorted(bys):
            g = bys[s]
            print(f"    {s:11} n={len(g):>3}  bias {bias(g,'model'):+5.2f} -> {bias(g,'adj'):+5.2f}   MAE {mae(g,'model'):.2f} -> {mae(g,'adj'):.2f}")
        # flattening: corr(adjusted residual, model) should shrink toward 0
        adj_res = [r["adj"] - r["meas"] for r in rows]
        mod = [r["model"] for r in rows]
        mr, mm = statistics.mean(adj_res), statistics.mean(mod)
        cov = sum((a - mr) * (b - mm) for a, b in zip(adj_res, mod))
        sd = statistics.pstdev(adj_res) * statistics.pstdev(mod) * len(mod)
        print(f"  flattening corr(adj-residual, model): {cov/sd if sd else 0:+.2f}  (raw was +0.50)\n")


if __name__ == "__main__":
    main()
