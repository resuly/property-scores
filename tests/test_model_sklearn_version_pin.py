"""The active noise RF, the pyproject pin and the runtime agree on scikit-learn.

rf.pkl is a pickled RandomForestRegressor. sklearn stamps the version that
pickled it into every estimator's state (`_sklearn_version`) and only warns
(InconsistentVersionWarning) when a different version unpickles it; it then
runs with whatever tree internals the new version expects. The [ml] extra used
to say `scikit-learn>=1.4`, so any reinstall could silently pick a version the
artefact was never produced or validated with.

The contract: the version recorded inside the active artefact, the exact pin in
pyproject's [ml] extra, and the installed runtime are the same string. Changing
any one of them without the others (a reinstall, a retrain, a pin bump) fails
here. The artefact is a gitignored build product under data/, so the artefact
check skips on a clean checkout; point DATA_DIR at a data dir that has it.

The version is read from the pickle opcode stream without unpickling: no 114 MB
load, no sklearn import (which also means it works where scipy cannot load).
"""
import pickle
import pickletools
import re
from importlib import metadata
from pathlib import Path

import pytest

from property_scores.noise import model_registry

ROOT = Path(__file__).resolve().parents[1]
_STRING_OPS = {"SHORT_BINUNICODE", "BINUNICODE", "BINUNICODE8", "UNICODE",
               "SHORT_BINSTRING", "BINSTRING", "STRING"}


def pinned_sklearn_version(pyproject_text: str) -> str:
    m = re.search(r"^ml\s*=\s*\[(.*?)\]", pyproject_text, re.M | re.S)
    assert m, "pyproject has no [ml] extra"
    specs = re.findall(r'"scikit-learn\s*([^"]*)"', m.group(1))
    assert len(specs) == 1, f"[ml] must name scikit-learn exactly once: {specs}"
    exact = re.fullmatch(r"==\s*(\d+\.\d+\.\d+)", specs[0].strip())
    assert exact, (f"[ml] scikit-learn must be an exact ==X.Y.Z pin, "
                   f"got {specs[0]!r}")
    return exact.group(1)


def artefact_sklearn_version(path: Path, max_ops: int = 20000) -> str:
    """First `_sklearn_version` value in a pickle, read from opcodes only."""
    with open(path, "rb") as f:
        want_value = False
        for i, (op, arg, _pos) in enumerate(pickletools.genops(f)):
            if i >= max_ops:
                break
            if op.name not in _STRING_OPS:
                continue
            s = arg.decode() if isinstance(arg, bytes) else arg
            if want_value:
                return s
            want_value = s == "_sklearn_version"
    raise AssertionError(f"no _sklearn_version in the first {max_ops} "
                         f"pickle ops of {path}")


def _active_rf() -> Path:
    r = model_registry.resolve(force=True)
    rf = Path(r["rf"])
    if not rf.exists():
        pytest.skip(f"active noise model artefact absent ({rf}); gitignored "
                    "build product, set DATA_DIR to a data dir that has it")
    return rf


def _pin() -> str:
    return pinned_sklearn_version((ROOT / "pyproject.toml").read_text())


def test_parser_reads_version_from_a_real_pickle(tmp_path):
    p = tmp_path / "m.pkl"
    p.write_bytes(pickle.dumps(
        {"n_estimators": 3, "_sklearn_version": "9.8.7"}, protocol=4))
    assert artefact_sklearn_version(p) == "9.8.7"


def test_parser_refuses_a_pickle_without_a_version(tmp_path):
    p = tmp_path / "m.pkl"
    p.write_bytes(pickle.dumps({"n_estimators": 3}, protocol=4))
    with pytest.raises(AssertionError):
        artefact_sklearn_version(p)


def test_pin_must_be_exact():
    assert pinned_sklearn_version('ml = ["xgboost>=2.0", "scikit-learn==1.7.2"]') \
        == "1.7.2"
    with pytest.raises(AssertionError):
        pinned_sklearn_version('ml = ["xgboost>=2.0", "scikit-learn>=1.4"]')


def test_active_artefact_matches_pyproject_pin():
    rf = _active_rf()
    assert artefact_sklearn_version(rf) == _pin(), (
        f"{rf} was pickled by a different scikit-learn than [ml] pins. "
        "Re-pin to the artefact's version; do not retrain to match the pin.")


def test_runtime_matches_pyproject_pin():
    try:
        installed = metadata.version("scikit-learn")
    except metadata.PackageNotFoundError:
        pytest.skip("scikit-learn not installed ([ml] extra absent)")
    assert installed == _pin(), (
        f"installed scikit-learn {installed} != [ml] pin {_pin()}; "
        "the active rf.pkl would load with InconsistentVersionWarning")


def test_runtime_matches_active_artefact():
    rf = _active_rf()
    try:
        installed = metadata.version("scikit-learn")
    except metadata.PackageNotFoundError:
        pytest.skip("scikit-learn not installed ([ml] extra absent)")
    assert installed == artefact_sklearn_version(rf)
