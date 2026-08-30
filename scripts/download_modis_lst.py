"""下载澳洲夏季 MODIS 11A2 LST(Day+Night)建本地 mosaic, 供 heat_island 本地采样。

对每个覆盖澳洲的 MODIS sinusoidal tile, 取多个夏季 8-day composite 的
LST_Day_1km / LST_Night_1km, mask 无效像元, 跨 composite 求均值转 °C。
每次刷新都在独立 generation 目录构建完整 day/night tiles、VRT 与 manifest，
验证后只原子切换 data/global/modis_lst_current 这一个 symlink。

采样端复用 property_scores.common.landcover.sampler(= noise.raster_sample):
sample() 会自动把 lat/lng 重投影到栅格的 sinusoidal CRS, 所以无需 warp,
和 DEM(dem.vrt)/ WorldCover(lc.vrt)完全一致的本地 tile + VRT 模式。

用法:
  python scripts/download_modis_lst.py --dry-run                       # 只搜索统计, 不下载
  python scripts/download_modis_lst.py --tiles 29,12 --max-composites 3 # 单 tile 小测
  python scripts/download_modis_lst.py                                 # 全澳最近三个完整夏季
  python scripts/download_modis_lst.py --seasons 2022,2023,2024        # 多夏季均值(更稳)
"""
import argparse
import calendar
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
import warnings
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

import numpy as np
import rasterio
import requests

PC_STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
PC_SIGN = "https://planetarycomputer.microsoft.com/api/sas/v1/sign"
RELEASES_DIR = "data/global/modis_lst_releases"
ACTIVE_LINK = "data/global/modis_lst_current"
TILES_DIRNAME = "tiles"
DAY_VRT_NAME = "modis_lst_day.vrt"
NIGHT_VRT_NAME = "modis_lst_night.vrt"
METADATA_NAME = "modis_lst_metadata.json"
AU_BBOX = [112, -44, 154, -9]
VALID_DN_MIN = 7500   # MODIS LST 有效 DN 下限(< 此为 fill / 无效低值)
SCALE = 0.02          # DN -> Kelvin
NODATA = -9999.0

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "limon-heat-local/1.0"})
_sign_cache: dict[str, tuple[str, float]] = {}


def season_datetime_range(year: int) -> str:
    """Return a valid Dec-Feb STAC interval for leap and common years."""
    february_last_day = calendar.monthrange(year + 1, 2)[1]
    return f"{year}-12-01/{year + 1}-02-{february_last_day:02d}"


def stac_search_all(seasons):
    """搜索所有夏季 composite(POST 分页), 按 (h,v) tile 分组返回。"""
    by_tile = defaultdict(list)
    for yr in seasons:
        dt = season_datetime_range(yr)
        body = {"collections": ["modis-11A2-061"], "bbox": AU_BBOX,
                "datetime": dt, "limit": 500}
        url = f"{PC_STAC}/search"
        page = 0
        while True:
            r = SESSION.post(url, json=body, timeout=60)
            r.raise_for_status()
            js = r.json()
            for it in js.get("features", []):
                p = it.get("properties", {})
                h = p.get("modis:horizontal-tile")
                v = p.get("modis:vertical-tile")
                if h is None or v is None:
                    continue
                by_tile[(h, v)].append(it)
            page += 1
            nxt = next((l for l in js.get("links", []) if l.get("rel") == "next"), None)
            if not nxt:
                break
            url = nxt.get("href", url)
            body = nxt.get("body") or body
        print(f"  season {yr}-{yr + 1}: {page} page(s)")
    return by_tile


def sign(href):
    """PC SAS 签名(带缓存 + 重试)。"""
    now = time.time()
    if href in _sign_cache:
        u, ts = _sign_cache[href]
        if now - ts < 3000:
            return u
    for attempt in range(4):
        try:
            r = SESSION.get(PC_SIGN, params={"href": href}, timeout=20)
            if r.ok:
                u = r.json().get("href")
                _sign_cache[href] = (u, now)
                return u
        except requests.RequestException:
            pass
        time.sleep(2 ** attempt)
    return None


def read_lst_c(href):
    """signed COG -> °C float32 array(无效为 nan)及 meta(crs, transform, w, h)。"""
    signed = sign(href)
    if not signed:
        return None, None
    try:
        with rasterio.open(signed) as ds:
            raw = ds.read(1)
            arr = raw.astype("float32")
            arr[raw < VALID_DN_MIN] = np.nan
            arr = arr * SCALE - 273.15
            meta = (ds.crs, ds.transform, ds.width, ds.height)
        return arr, meta
    except Exception as e:
        print(f"    read fail: {str(e)[:60]}")
        return None, None


