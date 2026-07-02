"""下载澳洲夏季 MODIS 11A2 LST(Day+Night)建本地 mosaic, 供 heat_island 本地采样。

对每个覆盖澳洲的 MODIS sinusoidal tile, 取多个夏季 8-day composite 的
LST_Day_1km / LST_Night_1km, mask 无效像元, 跨 composite 求均值转 °C,
day / night 各写一张本地 GeoTIFF(保留原生 sinusoidal CRS), gdalbuildvrt 建
data/global/modis_lst_day.vrt + modis_lst_night.vrt。

采样端复用 property_scores.common.landcover.sampler(= noise.raster_sample):
sample() 会自动把 lat/lng 重投影到栅格的 sinusoidal CRS, 所以无需 warp,
和 DEM(dem.vrt)/ WorldCover(lc.vrt)完全一致的本地 tile + VRT 模式。

用法:
  python scripts/download_modis_lst.py --dry-run                       # 只搜索统计, 不下载
  python scripts/download_modis_lst.py --tiles 29,12 --max-composites 3 # 单 tile 小测
  python scripts/download_modis_lst.py                                 # 全澳全量(默认 2023 夏季)
  python scripts/download_modis_lst.py --seasons 2022,2023,2024        # 多夏季均值(更稳)
"""
import argparse
import os
import subprocess
import time
import warnings
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import rasterio
import requests

PC_STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
PC_SIGN = "https://planetarycomputer.microsoft.com/api/sas/v1/sign"
OUT_DIR = "data/global/modis_lst"
DAY_VRT = "data/global/modis_lst_day.vrt"
NIGHT_VRT = "data/global/modis_lst_night.vrt"
AU_BBOX = [112, -44, 154, -9]
VALID_DN_MIN = 7500   # MODIS LST 有效 DN 下限(< 此为 fill / 无效低值)
SCALE = 0.02          # DN -> Kelvin
NODATA = -9999.0

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "limon-heat-local/1.0"})
_sign_cache: dict[str, tuple[str, float]] = {}


def stac_search_all(seasons):
    """搜索所有夏季 composite(POST 分页), 按 (h,v) tile 分组返回。"""
    by_tile = defaultdict(list)
    for yr in seasons:
        dt = f"{yr}-12-01/{yr + 1}-02-29"
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


def build_vrt(vrt_path, tile_paths):
    if not tile_paths:
        print(f"skip vrt {vrt_path}: no tiles")
        return
    try:
        subprocess.run(["gdalbuildvrt", "-srcnodata", str(NODATA),
                        "-vrtnodata", str(NODATA), vrt_path] + tile_paths,
                       check=True, capture_output=True)
        print(f"built {vrt_path} ({len(tile_paths)} tiles)")
    except Exception as e:
        print(f"vrt fail {vrt_path}: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", default="2023",
                    help="逗号分隔的南半球夏季起始年(默认 2023 = 2023-12~2024-02)")
    ap.add_argument("--tiles", default="",
                    help="限定 tile, 如 '29,12' 或 '29,12;30,11'(测试用)")
    ap.add_argument("--max-composites", type=int, default=0,
                    help="每 tile 最多用几期 composite(0=全部)")
    ap.add_argument("--skip-existing", action="store_true",
                    help="已存在的 tile tif 跳过(断点续下)")
    ap.add_argument("--stat", choices=["median", "mean"], default="median",
                    help="逐像元聚合方式(默认 median, 抗热带云污染)")
    ap.add_argument("--dry-run", action="store_true", help="只搜索统计不下载")
    args = ap.parse_args()

    seasons = [int(s) for s in args.seasons.split(",") if s.strip()]
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

    os.makedirs(OUT_DIR, exist_ok=True)
    day_tiles, night_tiles = [], []
    t0 = time.time()
    for i, ((h, v), its) in enumerate(sorted(by_tile.items()), 1):
        tag = f"h{h:02d}v{v:02d}"
        dp, npth = f"{OUT_DIR}/modis_day_{tag}.tif", f"{OUT_DIR}/modis_night_{tag}.tif"

        if args.skip_existing and os.path.exists(dp) and os.path.exists(npth):
            day_tiles.append(dp)
            night_tiles.append(npth)
            print(f"  [{i}/{len(by_tile)}] {tag} skip(existing)", flush=True)
            continue

        day_hrefs = [it["assets"]["LST_Day_1km"]["href"] for it in its
                     if it.get("assets", {}).get("LST_Day_1km")]
        night_hrefs = [it["assets"]["LST_Night_1km"]["href"] for it in its
                       if it.get("assets", {}).get("LST_Night_1km")]

        day_arr, meta = agg_stack(day_hrefs, args.max_composites, stat=args.stat)
        if day_arr is not None:
            write_tile(dp, day_arr, meta)
            day_tiles.append(dp)

        night_arr, nmeta = agg_stack(night_hrefs, args.max_composites, stat=args.stat)
        if night_arr is not None:
            write_tile(npth, night_arr, nmeta)
            night_tiles.append(npth)

        print(f"  [{i}/{len(by_tile)}] {tag} done ({time.time() - t0:.0f}s)", flush=True)

    build_vrt(DAY_VRT, sorted(day_tiles))
    build_vrt(NIGHT_VRT, sorted(night_tiles))
    print(f"\n完成, 总耗时 {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
