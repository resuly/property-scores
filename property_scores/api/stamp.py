"""A short string that changes whenever this service would score differently.

Consumers cache /scores results for a long time (DA Leads keys them per parcel
and serves the cached payload for weeks), and a cached score carries no record
of what produced it. So when a model is swapped, a flag is flipped or an input
artefact is replaced, every already-cached parcel keeps serving the old number
with nothing in the payload able to say so. The only invalidation mechanism
that existed was a human remembering to bump a version string in the consumer's
source and redeploy it -- `scores:v7` in da_leads, whose own comment concedes
"otherwise wrong values live for up to 90 days".

This makes it data-driven instead: the consumer asks what the stamp is, and
treats a cached payload stamped with anything else as a miss.

Deliberately computed from the RUNNING PROCESS, not written by a deploy script.
A deploy script records what was deployed; it cannot know whether the service
was restarted, whether NOISE_MODEL_ID overrides the registry on this box, or
whether the artefact it copied is the one that loaded. Every one of those gaps
produces the same failure as having no stamp at all -- a stamp that says
"changed" when nothing did is a wasted recompute, one that says "unchanged"
when something did is a wrong answer served for weeks.

WHAT IT COVERS, and what it does not:

  covered  the noise config signature (model version + transfer/ML/recal flags
           and their tunables), the resolved noise model artefact id, the
           scoring code, and the declared input ARTEFACTS below (content-hashed
           where they are small, size+mtime for the 114 MB model pickle).
  NOT covered  changes to the raster TILES underneath lc.vrt. The .vrt is a
           catalogue: rewriting the COGs it points at changes no byte of the
           .vrt itself, and walking a continent of tiles on every poll is not
           an option. Nor does anything else catch it -- a restart will NOT
           move the stamp, because the code term is a content hash of the
           source, which a restart does not change.

           So this one needs a deliberate act: when you swap a mosaic, deploy
           with `PROPERTY_SCORES_REV` set to something new (a date, a ticket).
           That is the documented mechanism, and it is the only one. An earlier
           draft of this file claimed "pair it with a restart and the
           code-revision term will move" -- it does not, and believing it would
           leave every consumer serving bushfire scores from the old mosaic for
           the full downstream TTL.
"""
from __future__ import annotations

import hashlib
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]

_code_revision: str | None = None


def code_revision() -> str:
    """Identifier for the code this process is running.

    Resolved once per process because that is exactly what it describes: a
    running process cannot change its own code. Order is explicit-over-inferred
    so a deploy that sets the env var is authoritative and a dev checkout still
    gets a real answer.

    RESOLVE IT AT STARTUP, not on first use -- api/main.py does. A deploy is
    `git pull` then restart, and in the window between the two the working tree
    is ahead of the running process. Resolving lazily inside that window reads
    the NEW commit while the OLD code is still computing scores, so consumers
    flush their caches and refill them with old-code payloads stamped as new;
    the restart then does not change the stamp, and those payloads are trusted
    indefinitely. Resolving at startup makes the answer "the tree as of when
    this process began", which is what actually loaded.
    """
    global _code_revision
    if _code_revision is not None:
        return _code_revision

    env = os.environ.get("PROPERTY_SCORES_REV", "").strip()
    if env:
        _code_revision = f"env:{env}"
        return _code_revision

    # A CONTENT HASH OF THE SCORING CODE, not the repo's git HEAD.
    #
    # HEAD was the first version of this and it was wrong in the expensive
    # direction. This repo holds docs/, CHANGES.md, scripts/, tests/ and
    # api/static/*.html alongside the scoring modules, and its history is full
    # of docs-only and static-page-only commits. Keying on HEAD means a README
    # fix invalidates every cached score for every parcel downstream -- nine
    # live models recomputed per address, for a change that cannot move a
    # number. This module's own docstring calls that out as the failure to
    # avoid, so it must not be the common case.
    #
    # Hashing file CONTENT rather than mtime for the same reason in miniature:
    # a redeploy that rewrites an unchanged file (rsync, fresh clone) must not
    # count as a change.
    digest = hashlib.blake2s(digest_size=8)
    # Presentation, not scoring: these render numbers, they do not produce
    # them, and they change far more often. Compared as a PATH PREFIX, not as a
    # substring of the full path: `"api/static" in p.as_posix()` would also
    # drop a future property_scores/api/static_helpers.py, and would drop the
    # entire tree if the repo were ever checked out under a directory whose own
    # name contained "api/static".
    pkg = _REPO_ROOT / "property_scores"
    excluded = pkg / "api" / "static"
    files = sorted(
        p for p in pkg.rglob("*.py") if excluded not in p.parents
    )
    hashed = 0
    for p in files:
        try:
            digest.update(p.relative_to(_REPO_ROOT).as_posix().encode())
            digest.update(p.read_bytes())
            hashed += 1
        except OSError:
            logger.warning("score stamp: could not read %s", p)
    if hashed:
        _code_revision = f"py:{digest.hexdigest()}"
    else:
        # Never silently absent: an unknown code revision would make the stamp
        # claim more stability than it has. A per-process marker guarantees a
        # restart invalidates, which is the weakest honest answer.
        logger.error("score stamp: no scoring sources readable under %s; "
                     "falling back to a per-process revision", _REPO_ROOT)
        _code_revision = f"pid:{os.getpid()}"
    return _code_revision


