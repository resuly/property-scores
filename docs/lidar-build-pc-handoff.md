# 全澳 5m LiDAR DEM — 在大盘机器上构建 Handoff

> 在**有 ≥500 GB 空闲 SSD 的机器**上一次性烤好全国 5m LiDAR COG,再上传到 Oracle。
> 之前在 Mac / Oracle 都卡死,唯一原因是**磁盘不够**(大 zone 解压后几百 GB);这台 PC 有盘就能过。
> 关联:[national-lidar-bake-handoff.md](national-lidar-bake-handoff.md)(数据源/枚举细节)。
> 全部命令/数字 2026-07-16 真测。

---

## 0. 这是什么 / 为什么在这台机器上跑

property-scores 的多个评分(flood HAND / bushfire 坡度 / noise / walkability)现在用 30m/90m
高程,精度粗。要统一升到 **GA 全国 5m LiDAR 裸地 DEM**(~245,000 km² 有覆盖区),烤成本地
Int16 分米 COG,让所有评分**离线、瞬时、口径一致**地采 5m。

**唯一的坑 = 磁盘。** GA 的 zip 里 tif 是 DEFLATE 压的,解开极大:z55 = 137974×641646 =
885 亿像素 ≈ **354 GB**。不解压走 `/vsizip` 流式读,gdalwarp 每次随机读都要从头解一遍整条流 →
Mac 和 Oracle 上都**卡死**(实测 26% 一颗核、纹丝不动)。**解决 = 先把 tif 解压到本地盘**,
gdalwarp 对本地未压缩 tif 随机读是廉价的、还能跳过 nodata 块 → 分钟级跑完。这就是需要 500 GB 的原因。

---

## 1. 机器要求

- **≥500 GB 空闲 SSD**(z55 单 zone 峰值:354 GB tif + ~10 GB warp 中间 ≈ 365 GB)。
- **gdal CLI**:`gdalwarp` / `gdal_calc.py` / `gdalbuildvrt` / `gdal_translate` / `gdalinfo`。
- **python3**、**curl**(Win10+ 自带 `curl.exe`)。解压用 python `zipfile`(不需要 `unzip` 二进制)。
- 网络能匿名访问 `elevation-direct-downloads.s3-ap-southeast-2.amazonaws.com`(公开 S3,实测 200)。

**装 gdal 三选一(Windows):**
- **WSL2 Ubuntu(最省事,推荐)**:`sudo apt install -y gdal-bin python3-gdal python3-numpy git curl`,
  在 WSL 里把 500 GB 盘挂到有空间的路径(`/mnt/d/...`),repo clone 到那儿。
- **OSGeo4W**:装 `gdal` 包,用 "OSGeo4W Shell"(自带 gdal CLI + python + PATH)。
- **conda**:`conda create -n gdal python=3.11 gdal -c conda-forge && conda activate gdal`。

---

## 2. 步骤

```bash
# (1) 拿到最新代码(bake 脚本已含 --unzip 模式 + 跨平台 df/du)
git clone https://github.com/resuly/property-scores.git   # 或已有则 git pull
cd property-scores

# (2) 确认 data/global/lidar 所在盘有 ≥500 GB。脚本把中间文件写在
#     data/global/lidar/_work/ 下。若 repo 不在大盘上,把整个 repo 放到大盘,
#     或先 mkdir 大盘目录再软链:  ln -s /mnt/d/lidar_work data/global/lidar/_work

# (3) 烤全国(--unzip 必须加!大 zone 没它会卡死)。大 zone 先,峰值最低:
python3 scripts/bake_lidar_cog.py --unzip \
  nationalz55_ag nationalz56_ag nationalz54ag waz50 ntz53 ntz52 waz51

#     跑的时候另开一个窗口盯盘:  watch -n30 'df -h .'    (WSL/Linux)
#     或 Windows:  循环看 data/global/lidar/_work 目录大小
```

**每个 zone 脚本自动做**:下载 zip → 解压 tif 到 `_work/` → 删 zip → gdalwarp 到 EPSG:4326@5m
(`-r near`)→ gdal_calc ×10 转 Int16 分米(nodata -32768)→ gdal_translate 出 COG →
删中间文件 → 记 manifest。最后 `gdalbuildvrt au_lidar_5m.vrt *_5m.tif`(相对路径,可移植)。

**产物**(在 `data/global/lidar/`):`*_5m.tif` × 7 + `au_lidar_5m.vrt` + `manifest.json`,合计 **~10–12 GB**。

