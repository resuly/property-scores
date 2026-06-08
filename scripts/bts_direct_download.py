#!/usr/bin/env python3
"""Direct download of a BTS NTAD zip using curl_cffi Chrome impersonation.

Finding (2026-06-07): the BTS Akamai WAF blocks on TLS/HTTP-2 fingerprint, not
on geo/IP. Bare `curl` -> 403 (AkamaiGHost). curl_cffi with impersonate=chrome
-> 200/206 (AkamaiNetStorage) straight from the Melbourne home IP. No proxy
needed. We still use Range resume so a dropped connection doesn't restart.
"""
from __future__ import annotations
import sys, time
from pathlib import Path

sys.path.insert(0, "/Users/bwwan3/Documents/GitHub/limon-ops/scripts")
from proxy_pool import curl_requests  # noqa: E402

H = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.bts.gov/geospatial/national-transportation-noise-map",
}


def download(url: str, dest: Path, max_attempts: int = 40):
    dest.parent.mkdir(parents=True, exist_ok=True)
    # discover size
    probe = curl_requests.get(url, headers={**H, "Range": "bytes=0-1024"},
                              timeout=60, impersonate="chrome131")
    cr = probe.headers.get("Content-Range") or ""
    size = int(cr.rsplit("/", 1)[-1]) if "/" in cr else None
    print(f"size={size} ({size/1e6:.1f} MB)" if size else "size unknown", flush=True)

    for attempt in range(1, max_attempts + 1):
        have = dest.stat().st_size if dest.exists() else 0
        if size and have >= size:
            print("COMPLETE", flush=True)
            return have
        try:
            r = curl_requests.get(url, headers={**H, "Range": f"bytes={have}-"},
                                  timeout=600, impersonate="chrome131", stream=True)
            if r.status_code not in (200, 206):
                print(f"attempt {attempt}: HTTP {r.status_code}; retry", flush=True)
                time.sleep(3)
                continue
            mode = "ab" if have else "wb"
            t0 = time.time(); written = 0; last = t0
            with dest.open(mode) as fp:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    if not chunk:
                        break
                    fp.write(chunk)
                    written += len(chunk)
                    if time.time() - last > 15:
                        cur = have + written
                        pct = (cur / size * 100) if size else 0
                        print(f"  {cur/1e6:.0f}/{(size or 0)/1e6:.0f} MB "
                              f"({pct:.0f}%)  {written/1e6/(time.time()-t0):.2f} MB/s",
                              flush=True)
                        last = time.time()
            print(f"attempt {attempt}: +{written/1e6:.1f} MB in "
                  f"{time.time()-t0:.0f}s", flush=True)
        except Exception as e:
            print(f"attempt {attempt}: {type(e).__name__}: {e}; resume", flush=True)
            time.sleep(3)
    have = dest.stat().st_size if dest.exists() else 0
    print(f"stopped at {have}/{size}", flush=True)
    return have


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else \
        "https://www.bts.gov/bts-net-storage/CONUS_road_noise_2020.zip"
    dest = Path(sys.argv[2] if len(sys.argv) > 2 else
                "/Users/bwwan3/Documents/GitHub/property-scores/data/us/CONUS_road_noise_2020.zip")
    download(url, dest)
