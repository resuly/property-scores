"""Search the ring radii, then re-validate the winner on held-out cities.

The v5 rings (50/100/200/400/800 m) were chosen once in a POC and never tuned,
yet scaling them uniformly by 1.3 moved the A/B gate by as much as the entire
geodistance correction. So they are worth a search -- but selecting on the gate
and then quoting that same gate is gate-fitting, which is how you talk yourself
into a model that is not actually better.

So this does two separate things:

  SEARCH   pooled in-city 5-fold over all cities, to rank candidate ring sets.
  VALIDATE the winner and the v5 baseline on HELD-OUT cities: fit the per-state
           calibration on 4 cities, score the other 3, rotating. A ring set that
           only wins in search is reported as such.

Feature regeneration is 96 s per candidate, so a handful of candidates is
minutes, not a project.

    .venv/bin/python scripts/search_noise_rings.py --generate   # build caches
    .venv/bin/python scripts/search_noise_rings.py              # score them
"""
import itertools
import os
import subprocess
import sys

import numpy as np
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error
from sklearn.model_selection import KFold

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEEDS = (42, 1, 7)

# Candidates. Kept deliberately small and physically motivated rather than a
# blind grid: noise falls off with distance, so the question is how wide the
# NEAR ring should be and how far the outer ring should reach.
CANDIDATES = {
    "v5 baseline 50/100/200/400/800": [50, 100, 200, 400, 800],
    "uniform x1.3":                   [65, 130, 260, 520, 1040],
    "wider outer":                    [50, 100, 200, 500, 1200],
    "coarser near":                   [80, 160, 300, 600, 1000],
    "log-spaced 60..960":             [60, 120, 240, 480, 960],
    "near-heavy":                     [40, 80, 160, 400, 1000],
}


def cache_for(rings):
    return ("data/eu/transfer6_rings_"
            + "_".join(f"{r:g}" for r in rings) + "_cache.npz")


def generate():
    for name, rings in CANDIDATES.items():
        out = os.path.join(REPO, cache_for(rings))
        if os.path.exists(out):
            print(f"  have {name}")
            continue
        print(f"  generating {name} -> {out}", flush=True)
        env = {**os.environ, "RINGS_M": ",".join(str(r) for r in rings)}
        subprocess.run([sys.executable, "scripts/poc_eu_transfer6_geodist.py"],
                       cwd=REPO, env=env, check=True,
                       stdout=subprocess.DEVNULL)


def _load(rings):
    d = np.load(os.path.join(REPO, cache_for(rings)), allow_pickle=True)
    Xnl, ynl, Xau, yau = d["Xnl"], d["ynl"], d["Xau"], d["yau"]
    cau = np.array(d["cau"])
    k = yau >= 30
    return Xnl, ynl, Xau[k], yau[k], cau[k]


def score(rings, seed, holdout=None):
    """Pooled in-city 5-fold, or leave-cities-out when `holdout` is given."""
    Xnl, ynl, Xau, yau, cau = _load(rings)
    rf = RandomForestRegressor(n_estimators=400, min_samples_leaf=3,
                               max_features="sqrt", n_jobs=-1,
                               random_state=seed).fit(Xnl, ynl)
    raw = rf.predict(Xau).reshape(-1, 1)
    pred = np.zeros(len(yau))
    if holdout is None:
        for c in sorted(set(cau.tolist())):
            idx = np.where(cau == c)[0]
            for tr, te in KFold(5, shuffle=True, random_state=seed).split(idx):
                m = LinearRegression().fit(raw[idx[tr]], yau[idx[tr]])
                pred[idx[te]] = m.predict(raw[idx[te]])
        sel = np.ones(len(yau), bool)
    else:
        # Calibration fitted ONLY on the training cities, applied to unseen
        # ones. Harsher than in-city CV and closer to a new state going live.
        tr = ~np.isin(cau, holdout)
        te = np.isin(cau, holdout)
        m = LinearRegression().fit(raw[tr], yau[tr])
        pred[te] = m.predict(raw[te])
        sel = te
    return (mean_absolute_error(yau[sel], pred[sel]),
            float(np.corrcoef(pred[sel], yau[sel])[0, 1]))


def main():
    if "--generate" in sys.argv:
        generate()
        return 0

    print("SEARCH -- pooled in-city 5-fold (this is what gets gate-fitted)\n")
    print(f"{'ring set':34s} {'MAE':>14s} {'r':>14s}")
    results = {}
    for name, rings in CANDIDATES.items():
        if not os.path.exists(os.path.join(REPO, cache_for(rings))):
            print(f"{name:34s} (no cache, run --generate)")
            continue
        a = np.array([score(rings, s) for s in SEEDS])
        results[name] = (a[:, 0].mean(), a[:, 1].mean())
        print(f"{name:34s} {a[:,0].mean():7.3f}+-{a[:,0].std():.3f} "
              f"{a[:,1].mean():7.3f}+-{a[:,1].std():.3f}")

    if not results:
        return 1
    best = min(results, key=lambda k: results[k][0])
    print(f"\nsearch winner by MAE: {best}")

    print("\nVALIDATE -- leave-cities-out, calibration never sees the test city\n")
    cities = ["melbourne", "sydney", "adelaide", "perth", "hobart", "canberra", "darwin"]
    folds = [cities[i::3] for i in range(3)]
    print(f"{'ring set':34s} {'held-out MAE':>14s} {'held-out r':>12s}")
    for name in ({"v5 baseline 50/100/200/400/800", best}):
        rings = CANDIDATES[name]
        vals = [score(rings, s, holdout=f) for s in SEEDS for f in folds]
        a = np.array(vals)
        print(f"{name:34s} {a[:,0].mean():9.3f}+-{a[:,0].std():.3f} "
              f"{a[:,1].mean():7.3f}+-{a[:,1].std():.3f}")
    print("\nIf the winner does not also lead on held-out cities, it was fitted "
          "to the search and should not be shipped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
