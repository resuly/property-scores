"""Behavioural contract for the production Gunicorn configuration."""

import importlib.util
import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace

from gunicorn.app.base import Application
from gunicorn.workers import base as worker_base
from packaging.requirements import Requirement
from packaging.version import Version

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
    assert app.cfg.worker_class.__name__ == "UvicornWorker"
    assert app.cfg.worker_class.__module__.startswith("uvicorn_worker")


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
    """Exercise Gunicorn 26.2 Worker.init_process ordering, not source text."""
    events = []
    worker = object.__new__(worker_base.Worker)
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

    worker_base.Worker.init_process(worker)

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
