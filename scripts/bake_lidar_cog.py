#!/usr/bin/env python3
"""Bake GA national 5 m LiDAR DEM (per-UTM-zone S3 mosaics) into local Int16
decimetre COGs for the flood HAND read.

Source (2026-07-16, verified): GA "5 Metre DEM of Australia derived from LiDAR"
distributes per-UTM-zone mosaics as PUBLIC S3 zips (no ELVIS order flow, no AWS
signing). Each zip holds Float32 5 m GeoTIFF(s) in GDA94/MGA. eCat record
22be4b55-2465-4320-e053-10a3070a5236 / service DEM_LiDAR_5m_2025.

Per zone, disk-safe (see the national-lidar-bake-handoff.md §2 resource guard):
  1. download zip (resumable) into WORK
  2. list .tif members via zipfile (reads central dir only — no full extract)
  3. gdalbuildvrt over /vsizip/<zip>/<tif> paths (references inside the zip)
  4. gdalwarp -> EPSG:4326 @ 5 m, resample average, streamed from the zip,
     written straight to a DEFLATE-compressed Float32 GTiff (sparse; never a
     ~60 GB uncompressed grid)
  5. gdal_calc -> Int16 decimetres (x10), nodata -32768
  6. gdal_translate -of COG (DEFLATE, PREDICTOR=2)  -> data/global/lidar/<zone>_5m.tif
  7. delete zip + intermediates; print df / du
Finally gdalbuildvrt data/global/lidar/au_lidar_5m.vrt over all *_5m.tif.

Int16 decimetres (x10): AU max Kosciuszko 2228 m -> 22280 < 32767 (safe); nodata
-> -32768. LiDAR vertical precision is ~0.1-0.3 m, so decimetres keep all real
precision at 2.9x less storage than Float32.

Usage:
  python scripts/bake_lidar_cog.py <zone> [<zone> ...]   # e.g. ntz52
  python scripts/bake_lidar_cog.py --all                 # mainland AHD set
  python scripts/bake_lidar_cog.py --vrt-only            # just rebuild the VRT
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

BASE = ("https://elevation-direct-downloads.s3-ap-southeast-2.amazonaws.com/"
        "5m-dem/national_utm_mosaics")

# Mainland AHD/Ausgeoid set (skip _qg MDB duplicates + offshore islands; see
# handoff §0z). Sizes are the verified zip Content-Length for reference.
ZONES_MAIN = ["waz50", "waz51", "ntz52", "ntz53",
              "nationalz54ag", "nationalz55_ag", "nationalz56_ag"]

TR = "0.0000449"                     # ~5 m in degrees
SRC_NODATA = "-3.402823466e+38"      # Float32 min (observed GA nodata)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(REPO, "data", "global", "lidar")
WORK = os.path.join(REPO, "data", "global", "lidar", "_work")

# gdal_calc.py (system osgeo, compiled for NumPy 1.x) crashes if a NumPy 2.x in
# ~/.local shadows the system one (Oracle has numpy 2.4.4 there). Ignoring the
# user site-packages for our gdal subprocesses makes gdal_calc use the matching
# system numpy. Inherited by subprocess.run; harmless to the C++ gdal tools.
os.environ["PYTHONNOUSERSITE"] = "1"
# Records each baked zone's source zip Content-Length so --check can detect a GA
# re-release (the national 5 m mosaic is otherwise static since 2015).
MANIFEST = os.path.join(OUT_DIR, "manifest.json")

# --unzip: extract each zone's tif to disk before warping instead of streaming
# through /vsizip. REQUIRED for the big zones (z54/z55/z56): their tif is stored
# DEFLATE-compressed inside the zip and expands to hundreds of GB uncompressed
# (z55 = 137974x641646 = 88.5e9 px = ~354 GB). gdalwarp reading that through
# /vsizip must re-inflate the whole stream for every random block read and
# effectively stalls (verified on Mac AND Oracle). From a LOCAL uncompressed tif
# the same warp does cheap block seeks and skips nodata, finishing in minutes.
# Needs a big scratch disk (>=500 GB for z55: 354 GB tif + ~10 GB warp). Set by
# --unzip; leave off only for the small zones on a small disk.
UNZIP = False

# Force a clean PROJ data dir — the EclipseSUMO framework's stale proj.db
# (LAYOUT.VERSION.MINOR=4) otherwise hijacks the gdal CLI and breaks EPSG:4326.
# Prefer homebrew's (where the gdal CLI lives); fall back to rasterio's bundle.
def _force_proj():
    for cand in ("/opt/homebrew/share/proj",):
        if os.path.isfile(os.path.join(cand, "proj.db")):
            os.environ["PROJ_LIB"] = cand
            os.environ["PROJ_DATA"] = cand
            return
    try:
        import rasterio  # noqa
        p = os.path.join(os.path.dirname(rasterio.__file__), "proj_data")
        if os.path.isfile(os.path.join(p, "proj.db")):
            os.environ["PROJ_LIB"] = p
            os.environ["PROJ_DATA"] = p
    except Exception:
        pass


_force_proj()


def run(cmd):
    print("  $", " ".join(cmd), flush=True)
    t = time.time()
    r = subprocess.run(cmd)
    if r.returncode != 0:
        raise SystemExit(f"command failed ({r.returncode}): {' '.join(cmd)}")
    print(f"    ({time.time() - t:.0f}s)", flush=True)


def du(path):
    """Human-readable size of a file (portable; no `du`)."""
    try:
        n = os.path.getsize(path)
    except OSError:
        return "0"
    for u in ("B", "K", "M", "G", "T"):
        if n < 1024 or u == "T":
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}T"


def df_free():
    """Free / total on the repo's volume (portable; no `df`)."""
    u = shutil.disk_usage(REPO)
    return f"{u.free/1e9:.0f}G free / {u.total/1e9:.0f}G"


