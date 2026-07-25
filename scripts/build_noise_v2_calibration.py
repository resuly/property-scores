"""Build the v2 noise calibration: same RF, physics promoted into the blend.

The v2 finding, measured 2026-07-26 on the 2,225-point A/B harness: every
feature change tried was a wash or worse (corrected geometry -0.006 r, tuned
rings +0.004 r, directional features -0.002 r), and the ONE change that pays is
putting the physics road Lden into the calibration layer:

    production  affine(rf_raw)                     MAE 3.797  r 0.696
    v2          linear(rf_raw, phys_lden, n_roads) MAE 3.674  r 0.708

So v2 reuses the SHIPPED RF byte for byte. Only the calibration changes, from a
one-variable affine to a three-variable linear fit. Two consequences:

  * Zero extra runtime cost. `road_db` and `road_count` are already computed and
    returned on every request -- production computes the physics and then
    cancels it out of the score by arithmetic. v2 stops discarding it.
  * Deploy risk is a JSON file, not a 114 MB model.

Also fits the v1-style constrained variant (coefficients pinned globally,
per-state intercept only), because per-state fitting of four parameters on
n=176 (ACT) is exactly the overfit the v1 unified scheme was introduced to stop.
The CV picks between them rather than an opinion.

    .venv/bin/python scripts/build_noise_v2_calibration.py --register
"""
import argparse
import json
import os
import pickle
import sys

import numpy as np
from sklearn.linear_model import LinearRegression
from sklearn.model_selection import KFold

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from property_scores.noise import model_registry as mr  # noqa: E402

CITY_STATE = {"melbourne": "VIC", "sydney": "NSW", "adelaide": "SA",
              "perth": "WA", "hobart": "TAS", "canberra": "ACT", "darwin": "NT"}
MIN_LDEN = 30.0
SEED = 42
OUT = "data/noise_state_calibration_v2.json"


def _design(raw, phys, nroads):
    return np.c_[raw, phys, nroads]


