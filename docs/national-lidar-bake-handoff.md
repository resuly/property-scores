# 全澳 5m LiDAR Flood 高程层 — 一次性实施 Handoff

> 目的:新开对话照此**一步到位**建完。本文自包含,所有端点/命令/坑/资源约束/数字都是
> 2026-07-15 本人真查真测出来的(不是文档二手结论)。关联主文档:
> [flood-dem-upgrade.md](flood-dem-upgrade.md) 第五/六节;记忆 `project_flood_dem_upgrade`。

---

## 0. 一句话目标

用**一层统一的全国 5m LiDAR 裸地 COG**(本地采样)接进 flood HAND,替换现在 5 州的
live-API 拼图,填上 SA/ACT/NT(只能下载的州)、替掉 WA 那份老的非 LiDAR 数据。
有 LiDAR 的地方(≈245,000 km²,基本=有地址的地方)= high 置信度、确定性、亚毫秒;
没 LiDAR 的内陆 = DEM-H 30m 兜底。**改的是高程数据底座,不是评分方法论**(HAND +
官方图层 + JRC 卫星 + 降雨那套不动)。

---

## 0b. 架构定调:5m 全国基线 + 1m 按地块 on-demand(别烤全国 1m)

- **全国基线 = 5m**。所有筛查评分层(flood HAND / bushfire 坡度 / view / noise / solar)
  都够——1m vs 5m 只差水平细节,垂直精度一样(±0.2m),这些层都是大地形尺度。一份 ~10GB 通吃。
- **1m 只在地块级工程细节才有意义**(某块地的积水点/车道坡度/cut-fill 土方/granny flat 3D 地形/
  挡土墙)。这类**天生是 per-lot**:用户分析哪块地就 on-demand 拉那块的 1m 瓦片(同一条 ELVIS
  链路,单块 1m 几 MB、~26s 到手),用完不落盘。**绝不为此烤全国 1m(250GB)/ 买盘。**
- **Contour 地图视觉**想保留原生 1m 好看:继续读各州 live 等高线 API(免费零存储),不驱动全国 1m。

## 1. 现状:什么已完成,别重做

**已上线生产**(prod :8099 = `/var/www/property-scores` master,systemd `property-scores.service`):
- 5 州 on-demand LiDAR 已接进 flood,加了 `elevation_confidence` high/medium/low 徽章:
  - **NSW/QLD** 读州政府栅格 ImageServer(NSW 5m / QLD 0.5m,`flood/lidar.py` 的 `Window` + exportImage)
  - **VIC/TAS/WA** 读开放等高线 FeatureServer,IDW 插值(`ContourWindow`)
  - 兜底 DEM-H 30m(`common/terrain.py`),再兜底 proxy
- 代码:`property_scores/flood/lidar.py`(两 provider,一个 `open_window()` 鸭子类型
  `elev`/`close`/`source`/`uncertain_thresh`)+ `flood/score.py`(`_hand_local(lat,lng,state)`
  三级 + `_hand_from_elev` 共享环几何 + `_ELEV_CONFIDENCE` 映射)。
- 测试 90 passed。

**本项目 = 把上面统一成一套本地烤好的全国 5m LiDAR COG。** 完成后现有 5 个 live-API
provider 变冗余,可下线或留作 2015 后新拍区域的兜底(建议直接下线,LOCAL LiDAR VRT →
DEM-H → proxy,最简单)。

**ELVIS 采集链路已端到端验证**(2026-07-15):Canberra 24km² 1m 测试单 → 提交后 **26 秒**
收到 elevation@ga.gov.au「Your data is ready」→ **公开** zip(匿名可下,非 requester-pays)→
烤成 1.1MB 5m COG → 采样正确(湖岸 HAND 0.6m)。

---

## 2. ⚠️ 服务器 / 资源约束(最重要,先读)

- **Oracle Melbourne** `161.33.67.71`,ARM aarch64,Ubuntu 24.04,4 OCPU / 24GB RAM /
  250GB 卷。SSH:`ssh -i ~/.ssh/id_ed25519 ubuntu@161.33.67.71`。
- **磁盘现剩 ~52GB**(`df -h /` 实测 191G/242G used)。boot volume 已付费扩过,
  **除此之外不要开额外资源**(CLAUDE.md 硬规矩)。本层输出 ~10GB → 用完剩 ~42GB。**不用买盘。**
- **顺手回收 ~5GB**:`data/global/dem/`(旧 Copernicus DSM)占 6.2GB,P1 已全 AU 迁 DEM-H
  (`dem_h/` 5.3GB),那 196 块 **AU Copernicus 瓦片现在冗余**(仅 ~25 块非 AU 仍被合并 VRT 引用)。
  删 AU 那批可回收 ~5GB,几乎抵消 LiDAR 新增(净增 ~5GB)。⚠️删前确认 `dem.vrt.demh` 只引用
  DEM-H 不引用 AU Copernicus;保留非 AU 瓦片;`dem.vrt.copernicus.bak` 回退网若不再需要可一并清。