def free_gb():
    return shutil.disk_usage(REPO).free / 1e9


def load_manifest():
    try:
        with open(MANIFEST) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_manifest(m):
    tmp = MANIFEST + ".tmp"
    with open(tmp, "w") as f:
        json.dump(m, f, indent=2, sort_keys=True)
    os.replace(tmp, MANIFEST)


def head_size(zone):
    """Current Content-Length of the zone's S3 zip (bytes), or None on failure."""
    req = urllib.request.Request(f"{BASE}/{zone}.zip", method="HEAD",
                                 headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return int(r.headers.get("Content-Length") or 0) or None
    except Exception:
        return None


def download(zone, zip_path):
    url = f"{BASE}/{zone}.zip"
    print(f"  download {url}", flush=True)
    run(["curl", "-fL", "-C", "-", "--retry", "3", "-H", f"User-Agent: {UA}",
         "-o", zip_path, url])
    print(f"    zip size: {du(zip_path)}", flush=True)


def tif_members(zip_path):
    with zipfile.ZipFile(zip_path) as z:
        return [n for n in z.namelist() if n.lower().endswith(".tif")]


def bake_zone(zone):
    os.makedirs(WORK, exist_ok=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    zip_path = os.path.join(WORK, f"{zone}.zip")
    src_vrt = os.path.join(WORK, f"{zone}_src.vrt")
    warp_f = os.path.join(WORK, f"{zone}_f.tif")
    i16 = os.path.join(WORK, f"{zone}_i16.tif")
    out = os.path.join(OUT_DIR, f"{zone}_5m.tif")

    print(f"\n=== {zone} ===  (free before: {df_free()})", flush=True)
    t0 = time.time()

    if not os.path.exists(zip_path):
        download(zone, zip_path)
    zip_bytes = os.path.getsize(zip_path)
    members = tif_members(zip_path)
    if not members:
        raise SystemExit(f"{zone}: no .tif in zip")
    print(f"  {len(members)} tif member(s): {members[:3]}"
          f"{' ...' if len(members) > 3 else ''}", flush=True)

    raw_tifs = []
    if UNZIP:
        # Extract the tif(s) to disk, then drop the zip — a local uncompressed
        # tif lets gdalwarp seek/skip blocks cheaply (see UNZIP note).
        print(f"  unzip -> disk (free: {df_free()}) ...", flush=True)
        t = time.time()
        with zipfile.ZipFile(zip_path) as z:
            for m in members:
                z.extract(m, WORK)
                raw_tifs.append(os.path.join(WORK, m))
        print(f"    extracted {sum(os.path.getsize(p) for p in raw_tifs)/1e9:.0f}G"
              f" in {time.time()-t:.0f}s (free: {df_free()})", flush=True)
        try:
            os.remove(zip_path)
        except OSError:
            pass
        src_inputs = raw_tifs
    else:
        src_inputs = [f"/vsizip/{zip_path}/{m}" for m in members]

    run(["gdalbuildvrt", "-q", src_vrt, *src_inputs])
    # -r near: source and target are both 5 m, so this is a reprojection not a
    # downsample — nearest preserves the real bare-earth LiDAR value (no
    # smoothing) and is faster than average. The slow part is decompressing the
    # source (the zip's tif is uncompressed internally; a sparse zone expands to
    # tens of GB), which no resampler avoids; gdalwarp at least skips nodata
    # output blocks. The x10/Int16 step runs on the compact warped file, so it
    # stays fast.
    run(["gdalwarp", "-q", "-t_srs", "EPSG:4326", "-tr", TR, TR, "-r", "near",
         "-srcnodata", SRC_NODATA, "-dstnodata", "-9999",
         "-of", "GTiff", "-co", "COMPRESS=DEFLATE", "-co", "TILED=YES",
         "-co", "BIGTIFF=YES", "-wo", "NUM_THREADS=ALL_CPUS",
         "--config", "GDAL_CACHEMAX", "2048",
         "-multi", "-overwrite", src_vrt, warp_f])
    print(f"    warped float: {du(warp_f)}", flush=True)
    # zip / raw tifs / src VRT are only needed by the warp; drop them now so the
    # peak (raw + warped + int16 + cog coexisting) never overflows the disk.
    for f in (zip_path, src_vrt, *raw_tifs):
        try:
            os.remove(f)
        except OSError:
            pass

    run(["gdal_calc.py", "-A", warp_f,
         "--outfile", i16, "--calc", "where(A>-9000,rint(A*10),-32768)",
         "--NoDataValue", "-32768", "--type", "Int16", "--quiet",
         "--co", "COMPRESS=DEFLATE", "--co", "PREDICTOR=2", "--co", "BIGTIFF=YES"])
    print(f"    int16: {du(i16)}", flush=True)

    run(["gdal_translate", "-q", "-of", "COG", "-co", "COMPRESS=DEFLATE",
         "-co", "PREDICTOR=2", "-co", "BIGTIFF=YES",
         "-co", "NUM_THREADS=ALL_CPUS", i16, out])

    for f in (warp_f, i16):
        try:
            os.remove(f)
        except OSError:
            pass
    m = load_manifest()
    m[zone] = {"zip_bytes": zip_bytes, "tif": os.path.basename(out)}
    save_manifest(m)
    print(f"  -> {out}  ({du(out)})  in {time.time() - t0:.0f}s"
          f"  (free after: {df_free()})", flush=True)


def build_vrt():
    tifs = sorted(f for f in os.listdir(OUT_DIR) if f.endswith("_5m.tif")) \
        if os.path.isdir(OUT_DIR) else []
    if not tifs:
        print("no baked tiles yet; skip VRT")
        return
    # Write RELATIVE tile paths (basenames, cwd=OUT_DIR) so the VRT is portable:
    # rsync data/global/lidar/ to Oracle and it resolves as-is (no Mac paths baked
    # in). gdalbuildvrt records paths exactly as given, so run it inside OUT_DIR.
    print("  $ gdalbuildvrt -q au_lidar_5m.vrt *_5m.tif  (cwd=%s)" % OUT_DIR,
          flush=True)
    r = subprocess.run(["gdalbuildvrt", "-q", "au_lidar_5m.vrt", *tifs],
                       cwd=OUT_DIR)
    if r.returncode != 0:
        raise SystemExit("gdalbuildvrt failed")
    print(f"VRT: {os.path.join(OUT_DIR, 'au_lidar_5m.vrt')}  ({len(tifs)} tiles)")


def check(apply):
    """Compare each mainland zone's live S3 zip size to the manifest; report
    zones GA has re-released (or never baked). With apply=True, re-bake them and
    rebuild the VRT. This is the quarterly-cron entry point (the national 5 m
    mosaic is static since 2015, so normally nothing changes and this no-ops)."""
    m = load_manifest()
    changed, missing_head = [], []
    for z in ZONES_MAIN:
        live = head_size(z)
        if live is None:
            missing_head.append(z)
            print(f"  {z}: HEAD failed (skip)")
            continue
        was = (m.get(z) or {}).get("zip_bytes")
        baked = os.path.exists(os.path.join(OUT_DIR, f"{z}_5m.tif"))
        if not baked or was != live:
            changed.append(z)
            print(f"  {z}: CHANGED (baked={baked} manifest={was} live={live})")
        else:
            print(f"  {z}: up-to-date ({live} bytes)")
    if not changed:
        print("all zones up-to-date; nothing to do")
        return
    if not apply:
        print(f"\n{len(changed)} zone(s) need re-bake: {changed}  (run with --apply)")
        return
    print(f"\nre-baking {len(changed)} changed zone(s): {changed}")
    for z in changed:
        bake_zone(z)
    build_vrt()


def seed_manifest():
    """Backfill manifest.json from the current S3 zip sizes for zones already
    baked on disk. Use once after a bake that predates manifest support; right
    after a bake the baked tile == current release, so live size is the baseline."""
    m = load_manifest()
    for z in ZONES_MAIN:
        if os.path.exists(os.path.join(OUT_DIR, f"{z}_5m.tif")):
            live = head_size(z)
            if live:
                m[z] = {"zip_bytes": live, "tif": f"{z}_5m.tif"}
                print(f"  seeded {z}: {live} bytes")
    save_manifest(m)
    print(f"manifest: {MANIFEST}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("zones", nargs="*")
    ap.add_argument("--all", action="store_true", help="mainland AHD set")
    ap.add_argument("--vrt-only", action="store_true")
    ap.add_argument("--seed-manifest", action="store_true",
                    help="backfill manifest from S3 sizes for already-baked zones")
    ap.add_argument("--check", action="store_true",
                    help="compare S3 zip sizes to manifest (quarterly refresh)")
    ap.add_argument("--apply", action="store_true",
                    help="with --check: re-bake changed zones")
    ap.add_argument("--unzip", action="store_true",
                    help="extract tif to disk before warp (REQUIRED for big "
                         "zones; needs a big scratch disk, e.g. 500 GB)")
    a = ap.parse_args()
    global UNZIP
    UNZIP = a.unzip
    if a.vrt_only:
        build_vrt()
        return
    if a.seed_manifest:
        seed_manifest()
        return
    if a.check:
        check(a.apply)
        if os.path.isdir(WORK) and not os.listdir(WORK):
            shutil.rmtree(WORK, ignore_errors=True)
        return
    zones = ZONES_MAIN if a.all else a.zones
    if not zones:
        ap.error("give zone name(s), --all, or --check")
    for z in zones:
        bake_zone(z)
    build_vrt()
    if os.path.isdir(WORK) and not os.listdir(WORK):
        shutil.rmtree(WORK, ignore_errors=True)


if __name__ == "__main__":
    main()
