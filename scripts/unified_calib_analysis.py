"""统一重校准分析: 诊断 8 州 + 测试统一策略 (不做 per-state 补丁)。

复用 recalc_au_full_calibration.py 的 feature cache (au_full_feat_cache.npz) +
同一 RF + 同一 5-fold CV (calib_eval / recalc 的 cv_state 逻辑), 保证所有 cv_r
数与现有 _cv block 可比。

输出:
  (1) 诊断表: n, cv_r, slope, intercept, pred/true std-ratio + flag
  (2) 每个统一策略的 per-state cv_r+std-ratio + pooled cv_r/MAE
  (3) 推荐策略的 states block (8 州 slope/intercept)
"""
import json
import os
import pickle

import numpy as np
from sklearn.linear_model import LinearRegression, TheilSenRegressor, HuberRegressor
from sklearn.model_selection import KFold

CITY_STATE = {"melbourne": "VIC", "sydney": "NSW", "adelaide": "SA",
              "perth": "WA", "hobart": "TAS", "canberra": "ACT", "darwin": "NT"}
CACHE_PATH = "data/au_full_feat_cache.npz"
RF_PATH = "data/noise_transfer_rf.pkl"
STATES = ["VIC", "NSW", "SA", "WA", "TAS", "ACT", "NT"]  # QLD has no sample
SEED = 42


def load_raw():
    d = np.load(CACHE_PATH, allow_pickle=True)
    X, y, city = d["X"], d["y"], np.array(d["city"])
    with open(RF_PATH, "rb") as f:
        rf = pickle.load(f)
    raw = rf.predict(X)
    state = np.array([CITY_STATE[c] for c in city])
    return raw, y, state


# ---- affine fitters: return (slope, intercept) ----
def fit_ols(r, yy):
    lin = LinearRegression().fit(r.reshape(-1, 1), yy)
    return float(lin.coef_[0]), float(lin.intercept_)


def fit_theilsen(r, yy):
    m = TheilSenRegressor(random_state=SEED).fit(r.reshape(-1, 1), yy)
    return float(m.coef_[0]), float(m.intercept_)


def fit_huber(r, yy):
    m = HuberRegressor(epsilon=1.35, max_iter=500).fit(r.reshape(-1, 1), yy)
    return float(m.coef_[0]), float(m.intercept_)


def apply_affine(r, slope, intercept):
    return slope * r + intercept


# ---- CV harness: identical 5-fold to recalc.cv_state, but parametrized by a
#      per-state fold-fitter that returns (slope, intercept) from train data ----
def cv_predict_perstate(raw, y, state, fold_fitter, k=5):
    """For each state, 5-fold CV; fold_fitter(train_raw, train_y, st, global_*) ->
    (slope, intercept). Returns out-of-fold pred for the whole array."""
    pred = np.full_like(y, np.nan, dtype=float)
    # global affine pre-fit per fold is needed for shrinkage/constrained; we
    # compute a GLOBAL fold model inside each fold using ALL states' train rows.
    kf = KFold(n_splits=k, shuffle=True, random_state=SEED)
    idxarr = np.arange(len(y))
    for tr, te in kf.split(idxarr):
        # global affine on this fold's train (all states)
        g_slope, g_int = fit_ols(raw[tr], y[tr])
        for st in np.unique(state):
            tr_st = tr[state[tr] == st]
            te_st = te[state[te] == st]
            if len(te_st) == 0:
                continue
            if len(tr_st) < 2:
                pred[te_st] = apply_affine(raw[te_st], g_slope, g_int)
                continue
            sl, it = fold_fitter(raw[tr_st], y[tr_st], st, g_slope, g_int, len(tr_st))
            pred[te_st] = apply_affine(raw[te_st], sl, it)
    return pred


def per_state_metrics(pred, y, state):
    rows = {}
    for st in STATES:
        idx = state == st
        if idx.sum() < 2:
            continue
        p, t = pred[idx], y[idx]
        r = float(np.corrcoef(p, t)[0, 1]) if p.std() > 0 else 0.0
        mae = float(np.mean(np.abs(p - t)))
        std_ratio = float(p.std() / t.std()) if t.std() > 0 else 0.0
        rows[st] = {"n": int(idx.sum()), "cv_r": round(r, 3),
                    "cv_mae": round(mae, 2), "std_ratio": round(std_ratio, 3)}
    return rows


def pooled_metrics(pred, y):
    r = float(np.corrcoef(pred, y)[0, 1])
    mae = float(np.mean(np.abs(pred - y)))
    return {"cv_r": round(r, 3), "cv_mae": round(mae, 2)}


# ===================== fold_fitters for each strategy =====================
def ff_perstate_ols(r, yy, st, gs, gi, n):
    return fit_ols(r, yy)