def agg_stack(hrefs, max_comp, workers=8, stat="median"):
    """多期 href 逐像元聚合(忽略 nan)-> array + meta。

    stat="median"(默认): 逐像元中位数, 抗热带雨季云污染。MODIS 8-day LST 在
    热带(Darwin/Cairns)Dec-Feb 有大量云污染的低估期, 均值被拉低(Darwin 点
    11 期 mean 33.5 vs median 35.7 ≈ 远程晴天 35.3); QC mandatory-good 在这些
    点 N=0 不可用, median 是最鲁棒的修法, 对温带点(云少、分布紧凑)≈均值。
    stat="mean": 逐像元均值(旧行为)。

    并行拉取: 每期是独立 sign + 远程 COG open + read(纯网络 IO); ThreadPoolExecutor
    开 workers 路, 单 tile 从 ~250s 压到 ~50s。ex.map 保序返回, meta 取首个有效期。
    """
    use = hrefs[:max_comp] if max_comp else hrefs
    stack = []
    meta = None
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for arr, m in ex.map(read_lst_c, use):
            if arr is None:
                continue
            if meta is None:
                meta = m
            stack.append(arr)
    if not stack or meta is None:
        return None, None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)  # 全 nan 列
        agg = np.nanmedian(stack, axis=0) if stat == "median" else np.nanmean(stack, axis=0)
    agg = np.where(np.isnan(agg), NODATA, agg).astype("float32")
    return agg, meta


def write_tile(path, arr, meta):
    crs, transform, w, h = meta
    prof = {
        "driver": "GTiff", "height": h, "width": w, "count": 1,
        "dtype": "float32", "crs": crs, "transform": transform,
        "nodata": NODATA, "compress": "deflate", "tiled": True,
    }
    with rasterio.open(path, "w", **prof) as dst:
        dst.write(arr, 1)


def build_vrt(vrt_path, tile_paths, *, cwd=None):
    if not tile_paths:
        print(f"skip vrt {vrt_path}: no tiles")
        return False
    try:
        subprocess.run(["gdalbuildvrt", "-srcnodata", str(NODATA),
                        "-vrtnodata", str(NODATA), vrt_path] + tile_paths,
                       check=True, capture_output=True, cwd=cwd)
        print(f"built {vrt_path} ({len(tile_paths)} tiles)")
        return True
    except Exception as e:
        print(f"vrt fail {vrt_path}: {e}")
        return False


def default_seasons(today=None):
    """The three most recent *completed* southern-hemisphere summers."""
    today = today or date.today()
    last_start = today.year - 1 if today.month >= 3 else today.year - 2
    return [last_start - 2, last_start - 1, last_start]