- **★这不是 flood 专项开销,是共享地形地基**:同一份 5m LiDAR DEM 同时升级 flood(HAND)/
  bushfire(坡度)/ view / noise / solar(都采样 `common/terrain.py` 的 DEM),还能本地
  `gdal_contour` 生成等高线**退掉 da_leads 地图那套 5 州 contour live-API**(见记忆
  `project_contours_state_api`;注:5m 派生等高线线条比原生 1m 略粗,视觉略糙但统一+离线+填 SA/ACT/NT)。
  所以 10GB 换 5-6 个分数升级 + 退两套 live-API 拼图,ROI 高,**买盘只有全国上 1m(~250GB)才需要,而我们不需要 1m**。
- **★重活资源护栏**(`feedback_server_resource_guard`,2026-07-08 并行烤制宕全站 25min 的教训):
  bake/gdalwarp/大下载 **绝不并行**;跑前 `free -h` 查内存;跑时挂 `top`/资源监控,占爆就杀
  任务保机器。**分区串行**下载 + 烤制,不要一次拉全国。
- 部署:`cd /var/www/property-scores && git pull && sudo systemctl restart property-scores.service`。
- ⚠️ prod `.git` 曾被 sudo 搞成 root 属主致 pull 失败,遇到 `chown -R ubuntu:ubuntu .git` 修。
- flood 是 **live 现算**(prod 无 `flood_cache_*.parquet`),LOCAL COG 每请求采样一次,
  本地 rasterio 采样亚毫秒,没问题。

---

## 3. 数据源 + 采集方法(ELVIS,全部实测)

### 3a. 产品:下「5 Metre」不下「1 Metre」
- **GA 全国 5m LiDAR DEM**,≈245,000 km²(有人海岸带 + Murray-Darling 洪泛 + 各城镇),
  CC BY 4.0,attribution `© Geoscience Australia` + 各州署名(逐瓦片来源不同,建 attribution 登记)。
- **为什么 5m 不 1m**:1m 是 native,下全国 ≈ 2.5TB;5m 是 GA 派生产品,**下载量 1/25**
  (~15–20GB 全国),且已是 5m 不用降采样。5m 对 flood HAND **足够**——垂直精度和 1m 一样
  (±0.2m,同一批激光),只少了用不上的水平细节;NSW 那个"survey 级"live 源本来就是 5m。
  实测 1m vs 5m 同点高程差 <0.2m,而 30m DEM-H 差 ±4–6m。**"5m"是水平格子,不是高度误差。**

### 3b. 枚举可下瓦片(公开 API,无 auth)——**先跑这步把全国规模钉死**
```
GET https://api.elevation.fsdf.org.au/elevation/downloadables?polygon=<WKT>
```
- `<WKT>` = EPSG:4326 的 `POLYGON((lon lat,lon lat,...))`,lon lat 空格分,顶点间逗号,闭环。
- **必须带浏览器头**,否则 CloudFront 返回 403:
  `-H "User-Agent: Mozilla/5.0 ... Chrome/120 ..." -H "Origin: https://elevation.fsdf.org.au" -H "Referer: https://elevation.fsdf.org.au/"`
- 单次查询**上限 10,000 条**,大范围要切格子(如 1°×1°)分查。
- 返回 `available_data[].downloadables["Digital Elevation Models"]["5 Metre"][]`,每条有
  `file_name` / `file_url` / `file_size`(字节,可能是字符串)/ `bbox`(4326)。
- **第一步动作**:把全国人口区切成 1°×1° 格子逐格查「5 Metre」,累加 `file_size` + 去重瓦片
  → 得到确切**瓦片数 + 总下载量 + 覆盖足迹**。把 ~10GB / ~15-20GB 这两个估算核实成真数字。

### 3c. 采集:走 ELVIS 下单流程 → 公开 zip(**不用 AWS**)
> `file_url` 直链指向 **requester-pays** 桶(act-elvis / sa-elvis / ga-elvis,匿名 403,要 AWS
> 账号签名)。**别走直链**。走下单流程,输出是**公开** zip,零 AWS。

**下单 = 逆向出来的两步**(ELVIS Angular `sendDownloadRequest(e,r)`):
1. 生成一个 uuid `i`,构造
   `s = { available_data: <只留选中的 5 Metre 数据集,同 downloadables 结构>, parameters: {email, industry, outCoordSys:"", outFormat:"", polygon:"POLYGON((...))"} }`