def ff_global(r, yy, st, gs, gi, n):
    return gs, gi  # ignore per-state train, use fold global


def make_ff_shrink(k):
    def ff(r, yy, st, gs, gi, n):
        s, i = fit_ols(r, yy)
        w = n / (n + k)
        return w * s + (1 - w) * gs, w * i + (1 - w) * gi
    return ff


def ff_constrained_slope(r, yy, st, gs, gi, n):
    # fix slope = global, fit intercept = mean(y - gs*r)
    it = float(np.mean(yy - gs * r))
    return gs, it


def ff_theilsen(r, yy, st, gs, gi, n):
    if n < 5:
        return fit_ols(r, yy)
    return fit_theilsen(r, yy)


def ff_huber(r, yy, st, gs, gi, n):
    if n < 5:
        return fit_ols(r, yy)
    try:
        return fit_huber(r, yy)
    except Exception:
        return fit_ols(r, yy)


def make_ff_robust_shrink(k, robust="huber"):
    rfit = fit_huber if robust == "huber" else fit_theilsen
    def ff(r, yy, st, gs, gi, n):
        try:
            s, i = rfit(r, yy)
        except Exception:
            s, i = fit_ols(r, yy)
        w = n / (n + k)
        return w * s + (1 - w) * gs, w * i + (1 - w) * gi
    return ff


def make_ff_constrained_shrink_int(k):
    """Constrained slope=global; intercept shrunk toward global intercept by w."""
    def ff(r, yy, st, gs, gi, n):
        it_state = float(np.mean(yy - gs * r))
        w = n / (n + k)
        return gs, w * it_state + (1 - w) * gi
    return ff


def cv_state_recalc(raw, y, k=5, seed=42):
    """EXACT copy of recalc.cv_state: per-state independent KFold, OLS each fold.
    Used to prove our harness reproduces the existing _cv block numbers."""
    raw = raw.reshape(-1, 1)
    pred = np.zeros_like(y, dtype=float)
    kf = KFold(n_splits=min(k, len(y)), shuffle=True, random_state=seed)
    for tr, te in kf.split(raw):
        lin = LinearRegression().fit(raw[tr], y[tr])
        pred[te] = lin.predict(raw[te])
    mae = float(np.mean(np.abs(pred - y)))
    r = float(np.corrcoef(pred, y)[0, 1]) if len(y) > 1 else float("nan")
    return pred, mae, r


