"""Generate final unified 'states' block for the RECOMMENDED strategy:
CONSTRAINED slope = global slope (fit on all ~11k), per-state intercept only.

This is the in-sample fit that gets written to JSON (CV already validated in
unified_calib_analysis.py). Intercept_state = mean(y_state - g_slope * raw_state).
QLD = global affine (no sample). Prints before/after table + the JSON block.
"""
import json
import pickle

import numpy as np
from sklearn.linear_model import LinearRegression

CITY_STATE = {"melbourne": "VIC", "sydney": "NSW", "adelaide": "SA",
              "perth": "WA", "hobart": "TAS", "canberra": "ACT", "darwin": "NT"}
STATES = ["VIC", "NSW", "SA", "WA", "TAS", "ACT", "NT"]


def main():
    d = np.load("data/au_full_feat_cache.npz", allow_pickle=True)
    X, y, city = d["X"], d["y"], np.array(d["city"])
    with open("data/noise_transfer_rf.pkl", "rb") as f:
        rf = pickle.load(f)
    raw = rf.predict(X)
    state = np.array([CITY_STATE[c] for c in city])

    gl = LinearRegression().fit(raw.reshape(-1, 1), y)
    g_slope, g_int = float(gl.coef_[0]), float(gl.intercept_)

    with open("data/noise_state_calibration.json") as f:
        old = json.load(f)

    print(f"GLOBAL slope (pinned for all states) = {g_slope:.6f}")
    print(f"GLOBAL intercept (QLD fallback)       = {g_int:.6f}\n")
    print(f"{'st':4s} {'n':>5s} | {'old_slope':>9s} {'old_int':>8s} -> "
          f"{'new_slope':>9s} {'new_int':>8s}  | {'in_MAE':>6s} {'in_r':>5s} {'std_rat':>7s}")

    states_block = {}
    for st in STATES:
        idx = state == st
        n = int(idx.sum())
        r_st, y_st = raw[idx], y[idx]
        intercept = float(np.mean(y_st - g_slope * r_st))
        pred = g_slope * r_st + intercept
        mae = float(np.mean(np.abs(pred - y_st)))
        rr = float(np.corrcoef(pred, y_st)[0, 1])
        sr = float(pred.std() / y_st.std())
        o = old["states"][st]
        states_block[st] = {
            "slope": round(g_slope, 10),
            "intercept": round(intercept, 10),
            "n": n,
            "insample_mae": round(mae, 2),
            "insample_r": round(rr, 2),
            "city_sample": [c for c, s in CITY_STATE.items() if s == st][0],
            "calib_kind": "constrained-slope (global slope, per-state intercept)",
        }
        print(f"{st:4s} {n:>5d} | {o['slope']:>9.3f} {o['intercept']:>+8.2f} -> "
              f"{g_slope:>9.3f} {intercept:>+8.2f}  | {mae:>6.2f} {rr:>5.2f} {sr:>7.3f}")

    states_block["QLD"] = {
        "slope": round(g_slope, 10),
        "intercept": round(g_int, 10),
        "n": int(len(y)),
        "fallback": "global (no QLD SoundPLAN sample)",
    }
    print(f"\nQLD  {len(y):>5d} | global fallback slope={g_slope:.3f} int={g_int:+.2f}")

    print("\n" + "=" * 60)
    print("NEW 'states' BLOCK (paste into noise_state_calibration.json):")
    print("=" * 60)
    print(json.dumps(states_block, indent=2))

    with open("data/unified_states_block.json", "w") as f:
        json.dump(states_block, f, indent=2)
    print("\nsaved data/unified_states_block.json")


if __name__ == "__main__":
    main()