def repo_head() -> str:
    """Git HEAD, for /version's debugging output only.

    Deliberately NOT part of the stamp -- see code_revision. It answers "which
    commit is checked out", which is what a human wants when explaining a
    stamp change, and is exactly the wrong thing to invalidate caches on.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        logger.debug("git rev-parse unavailable", exc_info=True)
    return "unknown"


# Artefacts small enough to hash by content. size+mtime is a proxy for "has
# this changed", and it is wrong in both directions: a redeploy that rewrites a
# byte-identical registry.json moves the mtime (needless flush), and an in-place
# edit that preserves both would not move it at all. For a few KB of JSON there
# is no reason to accept a proxy. The 114 MB rf.pkl keeps size+mtime, because
# hashing it on every poll is not worth it and a model swap changes its size or
# is accompanied by a registry change anyway.
#
# lc.vrt is in here too, at 34 KB. It does NOT close the gap described in the
# module docstring -- rewriting the COGs it catalogues leaves the .vrt
# untouched -- but it does catch a REBUILT catalogue (tiles added or removed,
# paths changed), which is the common shape of a mosaic change, and mtime alone
# would have missed a rebuild that happened to produce the same size.
_CONTENT_HASHED_ARTEFACTS = {"noise_registry", "noise_calibration",
                             "worldcover_vrt"}
_CONTENT_HASH_MAX_BYTES = 2_000_000


def _artefact_paths() -> list[tuple[str, Path]]:
    """Declared input artefacts whose replacement changes scores.

    The PATHS come from model_registry.resolve(), which memoises, so they are
    fixed for the life of the process; what is read fresh on every call is
    their content or stat. That is the part that matters, because these files
    can be swapped under a running process -- lc.vrt is opened per call.
    """
    paths: list[tuple[str, Path]] = []
    try:
        from property_scores.noise import model_registry
        paths.append(("noise_registry", model_registry.registry_path()))
        resolved = model_registry.resolve()
        for key in ("rf", "calibration"):
            p = resolved.get(key)
            if p:
                paths.append((f"noise_{key}", Path(p)))
    except Exception:
        logger.exception("noise model artefacts unavailable for the score stamp")
    try:
        from property_scores.bushfire.score import _LC_VRT
        paths.append(("worldcover_vrt", Path(_LC_VRT)))
    except Exception:
        logger.exception("land-cover artefact unavailable for the score stamp")
    # VIC Sands attribution depends on the shared cadastre snapshot. A parcel
    # DB swap can move a historical activity onto/off the queried lot without
    # changing score code, so downstream parcel caches must see a new stamp.
    paths.append(("parcels_db", Path(os.environ.get(
        "PARCELS_DB", "/data/parcels/parcels.duckdb"))))
    return paths


def _artefact_tokens() -> dict[str, str]:
    out: dict[str, str] = {}
    for label, path in _artefact_paths():
        try:
            st = path.stat()
            if (label in _CONTENT_HASHED_ARTEFACTS
                    and st.st_size <= _CONTENT_HASH_MAX_BYTES):
                out[label] = "sha:" + hashlib.blake2s(
                    path.read_bytes(), digest_size=8).hexdigest()
            else:
                out[label] = f"{st.st_size}:{int(st.st_mtime)}"
        except OSError:
            # "absent" is a real state that changes scores: a missing lc.vrt
            # degrades bushfire fuel to the building-density proxy (see the
            # startup check in api/main.py). It must be part of the stamp, so
            # that restoring the mosaic invalidates everything scored without
            # it.
            out[label] = "absent"
    return out


def components() -> dict[str, str]:
    """The parts the stamp is made of, for /version and for debugging.

    Exposed rather than hidden inside the hash so that "why did every cache
    entry just flush" has an answer other than "the hash changed".
    """
    parts: dict[str, str] = {"code": code_revision()}
    try:
        from property_scores.noise.score import _CONFIG_SIG
        parts["noise_config"] = _CONFIG_SIG
    except Exception:
        logger.exception("noise config signature unavailable for the score stamp")
        parts["noise_config"] = "unavailable"
    try:
        from property_scores.noise import model_registry
        r = model_registry.resolve()
        parts["noise_model"] = f"{r['id']}@{r['source']}"
    except Exception:
        logger.exception("noise model id unavailable for the score stamp")
        parts["noise_model"] = "unavailable"
    parts.update(_artefact_tokens())
    return parts


def model_stamp() -> str:
    """12 hex chars over `components()`.

    Short because it is carried in a cached payload and compared, never parsed.
    Order-independent: sorted before hashing, so adding a component to the dict
    in a different position does not by itself invalidate every cache.
    """
    parts = components()
    blob = "\n".join(f"{k}={parts[k]}" for k in sorted(parts))
    return hashlib.blake2s(blob.encode(), digest_size=6).hexdigest()