def main():
    raw, y, state = load_raw()
    print(f"raw: mean={raw.mean():.1f} std={raw.std():.1f} "
          f"min={raw.min():.1f} max={raw.max():.1f}  n={len(y)}")

    # ---- SANITY: reproduce existing _cv block (recalc.cv_state) ----
    print("\n[SANITY] reproduce existing _cv block (per-state independent KFold):")
    print(f"{'st':4s} {'n':>5s} {'cv_r':>6s} {'cv_mae':>7s}  (compare to json _cv)")
    for st in STATES:
        idx = state == st
        _, mae, r = cv_state_recalc(raw[idx], y[idx])
        print(f"{st:4s} {int(idx.sum()):>5d} {r:>6.3f} {mae:>7.2f}")

    # =============== (1) DIAGNOSE: current per-state OLS ===============
    print("\n" + "=" * 78)
    print("(1) DIAGNOSE — current per-state OLS affine")
    print("=" * 78)
    g_slope, g_int = fit_ols(raw, y)
    print(f"GLOBAL affine: slope={g_slope:.3f} intercept={g_int:+.2f} n={len(y)}")
    diag = []
    intercepts = {}
    # in-sample slope/intercept per state
    for st in STATES:
        idx = state == st
        s, i = fit_ols(raw[idx], y[idx])
        intercepts[st] = i
    med_int = np.median(list(intercepts.values()))
    print(f"\n{'st':4s} {'n':>5s} {'cv_r':>6s} {'slope':>7s} {'intcpt':>8s} "
          f"{'std_rat':>8s}  flags")
    # CV with current per-state OLS for cv_r + std_ratio
    pred_cur = cv_predict_perstate(raw, y, state, ff_perstate_ols)
    cur_rows = per_state_metrics(pred_cur, y, state)
    for st in STATES:
        idx = state == st
        s, i = fit_ols(raw[idx], y[idx])
        m = cur_rows[st]
        flags = []
        if i > med_int + 8:  # intercept far above peers (median +8 dB)
            flags.append("INTERCEPT-FLOOR")
        if m["std_ratio"] < 0.6:
            flags.append("COLLAPSED-STD")
        if m["cv_r"] < 0.5:
            flags.append("LOW-CV-R")
        diag.append({"state": st, "n": m["n"], "cv_r": m["cv_r"],
                     "slope": round(s, 3), "intercept": round(i, 2),
                     "std_ratio": m["std_ratio"], "flag": "+".join(flags) or "ok"})
        print(f"{st:4s} {m['n']:>5d} {m['cv_r']:>6.3f} {s:>7.3f} {i:>+8.2f} "
              f"{m['std_ratio']:>8.3f}  {'+'.join(flags) or 'ok'}")
    print(f"\nmedian intercept across states = {med_int:.2f}  "
          f"(NSW intercept {intercepts['NSW']:.1f} is "
          f"{intercepts['NSW']/med_int:.1f}x median)")
    pooled_cur = pooled_metrics(pred_cur, y)
    pooled_cur_ns = pooled_metrics(pred_cur[state != "NSW"], y[state != "NSW"])
    print(f"POOLED (current per-state OLS): cv_r={pooled_cur['cv_r']} "
          f"MAE={pooled_cur['cv_mae']}  | no-NSW cv_r={pooled_cur_ns['cv_r']}")

    # =============== (2) TEST unified strategies ===============
    strategies = [
        ("(a) GLOBAL affine all states", ff_global),
        ("(b) SHRINK k=100", make_ff_shrink(100)),
        ("(b) SHRINK k=300", make_ff_shrink(300)),
        ("(b) SHRINK k=1000", make_ff_shrink(1000)),
        ("(c) CONSTRAINED slope=global, fit intercept", ff_constrained_slope),
        ("(d) ROBUST TheilSen per-state", ff_theilsen),
        ("(d) ROBUST Huber per-state", ff_huber),
        ("(e) ROBUST Huber + SHRINK k=300", make_ff_robust_shrink(300, "huber")),
        ("(e) ROBUST Huber + SHRINK k=1000", make_ff_robust_shrink(1000, "huber")),
        ("(e) ROBUST TheilSen + SHRINK k=300", make_ff_robust_shrink(300, "theilsen")),
        ("(f) CONSTRAINED slope + intercept-shrink k=100", make_ff_constrained_shrink_int(100)),
        ("(f) CONSTRAINED slope + intercept-shrink k=300", make_ff_constrained_shrink_int(300)),
        ("(f) CONSTRAINED slope + intercept-shrink k=1000", make_ff_constrained_shrink_int(1000)),
    ]
    print("\n" + "=" * 78)
    print("(2) UNIFIED STRATEGIES — per-state cv_r / std_ratio + pooled")
    print("=" * 78)
    results = {}
    for name, ff in strategies:
        pred = cv_predict_perstate(raw, y, state, ff)
        rows = per_state_metrics(pred, y, state)
        pooled = pooled_metrics(pred, y)
        pooled_ns = pooled_metrics(pred[state != "NSW"], y[state != "NSW"])
        results[name] = {"rows": rows, "pooled": pooled, "pooled_ns": pooled_ns}
        print(f"\n--- {name} ---")
        print(f"{'st':4s} {'n':>5s} {'cv_r':>6s} {'std_rat':>8s}")
        for st in STATES:
            m = rows[st]
            print(f"{st:4s} {m['n']:>5d} {m['cv_r']:>6.3f} {m['std_ratio']:>8.3f}")
        print(f"POOLED cv_r={pooled['cv_r']} MAE={pooled['cv_mae']}  "
              f"| no-NSW cv_r={pooled_ns['cv_r']} MAE={pooled_ns['cv_mae']}")

    # =============== summary comparison table ===============
    print("\n" + "=" * 78)
    print("SUMMARY — pooled cv_r/MAE + NSW std_ratio + min state cv_r")
    print("=" * 78)
    print(f"{'strategy':48s} {'pool_r':>7s} {'pool_MAE':>8s} "
          f"{'NSW_sr':>7s} {'NSW_r':>6s} {'min_r':>6s}")
    base = {"rows": cur_rows, "pooled": pooled_cur}
    def line(label, res):
        rows = res["rows"]; pooled = res["pooled"]
        nsw_sr = rows["NSW"]["std_ratio"]; nsw_r = rows["NSW"]["cv_r"]
        min_r = min(rows[s]["cv_r"] for s in STATES)
        print(f"{label:48s} {pooled['cv_r']:>7.3f} {pooled['cv_mae']:>8.2f} "
              f"{nsw_sr:>7.3f} {nsw_r:>6.3f} {min_r:>6.3f}")
    line("CURRENT per-state OLS (baseline)", base)
    for name, res in results.items():
        line(name, res)

    # save full results
    out = {"global_affine": {"slope": g_slope, "intercept": g_int},
           "diagnose": diag, "baseline": {"rows": cur_rows, "pooled": pooled_cur},
           "strategies": {n: r for n, r in results.items()}}
    with open("data/unified_calib_results.json", "w") as f:
        json.dump(out, f, indent=2)
    print("\nsaved data/unified_calib_results.json")


if __name__ == "__main__":
    main()