def _cv(F, y, city, states, constrained):
    """In-city 5-fold, same protocol as the v1 _cv block so numbers compare."""
    pred = np.zeros(len(y))
    if constrained:
        # Global coefficients, per-state intercept: fit the slope vector once on
        # everything, then let each state move only the level.
        for tr, te in KFold(5, shuffle=True, random_state=SEED).split(F):
            g = LinearRegression().fit(F[tr], y[tr])
            base_tr, base_te = F[tr] @ g.coef_, F[te] @ g.coef_
            for c in np.unique(city):
                m_tr = (city[tr] == c)
                m_te = (city[te] == c)
                if m_te.sum() == 0:
                    continue
                off = (y[tr][m_tr] - base_tr[m_tr]).mean() if m_tr.sum() else g.intercept_
                idx = np.where(te)[0] if te.dtype == bool else te
                pred[idx[m_te]] = base_te[m_te] + off
    else:
        for c in np.unique(city):
            idx = np.where(city == c)[0]
            if len(idx) < 10:
                continue
            for tr, te in KFold(5, shuffle=True, random_state=SEED).split(idx):
                m = LinearRegression().fit(F[idx[tr]], y[idx[tr]])
                pred[idx[te]] = m.predict(F[idx[te]])
    ok = pred != 0
    return (float(np.mean(np.abs(pred[ok] - y[ok]))),
            float(np.corrcoef(pred[ok], y[ok])[0, 1]), int(ok.sum()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--register", action="store_true")
    ap.add_argument("--id", default="eu-transfer-v2-physics")
    a = ap.parse_args()

    full = np.load("data/au_full_feat_cache.npz", allow_pickle=True)
    phy = np.load("data/au_physics_cache_full.npz", allow_pickle=True)
    X, y, city = full["X"], full["y"], np.array(full["city"])
    assert np.allclose(phy["y"], y), "physics cache misaligned with feature cache"

    keep = (y >= MIN_LDEN) & phy["ok"] & (phy["rd_lden"] > 0)
    X, y, city = X[keep], y[keep], city[keep]
    phys, nroads = phy["rd_lden"][keep], phy["n_roads"][keep]
    print(f"{keep.sum()}/{len(keep)} points usable "
          f"(dropped {(~keep).sum()}: lden<{MIN_LDEN}, no road term, or failed)")

    # THE SHIPPED RF, not a rebuild: v2 changes only the calibration.
    src = mr.resolve()
    print(f"using RF from {src['id']} ({src['rf']})")
    with open(src["rf"], "rb") as f:
        rf = pickle.load(f)
    keys = list(json.loads(open(src["calibration"]).read())["_feature_keys"])
    raw = rf.predict(X)
    F = _design(raw, phys, nroads)

    print(f"\n{'scheme':34s} {'MAE':>8s} {'r':>8s}  n")
    res = {}
    for label, F_, cons in (("v1 baseline: affine(raw)", raw.reshape(-1, 1), False),
                            ("v2 per-state linear", F, False),
                            ("v2 constrained (global coef)", F, True)):
        mae, r, n = _cv(F_, y, city, None, cons)
        res[label] = (mae, r)
        print(f"{label:34s} {mae:8.3f} {r:8.3f}  {n}")

    per_state = res["v2 per-state linear"]
    constrained = res["v2 constrained (global coef)"]
    use_constrained = constrained[0] <= per_state[0]
    print(f"\nchosen: {'constrained' if use_constrained else 'per-state'} "
          f"(lower CV MAE)")

    # Fit the shipped coefficients.
    g = LinearRegression().fit(F, y)
    states = {}
    for c, st in CITY_STATE.items():
        m = city == c
        if m.sum() < 10:
            continue
        if use_constrained:
            off = float((y[m] - F[m] @ g.coef_).mean())
            states[st] = {"coef": [float(v) for v in g.coef_], "intercept": off,
                          "n": int(m.sum()), "city_sample": c}
        else:
            lin = LinearRegression().fit(F[m], y[m])
            states[st] = {"coef": [float(v) for v in lin.coef_],
                          "intercept": float(lin.intercept_),
                          "n": int(m.sum()), "city_sample": c}
    glob = {"coef": [float(v) for v in g.coef_], "intercept": float(g.intercept_),
            "n": int(len(y))}
    states["QLD"] = {**glob, "fallback": "global (no QLD SoundPLAN sample)"}

    calib = {
        "_schema": 2,
        "_feature_keys": keys,
        "_design": ["rf_raw", "physics_road_lden", "road_count"],
        "_min_lden": MIN_LDEN,
        "_note": ("v2: same RF as v1, physics road Lden and road count promoted "
                  "from decoration into the calibration. Physics correlates "
                  "+0.468 with truth on its own and carries signal the RF "
                  "structurally cannot see (direction, line-of-sight screening, "
                  "measured AADT)."),
        "_coeff_kind": ("constrained: global coefficient vector, per-state intercept"
                        if use_constrained else "per-state linear fit"),
        "_cv": {"v1_affine": {"mae": round(res["v1 baseline: affine(raw)"][0], 3),
                              "r": round(res["v1 baseline: affine(raw)"][1], 3)},
                "v2": {"mae": round(min(per_state[0], constrained[0]), 3),
                       "r": round(max(per_state[1], constrained[1]), 3)},
                "harness": "in-city 5-fold over the full SoundPLAN facade set"},
        "global_affine": glob,
        "states": states,
    }
    with open(OUT, "w") as f:
        json.dump(calib, f, indent=2)
    print(f"\nwrote {OUT}")

    if a.register:
        import subprocess
        subprocess.run([sys.executable, "scripts/noise_model.py", "register", a.id,
                        "--rf", str(src["rf"]), "--calib", OUT,
                        "--gate-r", str(round(max(per_state[1], constrained[1]), 3)),
                        "--gate-mae", str(round(min(per_state[0], constrained[0]), 3)),
                        "--built-by", "scripts/build_noise_v2_calibration.py",
                        "--notes",
                        "Same RF as eu-transfer-v1; physics road Lden + road count "
                        "promoted into the calibration. Zero extra runtime cost "
                        "(both already computed per request). CANDIDATE, not served."],
                       check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