2. 用**无认证 Cognito 身份池** `ap-southeast-2:56462c13-533a-4f84-9a68-631dcd3345ad`(region
   `ap-southeast-2`)拿临时 AWS 凭据 → 把 `${i}.json`(JSON.stringify(s))PUT 到 S3 桶
   `ga-elvis-uploads-prod`(SigV4 签名)。
3. `POST https://api.elevation.fsdf.org.au/elevation/initiateJob`,body `{"requestId":"<i>"}`(带浏览器头)。
4. ~26 秒后 `elevation@ga.gov.au` 发邮件,主题「Your data is ready」,正文含公开链接
   `https://elvis-downloads.s3.amazonaws.com/DATA_<n>.zip`(HTTP 200 匿名可下)。
   收邮件用 IMAP(`GMAIL_APP_PASSWORD` in `limon-ops/.env`,`botwang7@gmail.com`)轮询取链接。

**Cognito 拿临时凭据**(纯 HTTP,无 boto3 也行):
- `POST https://cognito-identity.ap-southeast-2.amazonaws.com/` header `X-Amz-Target: AWSCognitoIdentityService.GetId`,body `{"IdentityPoolId":"ap-southeast-2:56462c13-533a-4f84-9a68-631dcd3345ad"}` → `{IdentityId}`
- 同 host,`X-Amz-Target: AWSCognitoIdentityService.GetCredentialsForIdentity`,body `{"IdentityId":"..."}` → `{Credentials:{AccessKeyId,SecretKey,SessionToken}}`
- 用临时凭据 SigV4 签名 PUT 到 `ga-elvis-uploads-prod`。
- ⚠️ `s.available_data` 的确切"选中"结构(app 里 `datasetTreesToDownloadableTrees` 产出)有
  逆向风险,payload 错了 job 会静默失败。**建议先脚本化跑通 1 个小格子验证收到邮件,再批量。**

**退路(若脚本化 available_data 太脆):** 用浏览器 UI 半自动。Order Data → **Manual**(填
N/W/E/S,EPSG:4326)→ Search → 展开 `Geoscience Australia → Digital Elevation Models →
5 Metre` 点「Select all」→ Industry(必填,选 Property Development)→ Email `botwang7@gmail.com`
→ Order。每单一个 AOI。全国切成 ~30–60 个大格子(GA 5m 瓦片很大,一个 1°×1° 格是可控单量)。

---

## 4. 烤制管道(验证过的配方)→ `scripts/bake_lidar_cog.py`

每个下载 zip 解压后是若干 5m GeoTIFF(可能 float32/64,各州按 MGA zone 投影或已 4326)。
逐区(分区串行,见 §2 护栏):
```bash
# PROJ 冲突(dev Mac 上 EclipseSUMO proj.db 抢占;Oracle 一般干净但也 set 上)
export PROJ_LIB="$(python3 -c 'import os,rasterio;print(os.path.join(os.path.dirname(rasterio.__file__),"proj_data"))')"
export PROJ_DATA="$PROJ_LIB"

gdalbuildvrt -q region.vrt <解压出的 5m tiles>            # 有多版本时优选最新(如 ACT2020>ACT2015)
gdalwarp -q -t_srs EPSG:4326 -tr 0.0000449 0.0000449 -r average \
         -dstnodata -9999 -overwrite region.vrt region_5m_f.tif   # 0.0000449°≈5m
# → Int16 分米(×10)+ DEFLATE COG(rasterio 读→×10→Int16 nodata -32768→gdal_translate -of COG)
gdal_translate -q -of COG -co COMPRESS=DEFLATE -co PREDICTOR=2 region_5m_i16.tif region_5m.tif
rm -f <raw tiles> region.vrt region_5m_f.tif region_5m_i16.tif  # 烤完即删原始,省盘
```
- **为什么 Int16 分米不 Float32**(dtype 是垂直数值精度,跟 1m/5m 水平分辨率无关,别混):
  LiDAR 真实垂直精度 ±0.1~0.3m。Float32 存到亚毫米=记录噪声位(假精度);Int16 分米(0.1m)
  正好装下全部真精度、一分真信息不丢。省 2.9× 存储(全国 10GB vs float32 26GB),多的 16GB
  全是没意义的噪声。HAND 本就 round 到 0.1m,零影响。
- **Int16 分米**:全澳最高 Kosciuszko 2228m ×10 = 22280 < 32767,安全。海面/nodata → -32768。
- **实测密度**:5m Int16 DEFLATE ≈ **35–45 KB/km²**(ACT LiDAR;平滑区 WA 是 22)。
  全国 245k km² × ~40KB ≈ **~10GB**。用 §3b 枚举出的真瓦片数核死。
- 输出到 `data/global/lidar/<region>_5m.tif`,最后 `gdalbuildvrt data/global/lidar/au_lidar_5m.vrt data/global/lidar/*_5m.tif`。
- attribution 登记:每区记来源州 + 数据集名(同 `geocode_source` 模式),CC BY 逐州署名。

