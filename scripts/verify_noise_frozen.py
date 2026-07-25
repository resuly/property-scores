"""Prove option C froze noise: bit-identical scores against production code.

Option C keeps the noise pipeline on the pre-2026-07 distance formula
(`legacy_distance=True`) while every other score component moves to true metres.
The whole claim rests on noise being UNCHANGED, so this asserts it rather than
assuming it -- against a real checkout of the production commit, not a
reimplementation.

Runs the same points through two engine copies in separate processes (the
modules share names, so one process cannot hold both) and diffs the full noise
payload: score, estimated_db, lden_source, category, and every per-source
distance and dB in `sources`.

Three traps this deliberately avoids (all of them produced confidently wrong
numbers during the 2026-07-25 audit):

  1. `noise_score` keeps a 24 h sqlite result cache keyed on lat/lng/radius, and
     both copies share DATA_DIR -- so they replay each other's values and every
     address looks unchanged. Both probes stub `_cache_get`/`_cache_put`.
  2. Production flags live in the systemd unit, NOT `.env`. Without
     NOISE_TRANSFER / NOISE_QUIET_RECAL / NOISE_RAIL_RECAL you measure a physics
     path production never uses. This sets them and FAILS if any point comes
     back with `lden_source != "transfer"`.
  3. `noise/transfer.py` resolves its own repo-relative `data/`, ignoring
     DATA_DIR, so the reference worktree gets a symlink to the real data dir.

Usage:
    .venv/bin/python scripts/verify_noise_frozen.py            # 200 points
    .venv/bin/python scripts/verify_noise_frozen.py --n 40     # quick
    .venv/bin/python scripts/verify_noise_frozen.py --ref-commit 70cf13b
"""
import argparse
import csv
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROD_FLAGS = {"NOISE_TRANSFER": "1", "NOISE_QUIET_RECAL": "1", "NOISE_RAIL_RECAL": "1"}

PROBE = r'''
import json, os, sys, traceback
repo, pts_file = sys.argv[1], sys.argv[2]
sys.path.insert(0, repo)
os.chdir(repo)
from property_scores.noise import score as ns
# Trap 1: never let the shared 24 h sqlite cache answer for either copy.
ns._cache_get = lambda *a, **k: None
ns._cache_put = lambda *a, **k: None
out = []
for lat, lng, label in json.load(open(pts_file)):
    try:
        r = ns.noise_score(lat, lng)
    except Exception:
        out.append({"label": label, "error": traceback.format_exc(limit=3)})
        continue
    srcs = [
        {k: s.get(k) for k in ("type", "name", "distance_m", "db", "class")}
        for s in (r.get("sources") or [])
    ]
    out.append({
        "label": label, "lat": lat, "lng": lng,
        "score": r.get("score"), "estimated_db": r.get("estimated_db"),
        "lden_source": r.get("lden_source"), "category": r.get("category"),
        "sources": srcs,
    })
print("@@JSON@@" + json.dumps(out))
'''


