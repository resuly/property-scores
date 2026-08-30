"""Behavioural contract for the production Gunicorn configuration."""

import importlib.util
import os
import shutil
import subprocess
import sys
import zipfile
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace

from gunicorn.app.base import Application
from gunicorn.workers import base as worker_base
from packaging.requirements import Requirement
from packaging.version import Version
from uvicorn_worker import UvicornWorker

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "gunicorn_conf.py"


def _load_config_module(name="property_scores_gunicorn_conf"):
    spec = importlib.util.spec_from_file_location(name, CONFIG_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _ConfigApplication(Application):
    """Gunicorn's real config loader without parsing this pytest process argv."""

    def load_config(self):
        pass

    def load(self):
        return None


def test_gunicorn_parser_loads_control_socket_disable_and_worker_hook():
    app = _ConfigApplication()

    app.load_config_from_file(str(CONFIG_PATH))

    assert app.cfg.control_socket_disable is True
    assert callable(app.cfg.post_worker_init)
    assert app.cfg.worker_class is UvicornWorker


def test_post_worker_init_actually_enables_faulthandler_in_fresh_process():
    code = """
import faulthandler
import gunicorn_conf
faulthandler.disable()
assert not faulthandler.is_enabled()
gunicorn_conf.post_worker_init(object())
assert faulthandler.is_enabled()
print('enabled')
"""
    env = {**os.environ, "PYTHONPATH": str(ROOT)}

    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env,
        capture_output=True, text=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "enabled"


def test_post_worker_init_requests_all_thread_tracebacks(monkeypatch):
    config = _load_config_module("property_scores_gunicorn_conf_all_threads")
    calls = []
    monkeypatch.setattr(config.faulthandler, "enable", lambda **kwargs: calls.append(kwargs))

    config.post_worker_init(object())

    assert calls == [{"all_threads": True}]


def test_installed_gunicorn_calls_post_worker_hook_after_signal_initialisation(
        monkeypatch):
    """Bind lifecycle ordering to the worker parsed from the real config.

    uvicorn-worker 0.4 inherits Gunicorn's implementation unchanged. If a
    future version overrides init_process, this identity pin turns red before
    the generic Gunicorn harness can give false comfort about hook order.
    """
    app = _ConfigApplication()
    app.load_config_from_file(str(CONFIG_PATH))
    worker_class = app.cfg.worker_class
    assert worker_class is UvicornWorker
    assert issubclass(worker_class, worker_base.Worker)
    assert worker_class.init_process is worker_base.Worker.init_process

    events = []
    worker = object.__new__(worker_class)
    worker.cfg = SimpleNamespace(
        env={}, uid=os.getuid(), gid=os.getgid(), initgroups=False,
        reload=False, post_worker_init=lambda _worker: events.append("post_worker_init"),
    )
    worker.sockets = []
    worker.tmp = SimpleNamespace(fileno=lambda: 12)
    worker.log = SimpleNamespace(close_on_exec=lambda: None)
    worker.reloader = None
    worker.init_signals = lambda: events.append("init_signals")
    worker.load_wsgi = lambda: events.append("load_wsgi")
    worker.run = lambda: events.append("run")
    worker.booted = False
    monkeypatch.setattr(worker_base.util, "set_owner_process", lambda *a, **k: None)
    monkeypatch.setattr(worker_base.util, "seed", lambda: None)
    monkeypatch.setattr(worker_base.util, "set_non_blocking", lambda _fd: None)
    monkeypatch.setattr(worker_base.util, "close_on_exec", lambda _fd: None)
    monkeypatch.setattr(worker_base.os, "pipe", lambda: (10, 11))

    worker_class.init_process(worker)

    assert events == ["init_signals", "load_wsgi", "post_worker_init", "run"]
    assert worker.booted is True


def test_api_optional_dependencies_enforce_tested_gunicorn_floors():
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10 support
        import tomli as tomllib

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = {
        requirement.name: requirement
        for requirement in map(
            Requirement, config["project"]["optional-dependencies"]["api"])
    }
    gunicorn = requirements["gunicorn"]
    uvicorn_worker = requirements["uvicorn-worker"]

    assert Version("26.2") in gunicorn.specifier
    assert Version("26.1.999") not in gunicorn.specifier
    assert Version("0.4") in uvicorn_worker.specifier
    assert Version("0.3.999") not in uvicorn_worker.specifier
    assert Version(metadata.version("gunicorn")) in gunicorn.specifier
    assert Version(metadata.version("uvicorn-worker")) in uvicorn_worker.specifier


def test_setuptools_discovers_only_property_scores_packages():
    from setuptools.config.pyprojecttoml import read_configuration

    config = read_configuration(str(ROOT / "pyproject.toml"))
    packages = set(config["tool"]["setuptools"]["packages"])

    assert "property_scores" in packages
    assert "property_scores.contamination.sources" in packages
    assert all(name == "property_scores" or name.startswith("property_scores.")
               for name in packages)
    assert not any(name.startswith(("data", "tests", "scripts", "reg09_localtest"))
                   for name in packages)


def _wheel_builder_python():
    """A Python with setuptools+wheel; prefer this test environment."""
    suffix = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    candidates = [Path(sys.executable), Path(sys.base_prefix) / suffix]
    seen = set()
    for candidate in candidates:
        if candidate in seen or not candidate.exists():
            continue
        seen.add(candidate)
        probe = subprocess.run(
            [str(candidate), "-c", "import setuptools, wheel"],
            capture_output=True, text=True, check=False,
        )
        if probe.returncode == 0:
            return candidate
    raise AssertionError("wheel build test requires a Python with setuptools and wheel")


def test_built_wheel_installs_runtime_static_assets_and_imports_isolated(tmp_path):
    """Build the real wheel, target-install it, then import away from the repo."""
    source = tmp_path / "source"
    source.mkdir()
    shutil.copy2(ROOT / "pyproject.toml", source / "pyproject.toml")
    shutil.copy2(ROOT / "README.md", source / "README.md")
    shutil.copytree(
        ROOT / "property_scores", source / "property_scores",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    builder = _wheel_builder_python()
    build = subprocess.run(
        [str(builder), "-m", "pip", "wheel", "--no-deps",
         "--no-build-isolation", "--wheel-dir", str(wheel_dir), str(source)],
        capture_output=True, text=True, check=False,
    )
    assert build.returncode == 0, build.stderr or build.stdout
    wheels = list(wheel_dir.glob("property_scores-*.whl"))
    assert len(wheels) == 1

    expected_assets = {
        f"property_scores/api/static/{path.name}"
        for path in (ROOT / "property_scores/api/static").glob("*.html")
    }
    expected_assets.add("property_scores/api/static/css/styles.css")
    with zipfile.ZipFile(wheels[0]) as archive:
        names = set(archive.namelist())
    assert expected_assets <= names
    assert "property_scores/api/static/css/input.css" not in names
    assert not any(name.startswith(("data/", "tests/", "scripts/", "reg09_localtest/"))
                   for name in names)

    target = tmp_path / "target"
    install = subprocess.run(
        [str(builder), "-m", "pip", "install", "--no-deps",
         "--target", str(target), str(wheels[0])],
        capture_output=True, text=True, check=False,
    )
    assert install.returncode == 0, install.stderr or install.stdout

    code = """
import pathlib
import property_scores.api.main as main
target = pathlib.Path(__import__('sys').argv[1]).resolve()
assert pathlib.Path(main.__file__).resolve().is_relative_to(target)
assert (main.STATIC_DIR / 'index.html').is_file()
assert (main.STATIC_DIR / 'contamination.html').is_file()
assert (main.STATIC_DIR / 'css' / 'styles.css').is_file()
print(main.STATIC_DIR)
"""
    env = {**os.environ, "PYTHONPATH": str(target), "PYTHONDONTWRITEBYTECODE": "1"}
    imported = subprocess.run(
        [sys.executable, "-c", code, str(target)], cwd=tmp_path, env=env,
        capture_output=True, text=True, check=False,
    )
    assert imported.returncode == 0, imported.stderr or imported.stdout
    assert str(target) in imported.stdout
