#!/usr/bin/env python3
"""Range-validate free proxies against the BTS Akamai-WAF'd host, and
download the CONUS road-noise zip through whichever proxies get past the WAF.

Why a custom validator: proxy_pool's built-in check does a full GET of the
target URL, which never finishes within the per-proxy timeout for a
multi-hundred-MB zip — so every proxy "fails". Here we send a 64 KB
`Range: bytes=0-65535` request instead: fast, and it tells us (a) whether the
proxy's exit IP is past the WAF (200/206 + PK zip magic) and (b) the true file
size from Content-Range / Content-Length.

Reuses proxy_pool's scraper + geolocator (limon-ops/scripts/proxy_pool.py) but
NOT its validator. Stateless anonymous GET only — no tokens/cookies.

Subcommands:
    validate   scrape -> Range-test -> write good proxies + report file size
    size       just print the true file size using already-good proxies
    download   stream the full zip via good proxies, with Range resume + rotation
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

LIMON_SCRIPTS = Path("/Users/bwwan3/Documents/GitHub/limon-ops/scripts")
sys.path.insert(0, str(LIMON_SCRIPTS))
from proxy_pool import _fetch_proxies, _geolocate, curl_requests  # noqa: E402

TARGET = "https://www.bts.gov/bts-net-storage/CONUS_road_noise_2020.zip"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.bts.gov/geospatial/national-transportation-noise-map",
}
GOOD_FILE = Path("/Users/bwwan3/Documents/GitHub/property-scores/data/us/bts_good_proxies.txt")


def range_probe(proxy: str, target: str, nbytes: int = 65535, timeout: int = 25):
    """Return dict(ok, status, size, is_zip, exit_country?) or None on failure."""
    try:
        r = curl_requests.get(
            target,
            headers={**HEADERS, "Range": f"bytes=0-{nbytes}"},
            proxies={"http": proxy, "https": proxy},
            timeout=timeout,
            impersonate="chrome131",
            stream=True,
        )
        status = r.status_code
        # read at most nbytes+1 of body
        body = r.content[: nbytes + 1] if r.content is not None else b""
        cr = r.headers.get("Content-Range") or r.headers.get("content-range")
        cl = r.headers.get("Content-Length") or r.headers.get("content-length")
        size = None
        if cr and "/" in cr:
            tail = cr.rsplit("/", 1)[-1].strip()
            if tail.isdigit():
                size = int(tail)
        is_zip = body[:2] == b"PK"
        ok = status in (200, 206) and is_zip
        return {
            "proxy": proxy, "ok": ok, "status": status, "size": size,
            "content_length": int(cl) if (cl and cl.isdigit()) else None,
            "is_zip": is_zip, "nbody": len(body),
            "server": r.headers.get("Server") or r.headers.get("server"),
        }
    except Exception as e:
        return {"proxy": proxy, "ok": False, "status": None, "err": type(e).__name__}


def cmd_validate(args):
    print("[scrape] fetching proxy candidates ...", file=sys.stderr)
    cands = _fetch_proxies()
    print(f"[scrape] {len(cands)} candidates", file=sys.stderr)
    # prefer US/CA exits first
    print("[geo] geolocating (prefer US,CA) ...", file=sys.stderr)
    cc = _geolocate(cands)
    pref = {"US", "CA"}
    tier1 = [p for p in cands if cc.get(p, "??") in pref]
    tier2 = [p for p in cands if cc.get(p, "??") not in pref]
    print(f"[geo] tier1 US/CA: {len(tier1)}  tier2 rest: {len(tier2)}", file=sys.stderr)

    good: list[dict] = []
    true_size = None
    stop = threading.Event()

    def run_tier(tier, label):
        nonlocal true_size
        if stop.is_set():
            return
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(range_probe, p, TARGET, 65535, args.timeout): p for p in tier}
            for f in as_completed(futs):
                res = f.result()
                if res.get("ok"):
                    good.append(res)
                    if res.get("size"):
                        true_size = res["size"]
                    print(f"  [PASS][{label}] {res['proxy']}  status={res['status']} "
                          f"size={res.get('size')} zip={res['is_zip']} "
                          f"({len(good)}/{args.min})", file=sys.stderr, flush=True)
                    if len(good) >= args.min:
                        stop.set()
                        break
                elif res.get("status") and res["status"] not in (403,):
                    # surface non-403 anomalies for debugging
                    print(f"  [?][{label}] {res['proxy']} status={res['status']} "
                          f"zip={res.get('is_zip')}", file=sys.stderr, flush=True)

    run_tier(tier1, "US/CA")
    if len(good) < args.min and not stop.is_set():
        run_tier(tier2, "rest")

    GOOD_FILE.parent.mkdir(parents=True, exist_ok=True)
    with GOOD_FILE.open("w") as fp:
        for g in good:
            fp.write(f"{g['proxy']}\n")

    print("\n========== VALIDATE REPORT ==========")
    print(f"good proxies (past WAF, returned zip): {len(good)}")
    for g in good:
        print(f"  {g['proxy']:32s} status={g['status']} size={g.get('size')} "
              f"server={g.get('server')}")
    if true_size is not None:
        print(f"\nTRUE FILE SIZE: {true_size} bytes = {true_size/1e6:.1f} MB = {true_size/1e9:.3f} GB")
    else:
        print("\nTRUE FILE SIZE: could not read Content-Range (no proxy returned 206 with range)")
    print(f"good proxies written to {GOOD_FILE}")


def cmd_download(args):
    if not GOOD_FILE.exists():
        sys.exit("no good proxies; run validate first")
    proxies = [l.strip() for l in GOOD_FILE.read_text().splitlines() if l.strip()]
    if not proxies:
        sys.exit("good proxy file empty; run validate first")
    dest = Path(args.dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # discover size
    size = args.size
    if not size:
        for p in proxies:
            res = range_probe(p, TARGET, 1024, args.timeout)
            if res.get("size"):
                size = res["size"]
                break
    print(f"target size: {size} bytes ({size/1e6:.1f} MB)" if size else "size unknown")

    have = dest.stat().st_size if dest.exists() else 0
    print(f"resume from {have} bytes")
    attempts = 0
    bad: set[str] = set()
    while (size is None or have < size) and attempts < args.max_attempts:
        attempts += 1
        live = [p for p in proxies if p not in bad]
        if not live:
            print("all proxies exhausted; re-validate needed", file=sys.stderr)
            break
        proxy = random.choice(live)
        try:
            r = curl_requests.get(
                TARGET,
                headers={**HEADERS, "Range": f"bytes={have}-"},
                proxies={"http": proxy, "https": proxy},
                timeout=args.timeout,
                impersonate="chrome131",
                stream=True,
            )
            if r.status_code not in (200, 206):
                print(f"  attempt {attempts}: {proxy} -> HTTP {r.status_code}, drop",
                      file=sys.stderr)
                bad.add(proxy)
                continue
            mode = "ab" if have else "wb"
            t0 = time.time()
            written = 0
            with dest.open(mode) as fp:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    if not chunk:
                        break
                    fp.write(chunk)
                    written += len(chunk)
                    have += len(chunk)
            dt = time.time() - t0
            rate = (written / 1e6) / dt if dt else 0
            print(f"  attempt {attempts}: {proxy} +{written/1e6:.1f}MB in {dt:.0f}s "
                  f"({rate:.2f} MB/s) total={have/1e6:.1f}MB", file=sys.stderr)
            if written == 0:
                bad.add(proxy)
        except Exception as e:
            print(f"  attempt {attempts}: {proxy} err {type(e).__name__}: {e}",
                  file=sys.stderr)
            bad.add(proxy)
    print(f"\nfinal size on disk: {have} bytes; target {size}")
    if size and have >= size:
        print("DOWNLOAD COMPLETE")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate")
    v.add_argument("--min", type=int, default=4)
    v.add_argument("--workers", type=int, default=120)
    v.add_argument("--timeout", type=int, default=25)
    v.set_defaults(func=cmd_validate)
    d = sub.add_parser("download")
    d.add_argument("--dest", default="/Users/bwwan3/Documents/GitHub/property-scores/data/us/CONUS_road_noise_2020.zip")
    d.add_argument("--size", type=int, default=0)
    d.add_argument("--timeout", type=int, default=120)
    d.add_argument("--max-attempts", type=int, default=200)
    d.set_defaults(func=cmd_download)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
