#!/usr/bin/env python3
"""
Download + validate the US NTAD National Transportation Noise Map ROAD layer.

Source: U.S. DOT / Bureau of Transportation Statistics (BTS), National
Transportation Atlas Database (NTAD), "National Transportation Noise Map".
Metric: LAeq 24h (A-weighted equivalent continuous sound level), dB.
Raster: GeoTIFF, float32, 30 m grid, nodata = -3.4028235e+38.
        Values are masked below ~45 dB (only transportation noise >= 45 dB
        near roads is modeled), so only pixels near roads carry data.
License: Public domain. "This NTAD dataset is a work of the United States
        government as defined in 17 U.S.C. 101 and as such are not protected
        by any U.S. copyrights. This work is available for unrestricted public
        use." (BTS item fcd948117131499cb1289ddf6413b6d8). Commercial use OK;
        acknowledge BTS.

Two download channels (BTS WAF may block non-US / automated clients with 403):
  1. OFFICIAL  https://www.bts.gov/bts-net-storage/<name>.zip   (works in a
     real US browser; may 403 from datacenter / non-US IPs)
  2. MIRROR    GitHub ukdolls/BTS_2018NoiseMaps (public-domain re-host, 2018
     Alaska + Hawaii road/aviation only; no CONUS). Use for AK/HI validation
     when the official host blocks you.

This script DOES NOT download all-of-CONUS by default (CONUS road zip is large).
Default run validates a small AK/HI tile end to end so the pipeline is proven.

Usage:
    python scripts/download_us_noise.py --list
    python scripts/download_us_noise.py --validate          # tiny HI tile, mirror
    python scripts/download_us_noise.py --get hawaii_road_2018
    python scripts/download_us_noise.py --get conus_road_2020   # official, large
"""
from __future__ import annotations

import argparse
import os
import sys
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "us"

# Browser-like headers; BTS edge (Akamai) rejects bare clients.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.bts.gov/geospatial/national-transportation-noise-map",
}

OFFICIAL_BASE = "https://www.bts.gov/bts-net-storage"
MIRROR_RAW = "https://raw.githubusercontent.com/ukdolls/BTS_2018NoiseMaps/main"

# key -> (official zip basename, mirror filename or None, expected tif suffix)
CATALOG = {
    # --- CONUS road (the real prize; large; official only) ---
    "conus_road_2020": ("CONUS_road_noise_2020", None),
    "conus_road_2018": ("CONUS_road_noise_2018", None),
    "conus_road_2016": ("CONUS_road_noise_2016", None),
    # --- Hawaii road (small; both channels) ---
    "hawaii_road_2020": ("Hawaii_road_noise_2020", None),
    "hawaii_road_2018": ("Hawaii_road_noise_2018", "Hawaii_road_noise_2018.zip"),
    # --- Alaska road (medium; both channels) ---
    "alaska_road_2020": ("Alaska_road_noise_2020", None),
    "alaska_road_2018": ("Alaska_road_noise_2018", "Alaska_road_noise_2018.zip"),
}


def _download(url: str, dest: Path, max_bytes: int | None = None) -> Path:
    print(f"GET {url}")
    req = Request(url, headers=HEADERS)
    try:
        with urlopen(req, timeout=120) as r, open(dest, "wb") as f:
            total = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                total += len(chunk)
                if max_bytes and total >= max_bytes:
                    print(f"  (stopped at {total/1e6:.1f} MB cap)")
                    break
            print(f"  -> {dest} ({total/1e6:.1f} MB)")
    except HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} from {url} ({e.reason})") from e
    except URLError as e:
        raise RuntimeError(f"network error for {url}: {e.reason}") from e
    return dest


def fetch(key: str, prefer_mirror: bool = False) -> Path:
    """Download the zip for `key`, trying official first (or mirror first)."""
    if key not in CATALOG:
        raise SystemExit(f"unknown key {key!r}; see --list")
    base, mirror = CATALOG[key]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    dest = DATA_DIR / f"{base}.zip"
    if dest.exists() and dest.stat().st_size > 1000:
        print(f"already have {dest}")
        return dest

    channels = []
    official_url = f"{OFFICIAL_BASE}/{base}.zip"
    mirror_url = f"{MIRROR_RAW}/{mirror}" if mirror else None
    if prefer_mirror and mirror_url:
        channels = [("mirror", mirror_url), ("official", official_url)]
    else:
        channels = [("official", official_url)]
        if mirror_url:
            channels.append(("mirror", mirror_url))

    last_err = None
    for name, url in channels:
        try:
            return _download(url, dest)
        except RuntimeError as e:
            last_err = e
            print(f"  {name} failed: {e}")
    raise SystemExit(
        f"all channels failed for {key}. last error: {last_err}\n"
        f"  If BTS returns 403, the edge WAF is blocking this IP. Open the "
        f"official URL in a US browser, or email ntad@dot.gov.\n"
        f"  official: {official_url}"
    )


def unzip(zip_path: Path) -> list[Path]:
    out = zip_path.with_suffix("")
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(zip_path.parent)
        names = z.namelist()
    tifs = sorted(zip_path.parent.glob("**/*.tif"))
    print(f"extracted {len(names)} entries; tif files: {[str(t) for t in tifs]}")
    return tifs


def validate(tif: Path) -> None:
    os.environ.setdefault(
        "PROJ_LIB",
        str(Path(__import__("rasterio").__file__).parent / "proj_data"),
    )
    import numpy as np
    import rasterio
    from rasterio.windows import Window

    with rasterio.open(tif) as ds:
        print(f"\n== {tif.name} ==")
        print(f"  driver={ds.driver} size={ds.width}x{ds.height} "
              f"bands={ds.count} dtype={ds.dtypes[0]}")
        print(f"  crs={ds.crs} res={ds.res} nodata={ds.nodata}")
        print(f"  bounds={ds.bounds}")
        # scan windows for valid (near-road) pixels without loading huge rasters
        found = 0
        for ro in range(0, ds.height, 4000):
            for co in range(0, ds.width, 4000):
                h = min(4000, ds.height - ro)
                w = min(4000, ds.width - co)
                a = ds.read(1, window=Window(co, ro, w, h))
                v = a[a != ds.nodata]
                v = v[np.isfinite(v)]
                if v.size > 200:
                    print(f"  win(row{ro},col{co}) valid={v.size} "
                          f"min={v.min():.2f} median={np.median(v):.2f} "
                          f"max={v.max():.2f} dB")
                    found += 1
                    if found >= 3:
                        return
    if not found:
        print("  WARNING: no valid pixels found in sampled windows")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="list catalog keys")
    ap.add_argument("--get", metavar="KEY", help="download + extract + validate")
    ap.add_argument("--validate", action="store_true",
                    help="tiny end-to-end check: HI road 2018 via mirror")
    ap.add_argument("--prefer-mirror", action="store_true",
                    help="try GitHub mirror before official BTS host")
    args = ap.parse_args()

    if args.list:
        print("available keys:")
        for k, (base, mirror) in CATALOG.items():
            ch = "official+mirror" if mirror else "official only"
            print(f"  {k:18s} {base}.zip  [{ch}]")
        return

    if args.validate:
        z = fetch("hawaii_road_2018", prefer_mirror=True)
        for t in unzip(z):
            validate(t)
        return

    if args.get:
        z = fetch(args.get, prefer_mirror=args.prefer_mirror)
        for t in unzip(z):
            validate(t)
        return

    ap.print_help()


if __name__ == "__main__":
    main()