---

## 5. 接入 flood(`property_scores/flood/lidar.py` + `score.py`)

- 新增 **LOCAL provider**:仿 `common/terrain.py` 的写法,开 `data/global/lidar/au_lidar_5m.vrt`,
  用共享 `noise/raster_sample.py` 采样器(任意 CRS + 本地路径 + per-thread handle)。
  覆盖门控 = 采样非 nodata(-32768)。source = `lidar_5m_local`,`uncertain_thresh=1.0`,
  confidence = **high**。
- `_hand_local(lat,lng,state)` 顺序改为:**① LOCAL LiDAR VRT(有覆盖就用)→ ② DEM-H 30m →
  ③ proxy**。现有 5 州 live-API provider(`open_window` 的 raster/contour 分支)**下线**
  (或降为 LOCAL 无覆盖时的兜底;建议直接删,统一走本地)。
- `_ELEV_CONFIDENCE` 加 `"lidar_5m_local": "high"`。
- 覆盖用 §3b 足迹或直接靠 VRT nodata 门控(后者更简单,和 terrain.py 一致)。
- flood.html 徽章文案:high 的副标从"Survey-grade LiDAR"保持;方法论 data-source 行更新为
  "全国 5m LiDAR 裸地(GA,有覆盖处),else DEM-H 30m"。

---

## 6. 执行顺序(建议)

1. **枚举**(§3b):切格子查全国「5 Metre」→ 确切瓦片数 + 下载量 + 覆盖足迹。**先量再建。**
2. **打通采集**:脚本化下单跑 1 个小格子(或 UI 半自动),确认收到公开 zip。
3. **写 `bake_lidar_cog.py`**(§4),先烤 1 个首府(如 Canberra 或 Adelaide)端到端验证。
4. **加 LOCAL provider + 改 `_hand_local`**(§5),本地测:covered 点 = high LiDAR、无覆盖 =
   DEM-H medium、真 flood 分前后无回归。90+ 测试全过。
5. **分区串行**跑全国采集 + 烤制(§2 护栏:不并行、监控、烤完删原始)。
6. **部署**:ship `data/global/lidar/` + VRT 到 Oracle,`git pull` 代码,restart service。
   prod 实测 5-10 个跨州地址(含 SA/ACT/NT 现在能拿到 high 了)。
7. **收尾**:更新 flood-dem-upgrade.md + 记忆 + Kenneth 卡;live-API provider 下线的说明。

**可选先落地**:若不想一次全国,先烤 **Canberra + Adelaide**(填最大的两个 gap,当天能完),
全国排后——同一套管道,只是 AOI 小。

---

## 7. 坑 / 已验证事实(别重踩)

- ELVIS `downloadables`/`initiateJob` **裸 curl 无 UA = CloudFront 403**;带浏览器 UA+Origin+Referer 才通。
- **下 5 Metre 不下 1 Metre**(§3a):1m 是 float64、含重复年份版本、25× 大;我第一次误下 1m,
  450MB 只有 1MB 有用。
- 直链 `file_url` = requester-pays(要 AWS);**下单流程输出的 elvis-downloads zip 是公开的**(不要 AWS)。
- 单查询 10,000 条上限;大 AOI 要切格。
- PROJ 冲突要 set `PROJ_LIB`/`PROJ_DATA` 到 rasterio bundled(§4)。
- ArcGIS/GA 服务连打会限流——采集**串行 + 间隔**,别并发轰。
- 资源护栏:**bake 绝不并行**,跑前查内存跑时监控(§2)。
- "5m 分辨率 ≠ 5m 高度误差":5m LiDAR 垂直仍 ±0.2m,是水平格子大小;±5m 那种"淹一层楼"是
  30m DEM-H 的病,LiDAR 正是解药。对外话术别把 5m 说成不准,也别把 LiDAR 说成"厘米级"(是分米级)。

---

## 8. Kenneth / 对外话术(诚实边界)

- 说**"分米级 / sub-metre 垂直精度"**,不说"厘米级"(会被专业对手抓)。
- 说**"地形洪水信号 / 相对筛查"**,不说"水力模拟",不说"判定/保险定价级"。
- 说**"5 州已上线读 LiDAR + 下一步统一全国基线"**,不说"全国已实施"(还没建)。
- **换的是高程数据底座,不是评分方法论**——HAND + 官方图层 + 卫星 + 降雨不变,只是把 30m
  雷达噪声换成 LiDAR 裸地。
- 覆盖=有 LiDAR 的人口区(~245k km²),内陆无 LiDAR 处仍 DEM-H 30m——别吹"全国每一寸 LiDAR"。
- 披露纪律:覆盖/数据源可讲,**方法论 how 控着说**。