def write_metadata(path, *, release_id, seasons, stat, tile_count,
                   composite_count):
    """Publish vintage only after both VRTs completed, using atomic replace."""
    payload = {
        "collection": "modis-11A2-061",
        "seasons": seasons,
        "period_start": f"{min(seasons)}-12-01",
        "period_end": (
            f"{max(seasons) + 1}-02-"
            f"{calendar.monthrange(max(seasons) + 1, 2)[1]:02d}"),
        "stat": stat,
        "native_grid_step_m": 926.625,
        "tile_count": tile_count,
        "composite_count": composite_count,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "release_id": release_id,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    print(f"wrote {path}")


def validate_tile_sets(expected_tags, day_tags, night_tags):
    """Require the same complete tile identity set on both sides."""
    expected = set(expected_tags)
    day = set(day_tags)
    night = set(night_tags)
    if not expected or day != expected or night != expected:
        raise RuntimeError(
            "day/night mosaic incomplete; active generation was not changed "
            f"(expected={sorted(expected)}, day={sorted(day)}, "
            f"night={sorted(night)})")


def publish_release(stage_dir, release_id):
    """Rename a verified generation, then atomically switch one symlink."""
    os.makedirs(RELEASES_DIR, exist_ok=True)
    final_dir = os.path.join(RELEASES_DIR, release_id)
    if os.path.exists(final_dir):
        raise RuntimeError(f"release already exists: {final_dir}")
    if os.path.lexists(ACTIVE_LINK) and not os.path.islink(ACTIVE_LINK):
        raise RuntimeError(
            f"refusing to replace non-symlink active path: {ACTIVE_LINK}")

    os.replace(stage_dir, final_dir)
    active_parent = os.path.dirname(ACTIVE_LINK) or "."
    os.makedirs(active_parent, exist_ok=True)
    temp_link = (
        f"{ACTIVE_LINK}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    target = os.path.relpath(final_dir, active_parent)
    try:
        os.symlink(target, temp_link)
        os.replace(temp_link, ACTIVE_LINK)
    finally:
        if os.path.lexists(temp_link):
            os.unlink(temp_link)
    print(f"activated {ACTIVE_LINK} -> {target}")
    return final_dir


def build_parser():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", default="",
                    help=("逗号分隔的南半球夏季起始年;"
                          "默认自动取最近三个完整夏季"))
    ap.add_argument("--tiles", default="",
                    help="限定 tile, 如 '29,12' 或 '29,12;30,11'(测试用)")
    ap.add_argument("--max-composites", type=int, default=0,
                    help="每 tile 最多用几期 composite(0=全部)")
    ap.add_argument("--stat", choices=["median", "mean"], default="median",
                    help="逐像元聚合方式(默认 median, 抗热带云污染)")
    ap.add_argument("--dry-run", action="store_true", help="只搜索统计不下载")
    return ap


def main():
    args = build_parser().parse_args()

    seasons = ([int(s) for s in args.seasons.split(",") if s.strip()]
               if args.seasons.strip() else default_seasons())
    only = set()
    if args.tiles:
        for t in args.tiles.replace(";", " ").split():
            h, v = t.split(",")
            only.add((int(h), int(v)))

    print(f"STAC 搜索 seasons={seasons} ...")
    by_tile = stac_search_all(seasons)
    print(f"覆盖 {len(by_tile)} 个 tile, 共 {sum(len(v) for v in by_tile.values())} composites")

    if only:
        by_tile = {k: v for k, v in by_tile.items() if k in only}
        print(f"限定 {sorted(by_tile.keys())}")

    if args.dry_run:
        for (h, v), its in sorted(by_tile.items()):
            print(f"  h{h:02d}v{v:02d}: {len(its)} composites")
        return

    os.makedirs(RELEASES_DIR, exist_ok=True)
    stage_dir = tempfile.mkdtemp(prefix=".staging-", dir=RELEASES_DIR)
    tiles_dir = os.path.join(stage_dir, TILES_DIRNAME)
    os.makedirs(tiles_dir)
    day_tiles: dict[str, str] = {}
    night_tiles: dict[str, str] = {}
    t0 = time.time()
    release_id = (
        f"summer-{'-'.join(str(year) for year in seasons)}-{args.stat}-"
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-"
        f"{uuid.uuid4().hex[:8]}")
    try:
        for i, ((h, v), its) in enumerate(sorted(by_tile.items()), 1):
            tag = f"h{h:02d}v{v:02d}"
            day_rel = f"{TILES_DIRNAME}/modis_day_{tag}.tif"
            night_rel = f"{TILES_DIRNAME}/modis_night_{tag}.tif"

            day_hrefs = [it["assets"]["LST_Day_1km"]["href"] for it in its
                         if it.get("assets", {}).get("LST_Day_1km")]
            night_hrefs = [it["assets"]["LST_Night_1km"]["href"] for it in its
                           if it.get("assets", {}).get("LST_Night_1km")]

            day_arr, meta = agg_stack(
                day_hrefs, args.max_composites, stat=args.stat)
            night_arr, nmeta = agg_stack(
                night_hrefs, args.max_composites, stat=args.stat)
            if day_arr is not None and night_arr is not None:
                write_tile(os.path.join(stage_dir, day_rel), day_arr, meta)
                write_tile(os.path.join(stage_dir, night_rel), night_arr, nmeta)
                day_tiles[tag] = day_rel
                night_tiles[tag] = night_rel
            else:
                print(f"  [{i}/{len(by_tile)}] {tag} incomplete", flush=True)

            print(
                f"  [{i}/{len(by_tile)}] {tag} done "
                f"({time.time() - t0:.0f}s)", flush=True)

        expected_tags = {
            f"h{h:02d}v{v:02d}" for h, v in by_tile
        }
        validate_tile_sets(expected_tags, day_tiles, night_tiles)
        day_ok = build_vrt(
            DAY_VRT_NAME, sorted(day_tiles.values()), cwd=stage_dir)
        night_ok = build_vrt(
            NIGHT_VRT_NAME, sorted(night_tiles.values()), cwd=stage_dir)
        if not (day_ok and night_ok):
            raise RuntimeError(
                "day/night VRT build failed; active generation was not changed")
        write_metadata(
            os.path.join(stage_dir, METADATA_NAME),
            release_id=release_id,
            seasons=seasons,
            stat=args.stat,
            tile_count=len(day_tiles),
            composite_count=sum(len(items) for items in by_tile.values()),
        )
        publish_release(stage_dir, release_id)
        stage_dir = None
        print(f"\n完成, 总耗时 {time.time() - t0:.0f}s")
    finally:
        # A failed generation was created by this invocation and was never
        # activated. Removing only that exact mkdtemp path cannot touch the
        # live pointer or a prior verified release.
        if stage_dir and os.path.isdir(stage_dir):
            shutil.rmtree(stage_dir)


if __name__ == "__main__":
    main()
