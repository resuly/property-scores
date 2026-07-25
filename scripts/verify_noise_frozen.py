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
# Go through the SAME entry point the licensed feed uses. noise_score() alone
# carries no per-source breakdown -- that comes from noise_debug via
# _noise_for_batch(detail=True), which is what Foundit actually receives.
from property_scores.api.main import _noise_for_batch
out = []
for lat, lng, label in json.load(open(pts_file)):
    try:
        r = _noise_for_batch(lat, lng, detail=True)
    except Exception:
        out.append({"label": label, "error": traceback.format_exc(limit=3)})
        continue
    srcs = []
    for group, rows in sorted((r.get("sources") or {}).items()):
        for s in (rows if isinstance(rows, list) else []):
            # lat/lng MUST be included: they are what makes a row unique. Two
            # different roads can share a name, class and rounded dB, and
            # without coordinates they collapse to one identity and get paired
            # arbitrarily between the two runs -- which shows up as a source
            # whose distance appears to shrink.
            srcs.append({"group": group,
                         **{k: s.get(k) for k in
                            ("source", "type", "route", "road_name", "class",
                             "lat", "lng", "distance_m", "db", "screening_db")}})
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
    cur2 = run_probe(REPO, pts_file, "current branch (repeat)")

    # Determinism first: an unstable payload makes every other comparison
    # meaningless, and production genuinely is unstable here (ties in the source
    # sort break on DuckDB scan order).
    unstable = [c["label"] for c, d in zip(cur, cur2) if c != d]
    if unstable:
        print(f"NOT DETERMINISTIC: {len(unstable)} of {len(cur)} points differ "
              f"between two identical runs of the current code (e.g. "
              f"{unstable[:3]}). Fix the ordering before trusting any diff.")
        return 2
    print(f"determinism: two identical runs of the current code agree on all "
          f"{len(cur)} points")

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

    def _modelled(r):
        """Everything the model produces: scores, dB, and each source's identity
        and level.

        Excludes two things on purpose. `distance_m` is a measurement, not a
        model input, and is deliberately corrected (see score._true_metres). And
        ORDER is compared as a set, because production has no canonical order at
        all -- ties break on DuckDB scan order, so a given production run is one
        arbitrary sequence among many. Determinism is checked separately above;
        here the question is only whether the same sources came back at the same
        levels.
        """
        return (r["score"], r["estimated_db"], r["category"],
                sorted(json.dumps({k: v for k, v in s.items() if k != "distance_m"},
                                  sort_keys=True) for s in r["sources"]))

    diffs = [(p, c) for p, c in zip(prod, cur) if _modelled(p) != _modelled(c)]

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

    print("\nnoise SCORE, dB and every source's level are BIT-IDENTICAL to "
          "production on every point.")

    # The reported distances SHOULD have moved, and only ever upward (the legacy
    # formula could only understate). A run where nothing moved would mean the
    # honest-distance fix silently did nothing.
    # Pair sources by IDENTITY, not position -- the order deliberately changed,
    # so zipping the two lists positionally would compare different roads. The
    # non-distance fields are already proven an identical multiset above, so
    # sorting both sides by that identity pairs them correctly.
    def _by_identity(rows):
        # Distance is the LAST key, not excluded: some rows are genuinely
        # indistinguishable otherwise. The Overture rail fallback returns no
        # coordinates, so Canberra's tram segments differ only in distance --
        # identity alone pairs them crosswise and invents a -2 m "shrink"
        # against its own +2 m. Ordering ties by distance makes the pairing
        # unambiguous; a real shrink still shows, because then the two
        # multisets genuinely differ.
        return sorted(rows, key=lambda s: (
            json.dumps({k: v for k, v in s.items() if k != "distance_m"},
                       sort_keys=True),
            s["distance_m"] if s["distance_m"] is not None else -1))

    deltas = [(c_s["distance_m"] - p_s["distance_m"], p, p_s)
              for p, c in zip(prod, cur)
              for p_s, c_s in zip(_by_identity(p["sources"]), _by_identity(c["sources"]))
              if p_s["distance_m"] is not None and c_s["distance_m"] is not None]
    moved = [d for d, _, _ in deltas if d != 0]
    shrank = [(d, p, s) for d, p, s in deltas if d < -1]
    if not deltas:
        print("no per-source distances returned -- cannot check the reporting fix")
        return 0
    print(f"\nreported distances: {len(moved)} of {len(deltas)} corrected "
          f"(max +{max([d for d, _, _ in deltas], default=0):.0f} m)")
    if shrank:
        d, p, s = shrank[0]
        print(f"UNEXPECTED: {len(shrank)} distance(s) got SHORTER, e.g. "
              f"{p['label']} {s.get('road_name') or s.get('class')} {d:.0f} m. "
              f"The legacy formula understates, so a correction can only grow.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
