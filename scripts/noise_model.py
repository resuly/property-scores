"""Manage noise model versions: list, register, activate, archive, verify.

    scripts/noise_model.py list
    scripts/noise_model.py migrate            # adopt today's flat files as v1
    scripts/noise_model.py register <id> --rf F --calib F [--gate-r R --gate-mae M]
    scripts/noise_model.py activate <id>      # rollback / promote
    scripts/noise_model.py archive <id> --reason "..."
    scripts/noise_model.py verify             # every registered version loadable

A candidate model is registered with status "candidate" and is NOT served until
`activate`. That is the whole point: build and score as many as you like without
touching what customers see, then promote one with a single command, and roll
back with the same command.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from property_scores.noise import model_registry as mr  # noqa: E402


def _root() -> Path:
    p = mr._data_dir() / "models" / "noise"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _reg() -> dict:
    return mr.load_registry() or {"schema": 1, "active": None, "versions": {}}


def _save(reg: dict) -> None:
    mr.registry_path().write_text(json.dumps(reg, indent=2) + "\n")


def _sha(p: Path, cap: int = 64 * 1024 * 1024) -> str:
    """Hash the first 64 MB. Enough to catch a swapped/truncated artefact
    without re-reading 114 MB on every check."""
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read(cap))
    return h.hexdigest()[:16]


def _git_commit() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10,
                              cwd=Path(__file__).parent.parent).stdout.strip() or None
    except Exception:
        return None


def cmd_list(a):
    reg = _reg()
    if not reg["versions"]:
        print("no versions registered. Run: scripts/noise_model.py migrate")
        r = mr.resolve(force=True)
        print(f"currently resolving to: {r['id']} ({r['source']})")
        return 0
    print(f"{'':2s} {'id':28s} {'status':10s} {'created':12s} {'gate r':>7s} {'gate MAE':>9s}")
    for vid, v in sorted(reg["versions"].items()):
        mark = "->" if vid == reg.get("active") else "  "
        g = v.get("gate") or {}
        print(f"{mark} {vid:28s} {v.get('status',''):10s} {str(v.get('created',''))[:10]:12s} "
              f"{str(g.get('r','-')):>7s} {str(g.get('mae','-')):>9s}")
    print(f"\nactive: {reg.get('active')}")
    return 0


def _register(vid, rf, calib, status, gate, notes, training, move=False):
    root = _root()
    vdir = root / vid
    vdir.mkdir(parents=True, exist_ok=True)
    for src, name in ((rf, "rf.pkl"), (calib, "calibration.json")):
        dst = vdir / name
        if dst.exists():
            print(f"  {name} already present, keeping")
            continue
        (shutil.move if move else shutil.copy2)(str(src), str(dst))
        print(f"  {'moved' if move else 'copied'} {src} -> {dst}")
    manifest = {
        "id": vid,
        "status": status,
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "code_commit": _git_commit(),
        "artefacts": {
            "rf.pkl": {"sha256_64mb": _sha(vdir / "rf.pkl"),
                       "bytes": (vdir / "rf.pkl").stat().st_size},
            "calibration.json": {"sha256_64mb": _sha(vdir / "calibration.json"),
                                 "bytes": (vdir / "calibration.json").stat().st_size},
        },
        "gate": gate,
        "training": training,
        "notes": notes,
    }
    (vdir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    reg = _reg()
    reg["versions"][vid] = {"status": status, "created": manifest["created"],
                            "gate": gate, "notes": notes}
    _save(reg)
    return manifest


def cmd_migrate(a):
    """Adopt the current flat production files as the first registered version.

    COPIES rather than moves: the legacy paths keep working, so a half-deployed
    fleet cannot end up with boxes that find no model at all.
    """
    d = mr._data_dir()
    rf, calib = d / mr.LEGACY_RF, d / mr.LEGACY_CALIB
    if not rf.exists() or not calib.exists():
        print(f"legacy artefacts not found in {d}")
        return 2
    vid = a.id
    print(f"registering {vid} from the live flat files:")
    _register(
        vid, rf, calib, status="active",
        gate={"harness": "scripts/calib_eval.py in-city 5-fold, MIN_LDEN=30",
              "r": 0.696, "mae": 3.798,
              "measured": "2026-07-26, mean of 5 seeds"},
        notes=("EU(NL+UK) geometry-only RF + per-state affine. The model live in "
               "production since 2026-06-08, adopted into the registry "
               "unchanged on 2026-07-26. Distances in its features use the "
               "pre-2026-07 formula; retraining on corrected geometry was "
               "measured and did NOT improve the gate (3.798->3.852 MAE), so "
               "the callers pass legacy_distance=True to keep feeding it the "
               "ruler it was fitted on."),
        training={
            "features": "75 geometry/terrain/landcover, scripts/poc_eu_transfer5.py fkeys()",
            "eu_points": 15200, "au_calibration_points": 11015,
            "feature_cache": "data/eu/transfer5_cache.npz",
            "built_by": "scripts/build_noise_model.py + scripts/recalc_au_full_calibration.py "
                        "+ scripts/unified_calib_analysis.py (constrained slope 0.88)",
        })
    reg = _reg()
    reg["active"] = vid
    _save(reg)
    print(f"\nactive = {vid}")
    print("legacy flat files left in place as a fallback; remove them only once "
          "every box resolves via the registry.")
    return 0


def cmd_register(a):
    _register(a.id, Path(a.rf), Path(a.calib), status="candidate",
              gate=({"r": a.gate_r, "mae": a.gate_mae,
                     "harness": "scripts/calib_eval.py in-city 5-fold"}
                    if a.gate_r is not None else None),
              notes=a.notes, training={"built_by": a.built_by} if a.built_by else None)
    print(f"registered {a.id} as CANDIDATE (not served). Promote with: "
          f"scripts/noise_model.py activate {a.id}")
    return 0


def cmd_activate(a):
    reg = _reg()
    if a.id not in reg["versions"]:
        print(f"unknown version {a.id}; run list")
        return 2
    vdir = _root() / a.id
    for n in ("rf.pkl", "calibration.json"):
        if not (vdir / n).exists():
            print(f"refusing: {vdir/n} missing")
            return 2
    prev = reg.get("active")
    reg["active"] = a.id
    for vid, v in reg["versions"].items():
        if v.get("status") in ("active", "candidate"):
            v["status"] = "active" if vid == a.id else (
                "candidate" if v.get("status") == "candidate" else "superseded")
    reg["versions"][a.id]["status"] = "active"
    if prev and prev != a.id:
        reg["versions"][prev]["status"] = "superseded"
    _save(reg)
    print(f"active: {prev} -> {a.id}")
    print("restart property-scores to load it (the RF is a process-level "
          "singleton). Since 2026-08-18 score.NOISE_MODEL_VERSION carries the "
          "resolved model id when NOISE_TRANSFER is on, so after the restart "
          "every precomputed grid baked by the previous model is refused and "
          "those regions fall back to live compute until re-baked "
          "(scripts/precompute_noise.py). That is the intended behaviour, not "
          "a fault: it is what stops the old model's grids outliving it. The "
          "sqlite RESULT cache is a separate story: a registry-only swap does "
          "not change its key, so rows computed by the previous model keep "
          "being served until they age out of the 24h TTL.")
    return 0


def cmd_archive(a):
    reg = _reg()
    if a.id == reg.get("active"):
        print("refusing to archive the ACTIVE version; activate another first")
        return 2
    src = _root() / a.id
    if not src.exists():
        print(f"{src} not found")
        return 2
    dst = _root() / "_archive" / a.id
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    v = reg["versions"].get(a.id, {})
    v["status"] = "archived"
    v["archived_reason"] = a.reason
    v["archived_path"] = str(dst.relative_to(mr._data_dir()))
    reg["versions"][a.id] = v
    _save(reg)
    print(f"archived {a.id} -> {dst}\nreason: {a.reason}")
    return 0


def cmd_verify(a):
    """Every registered version must actually load, and hashes must still match.
    Catches a truncated copy or an artefact edited in place."""
    import pickle
    reg = _reg()
    bad = 0
    for vid in sorted(reg["versions"]):
        v = reg["versions"][vid]
        if v.get("status") == "archived":
            print(f"  {vid:28s} archived, skipped")
            continue
        vdir = _root() / vid
        mp = vdir / "manifest.json"
        if not mp.exists():
            print(f"  {vid:28s} FAIL no manifest"); bad += 1; continue
        man = json.loads(mp.read_text())
        okmsg = []
        for name, meta in (man.get("artefacts") or {}).items():
            p = vdir / name
            if not p.exists():
                okmsg.append(f"{name} MISSING"); bad += 1; continue
            if _sha(p) != meta.get("sha256_64mb"):
                okmsg.append(f"{name} HASH CHANGED"); bad += 1
        try:
            with open(vdir / "rf.pkl", "rb") as f:
                m = pickle.load(f)
            n = getattr(m, "n_estimators", "?")
            okmsg.append(f"rf loads ({n} trees)")
        except Exception as e:
            okmsg.append(f"rf UNPICKLABLE {type(e).__name__}"); bad += 1
        print(f"  {vid:28s} {v.get('status',''):10s} " + "; ".join(okmsg))
    r = mr.resolve(force=True)
    print(f"\nresolves to: {r['id']} (via {r['source']})")
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    m = sub.add_parser("migrate"); m.add_argument("--id", default="eu-transfer-v1")
    m.set_defaults(fn=cmd_migrate)
    r = sub.add_parser("register")
    r.add_argument("id"); r.add_argument("--rf", required=True)
    r.add_argument("--calib", required=True)
    r.add_argument("--gate-r", type=float); r.add_argument("--gate-mae", type=float)
    r.add_argument("--notes", default=""); r.add_argument("--built-by")
    r.set_defaults(fn=cmd_register)
    a = sub.add_parser("activate"); a.add_argument("id"); a.set_defaults(fn=cmd_activate)
    ar = sub.add_parser("archive"); ar.add_argument("id")
    ar.add_argument("--reason", required=True); ar.set_defaults(fn=cmd_archive)
    sub.add_parser("verify").set_defaults(fn=cmd_verify)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
