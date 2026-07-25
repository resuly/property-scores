"""Which noise model is live, what produced it, and how to roll back.

Before this existed, the answer to "which model is production running?" was a
114 MB `data/noise_transfer_rf.pkl` with no version, no provenance, and no
history (data/ is gitignored). Four model .pkl files sat side by side and only
one was reachable; three calibration backups had names like `.pre_opt_bak`.
Rolling back meant knowing which file to copy over which.

Layout under DATA_DIR:

    models/noise/
        registry.json              which version is active
        <version-id>/
            rf.pkl
            calibration.json
            manifest.json          provenance + gate scores
        _archive/<version-id>/     superseded, kept for reference

Resolution order:
  1. NOISE_MODEL_ID env var          -- one-restart rollback / A-B on a box
  2. registry.json "active"
  3. legacy flat data/*.pkl paths    -- so a box without the registry still runs

Rollback is therefore: `NOISE_MODEL_ID=eu-transfer-v1 systemctl restart ...`,
or edit one field in registry.json. No file shuffling.

NOTE this is deliberately separate from score.NOISE_MODEL_VERSION, which is a
CACHE KEY (it invalidates precomputed grids). A model swap should usually change
both, but they answer different questions: this says WHICH ARTEFACT, that says
IS THE CACHE STILL VALID.
"""
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_REPO_DATA = Path(__file__).parent.parent.parent / "data"

# The pre-registry filenames. Kept working on purpose: a server that has not
# been migrated yet must still load its model rather than fall back to physics.
LEGACY_RF = "noise_transfer_rf.pkl"
LEGACY_CALIB = "noise_state_calibration.json"

_resolved: dict | None = None


def _data_dir() -> Path:
    # transfer.py historically resolved data/ relative to the repo, ignoring
    # DATA_DIR. Honour DATA_DIR when set (that is what the systemd unit uses)
    # and fall back to the repo copy, so both layouts work.
    env = os.environ.get("DATA_DIR")
    if env:
        p = Path(env)
        if p.exists():
            return p
    return _REPO_DATA


def registry_path() -> Path:
    return _data_dir() / "models" / "noise" / "registry.json"


def load_registry() -> dict | None:
    p = registry_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        logger.exception("noise model registry unreadable at %s", p)
        return None


def resolve(force: bool = False) -> dict:
    """Return {id, source, rf, calibration, manifest} for the model to load.

    `source` is one of "env", "registry", "legacy" so logs and /health can say
    where the running model came from -- the question that was previously
    unanswerable.
    """
    global _resolved
    if _resolved is not None and not force:
        return _resolved

    root = _data_dir() / "models" / "noise"
    reg = load_registry()
    want = os.environ.get("NOISE_MODEL_ID") or (reg or {}).get("active")
    source = ("env" if os.environ.get("NOISE_MODEL_ID")
              else "registry" if want else "legacy")

    if want:
        vdir = root / want
        rf, calib = vdir / "rf.pkl", vdir / "calibration.json"
        if rf.exists() and calib.exists():
            _resolved = {"id": want, "source": source, "rf": rf,
                         "calibration": calib,
                         "manifest": vdir / "manifest.json"}
            logger.info("noise model %s (%s)", want, source)
            return _resolved
        # Do NOT silently fall through on an explicit request: a typo'd
        # NOISE_MODEL_ID quietly serving the old model is exactly the kind of
        # thing that makes a rollback look like it worked when it did not.
        if os.environ.get("NOISE_MODEL_ID"):
            raise FileNotFoundError(
                f"NOISE_MODEL_ID={want} requested but {vdir} is missing rf.pkl "
                f"or calibration.json. Refusing to fall back silently.")
        logger.warning("registry active=%s incomplete at %s, using legacy paths",
                       want, vdir)

    d = _data_dir()
    _resolved = {"id": "legacy-unversioned", "source": "legacy",
                 "rf": d / LEGACY_RF, "calibration": d / LEGACY_CALIB,
                 "manifest": None}
    return _resolved


def describe() -> dict:
    """Small dict for /health and logs: which model, from where, how good."""
    r = resolve()
    out = {"model_id": r["id"], "resolved_from": r["source"]}
    mp = r.get("manifest")
    if mp and Path(mp).exists():
        try:
            m = json.loads(Path(mp).read_text())
            out["gate"] = m.get("gate")
            out["created"] = m.get("created")
        except Exception:
            pass
    return out
