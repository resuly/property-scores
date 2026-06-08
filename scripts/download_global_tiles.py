"""Download Copernicus GLO-30 DEM (1deg) + ESA WorldCover (3deg) tiles covering
our NL training region + AU sample cities, into data/global/. Both native
EPSG:4326. Skips ocean/404 tiles. Builds simple file lists for VRT mosaicking.
"""
import math
import os
import subprocess
import urllib.request

OUT = "data/global"
os.makedirs(f"{OUT}/dem", exist_ok=True)
os.makedirs(f"{OUT}/lc", exist_ok=True)

# regions: (lat_min,lat_max,lng_min,lng_max)
REGIONS = [
    (50.6, 53.8, 3.1, 7.4),     # Netherlands
    (-38.3, -37.4, 144.5, 145.5),   # Melbourne
    (-34.2, -33.5, 150.8, 151.4),   # Sydney
    (-35.2, -34.7, 138.4, 138.8),   # Adelaide
    (-32.2, -31.7, 115.6, 116.1),   # Perth
    (-43.1, -42.7, 147.0, 147.5),   # Hobart
    (-35.5, -35.1, 149.0, 149.3),   # Canberra
    (-12.6, -12.3, 130.7, 131.1),   # Darwin
    # US training cities (+/-0.2deg bbox for road-noise transfer sampling)
    (33.85, 34.25, -118.45, -118.05),   # Los Angeles (34.05, -118.25)
    (40.51, 40.91, -74.21, -73.81),     # New York City (40.71, -74.01)
    (41.68, 42.08, -87.83, -87.43),     # Chicago (41.88, -87.63)
    (29.56, 29.96, -95.57, -95.17),     # Houston (29.76, -95.37)
    # === AU 全境人口覆盖 (2026-06-08, score.py transfer 部署前置; 沿海人口带+首府+主要regional, 跳无人中部荒漠) ===
    (-39.2, -33.5, 140.8, 151.5),   # VIC全 + NSW南内陆 (Melbourne-Geelong-Ballarat-Bendigo-Albury-Wagga-Canberra)
    (-34.8, -25.8, 150.3, 153.8),   # NSW海岸 + SEQ + Toowoomba (Sydney-Newcastle-Coffs-Byron-Brisbane-GoldCoast-Sunshine)
    (-38.7, -34.0, 138.0, 141.2),   # SA (Adelaide-MtGambier)
    (-43.7, -40.3, 144.8, 148.5),   # Tasmania (Hobart-Launceston)
    (-35.5, -31.0, 115.3, 117.3),   # WA SW (Perth-Mandurah-Bunbury)
    (-25.0, -16.5, 145.3, 153.6),   # QLD海岸 (Cairns-Townsville-Mackay-Rockhampton-Bundaberg-HerveyBay)
    (-12.9, -12.2, 130.6, 131.3),   # Darwin
]


def dem_tiles(regions):
    tiles = set()
    for la0, la1, lo0, lo1 in regions:
        for la in range(math.floor(la0), math.ceil(la1)):
            for lo in range(math.floor(lo0), math.ceil(lo1)):
                ns = "N" if la >= 0 else "S"
                ew = "E" if lo >= 0 else "W"
                t = f"Copernicus_DSM_COG_10_{ns}{abs(la):02d}_00_{ew}{abs(lo):03d}_00_DEM"
                tiles.add(t)
    return sorted(tiles)


def lc_tiles(regions):
    tiles = set()
    for la0, la1, lo0, lo1 in regions:
        for la in range(math.floor(la0 / 3) * 3, math.ceil(la1 / 3) * 3, 3):
            for lo in range(math.floor(lo0 / 3) * 3, math.ceil(lo1 / 3) * 3, 3):
                ns = "N" if la >= 0 else "S"
                ew = "E" if lo >= 0 else "W"
                tiles.add(f"ESA_WorldCover_10m_2021_v200_{ns}{abs(la):02d}{ew}{abs(lo):03d}_Map")
    return sorted(tiles)


def fetch(url, path):
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        return "cached"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "noise-poc"})
        with urllib.request.urlopen(req, timeout=120) as r, open(path, "wb") as f:
            f.write(r.read())
        return "ok"
    except Exception as e:  # noqa: BLE001
        if os.path.exists(path):
            os.remove(path)
        return f"skip ({str(e)[:40]})"


def main():
    dts = dem_tiles(REGIONS)
    print(f"DEM tiles: {len(dts)}")
    got = []
    for t in dts:
        url = f"https://copernicus-dem-30m.s3.amazonaws.com/{t}/{t}.tif"
        p = f"{OUT}/dem/{t}.tif"
        s = fetch(url, p)
        if s in ("ok", "cached"):
            got.append(p)
        print(f"  {t}: {s}", flush=True)
    open(f"{OUT}/dem_list.txt", "w").write("\n".join(got))

    lts = lc_tiles(REGIONS)
    print(f"LC tiles: {len(lts)}")
    gotl = []
    for t in lts:
        url = f"https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/{t}.tif"
        p = f"{OUT}/lc/{t}.tif"
        s = fetch(url, p)
        if s in ("ok", "cached"):
            gotl.append(p)
        print(f"  {t}: {s}", flush=True)
    open(f"{OUT}/lc_list.txt", "w").write("\n".join(gotl))

    # build VRT mosaics if gdalbuildvrt available
    for layer, lst in [("dem", got), ("lc", gotl)]:
        if lst:
            try:
                subprocess.run(["gdalbuildvrt", f"{OUT}/{layer}.vrt"] + lst, check=True,
                               capture_output=True)
                print(f"built {OUT}/{layer}.vrt ({len(lst)} tiles)")
            except Exception as e:  # noqa: BLE001
                print(f"vrt {layer} failed: {e}")


if __name__ == "__main__":
    main()