def sample_points(n: int) -> list:
    """Real coordinates across all seven sampled cities/states, round-robin so a
    small --n still spans every state rather than one city."""
    per_city = {}
    for fn in sorted(glob.glob(str(REPO / "data/ambient_sample/antn_*_buildings_.csv"))):
        city = Path(fn).stem.replace("antn_", "").replace("_buildings_", "")
        pts = []
        for r in csv.DictReader(open(fn)):
            m = re.search(r"POINT \(([-\d.]+) ([-\d.]+)\)", r.get("geometry", ""))
            if m:
                pts.append((float(m.group(1)), float(m.group(2)), city))
        step = max(1, len(pts) // max(1, n))
        per_city[city] = pts[::step]
    out, i = [], 0
    while len(out) < n and any(len(v) > i for v in per_city.values()):
        for city in sorted(per_city):
            if len(per_city[city]) > i and len(out) < n:
                out.append(per_city[city][i])
        i += 1
    return out


def run_probe(repo: Path, pts_file: Path, label: str) -> list:
    env = {**os.environ, **PROD_FLAGS, "DATA_DIR": str(REPO / "data")}
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(PROBE)
        probe_path = f.name
    try:
        p = subprocess.run([sys.executable, probe_path, str(repo), str(pts_file)],
                           capture_output=True, text=True, env=env, cwd=str(repo))
    finally:
        os.unlink(probe_path)
    marker = [l for l in p.stdout.splitlines() if l.startswith("@@JSON@@")]
    if not marker:
        print(f"probe FAILED for {label}\n--- stdout ---\n{p.stdout[-2000:]}"
              f"\n--- stderr ---\n{p.stderr[-3000:]}")
        sys.exit(2)
    return json.loads(marker[-1][len("@@JSON@@"):])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--ref-commit", default="70cf13b",
                    help="production commit to diff against")
    a = ap.parse_args()

    wt = Path(tempfile.gettempdir()) / f"psref-{a.ref_commit}"
    if not wt.exists():
        subprocess.run(["git", "-C", str(REPO), "worktree", "add", "--detach",
                        str(wt), a.ref_commit], check=True,
                       capture_output=True, text=True)
    # Trap 3: transfer.py resolves <repo>/data itself, ignoring DATA_DIR, and the
    # worktree checks out a REAL data/ (a few small files are git-tracked despite
    # the .gitignore). So link in each missing entry rather than the directory --
    # symlinking the whole dir silently no-ops and the RF then "fails to load",
    # which shows up as an all-physics run.
    (wt / "data").mkdir(exist_ok=True)
    linked = 0
    for src in (REPO / "data").iterdir():
        dst = wt / "data" / src.name
        if not dst.exists():
            dst.symlink_to(src)
            linked += 1
    if linked:
        print(f"linked {linked} data entries into the reference worktree")

    pts = sample_points(a.n)
    if not pts:
        print("no sample points found (data/ambient_sample/antn_*.csv missing)")
        return 2
    pts_file = Path(tempfile.gettempdir()) / "noise_verify_pts.json"
    pts_file.write_text(json.dumps(pts))
    cities = sorted({p[2] for p in pts})
    print(f"{len(pts)} points across {len(cities)} cities: {', '.join(cities)}")
    print(f"reference = {a.ref_commit} at {wt}")
    print(f"flags     = {' '.join(f'{k}={v}' for k, v in PROD_FLAGS.items())}\n")

    prod = run_probe(wt, pts_file, f"production {a.ref_commit}")
    cur = run_probe(REPO, pts_file, "current branch")

    errs = [r for r in prod + cur if r.get("error")]
    if errs:
        print(f"{len(errs)} point(s) raised; first:\n{errs[0]['error']}")
        return 2

    # Trap 2: prove we are on the production code path before trusting anything.
    off = [r["label"] for r in prod + cur if r.get("lden_source") != "transfer"]
    if off:
        print(f"NOT on the production path: {len(off)} result(s) with "
              f"lden_source != 'transfer' (e.g. {off[:3]}).\n"
              f"Production sets NOISE_TRANSFER=1 in the systemd unit; a physics "
              f"result means the transfer model failed to load.")
        return 2

    diffs = []
    for p, c in zip(prod, cur):
        if (p["score"], p["estimated_db"], p["category"], p["sources"]) != \
           (c["score"], c["estimated_db"], c["category"], c["sources"]):
            diffs.append((p, c))

    print(f"checked {len(prod)} points, all on lden_source=transfer")
    if diffs:
        print(f"\nNOISE IS NOT FROZEN -- {len(diffs)} of {len(prod)} differ:\n")
        for p, c in diffs[:10]:
            print(f"  {p['label']} {p['lat']:.5f},{p['lng']:.5f}: "
                  f"score {p['score']} -> {c['score']}, "
                  f"dB {p['estimated_db']} -> {c['estimated_db']}")
            if p["sources"] != c["sources"]:
                print(f"     sources differ ({len(p['sources'])} -> {len(c['sources'])})")
        return 1

    print("\nnoise is BIT-IDENTICAL to production on every point, including "
          "every per-source distance and dB.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