### 预期耗时(诚实:大 zone 的实测速度我没在大盘上验过)
- 下载:全 7 个 zip ~32 GB,按你带宽算(S3 单流 ~25–90 MB/s)。
- 解压:z55 的 354 GB 写盘,按 SSD 写速 ~10–30 分钟;小 zone 秒级。
- warp:本地读盘,gdalwarp 跳 nodata,估 z55 ~15–60 分钟,z56/z54 类似,小 zone 分钟级。
- **全国总计估 2–5 小时**(下载 + 解压 + warp),一次性。**若某个大 zone warp 仍异常慢
  (>2h 无进展),先只跑那一个 zone 排查,别怀疑小 zone。**

---

## 3. 验证(上传前必做)

```bash
# 7 个 COG 都在、VRT 引用相对路径(不是绝对路径)、跨州采样正确
ls -lh data/global/lidar/*_5m.tif
grep -o '<SourceFilename[^>]*>[^<]*' data/global/lidar/au_lidar_5m.vrt | head   # 应是裸文件名
# 抽样(值是分米,/10 = 米):Sydney ~209 / Adelaide ~449 / Perth ~128 / Darwin ~253
for p in "151.2093 -33.8688" "138.6007 -34.9285" "115.8605 -31.9505" "130.8456 -12.4634"; do
  echo -n "$p -> "; echo "$p" | gdallocationinfo -valonly -geoloc data/global/lidar/au_lidar_5m.vrt
done
```

---

## 4. 上传到 Oracle

```bash
# 只传成品(COG + 相对路径 VRT + manifest);_work/ 不传。rsync 增量,断了可续。
rsync -avz -e "ssh -i <你的key> " \
  --include='*_5m.tif' --include='au_lidar_5m.vrt' --include='manifest.json' \
  --exclude='_work' --exclude='*' \
  data/global/lidar/ ubuntu@161.33.67.71:/var/www/property-scores/data/global/lidar/
# ~10–12 GB,按上行带宽算(实测某 Mac 4.8 MB/s → ~40 分钟;你 PC 可能更快)。
```

上传完**告诉 Claude(在 limon-ops 会话)**,剩下的**代码接线 + 部署 + 验证**我来做:
- `common/terrain.py` 改成优先采本地 5m VRT(有覆盖用 5m,否则 DEM-H 30m 兜底)→
  flood/bushfire/noise/walkability **自动全升 5m**;flood 已接的 `lidar_local` 收编统一。
- `sudo systemctl restart property-scores.service`,跨州地址实测 + `pytest`(90 passed)不回归。
- Oracle 装季度 `cron_with_alert.sh --check`(HEAD 校验,GA 极少更新,基本 no-op)。

---

## 5. 兜底 / 备选(别忘了还有一条路)

若这台 PC 的大 zone warp 也不顺(或你懒得每年重烤),**GA 有全国 5m 的 live 服务**,实测可用:
- WCS 窗口:`services.ga.gov.au/gis/services/DEM_LiDAR_5m_2025/MapServer/WCSServer`
  `?SERVICE=WCS&VERSION=1.0.0&REQUEST=GetCoverage&COVERAGE=1&CRS=EPSG:4283&BBOX=<minx,miny,maxx,maxy>&WIDTH=223&HEIGHT=223&FORMAT=GeoTIFF`
  → 一次 ~200ms 返回一格 Float32 高程栅格,全国覆盖(含 SA/NT/WA)。
- 那条路**零构建零存储**,代价是评分走网络(按格缓存 + DEM-H 兜底,和现有 NSW/QLD provider 同款)。
- 本地烤 vs GA live 的取舍:本地=离线/瞬时/一致但要构建+存储;live=零构建但每次评分依赖 GA。
  你选了本地(offline 更适合"垫在所有评分底下"的基座),这份 handoff 就是为它准备的。

---

## 6. 关键坑(别重踩)

- **必须 `--unzip`**;不加则大 zone 走 /vsizip 卡死。
- `-r near` 不是 average:源和目标都 5m,是重投影不是降采样,near 保真且快。
- Int16 分米:全澳最高 Kosciuszko 2228m ×10=22280 < 32767 安全;海面/nodata → -32768。
- gdal_calc 若报 numpy 版本冲突(system osgeo 对 numpy 1.x,~/.local 有 numpy 2.x):脚本已
  设 `PYTHONNOUSERSITE=1` 规避;仍报则 `pip uninstall numpy`(用 system)或在干净 env 跑。
- VRT 必须**相对路径**(脚本已在 OUT_DIR 内 cwd 生成),否则传到 Oracle 解析不到瓦片。
- PROJ 若报 `Invalid SRS`(某些机器有冲突 proj.db):`export PROJ_LIB=<gdal的proj目录>`
  (WSL: `/usr/share/proj`;OSGeo4W: `...\share\proj`;conda: `$CONDA_PREFIX/share/proj`)。
