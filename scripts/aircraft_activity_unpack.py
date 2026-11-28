"""Unpack the aircraft-activity .npz grids into .npy files the service can mmap.

    .venv/bin/python scripts/aircraft_activity_unpack.py --dir $AIRCRAFT_ACTIVITY_DIR

Writes <dir>/unpacked/<city>/{<metric>.npy, hourly.npy, q1_<key>.npy, ...,
COMPLETE}. The service (property_scores/aircraft_activity/store.py) uses a
city's unpacked files, memory-mapped, only when its COMPLETE marker exists;
otherwise it decompresses the .npz into a small in-process LRU. Unpacking
costs ~640 MB of disk for the 14-city 12-month release and makes the resident
cost per worker a few pages per query instead of ~45 MB per loaded city.

Each file is written to a temporary name and renamed, and COMPLETE is written
last after every array has been read back and compared, so a crash mid-way
leaves the city on the npz path rather than half-mapped. Re-running replaces
everything (use it after every data drop).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from property_scores.aircraft_activity.store import (  # noqa: E402
    METRIC_KEYS, QUARTER_KEYS, QUARTERS)


def _save(dst: Path, a: np.ndarray):
    tmp = dst.with_suffix(".tmp.npy")
    np.save(tmp, np.ascontiguousarray(a, dtype=np.float32))
    back = np.load(tmp, mmap_mode="r")
    if back.shape != a.shape or not np.array_equal(back, a.astype(np.float32), equal_nan=True):
        raise RuntimeError(f"read-back mismatch for {dst}")
    os.replace(tmp, dst)


def unpack_city(d: Path, city: str) -> int:
    out = d / "unpacked" / city
    out.mkdir(parents=True, exist_ok=True)
    marker = out / "COMPLETE"
    if marker.exists():
        marker.unlink()
    n = 0
    with np.load(d / f"grid_{city}.npz") as z:
        for k in METRIC_KEYS:
            if k in z.files:
                _save(out / f"{k}.npy", z[k]); n += 1
        if "hourly" in z.files:
            _save(out / "hourly.npy", z["hourly"]); n += 1
    for q in QUARTERS:
        f = d / f"grid_{city}_{q}.npz"
        if f.exists():
            with np.load(f) as z:
                for k in QUARTER_KEYS:
                    if k in z.files:
                        _save(out / f"{q}_{k}.npy", z[k]); n += 1
    marker.write_text("ok\n")
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, type=Path)
    a = ap.parse_args()
    d = a.dir.expanduser()
    cities = sorted(p.stem[len("meta_"):] for p in d.glob("meta_*.json"))
    if not cities:
        print(f"no meta_*.json in {d}", file=sys.stderr)
        return 2
    for c in cities:
        print(c, unpack_city(d, c), "arrays")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
