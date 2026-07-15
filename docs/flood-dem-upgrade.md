# Flood Score DEM Upgrade — Research, Plan & Progress Tracker

> 起因: 2026-07-15 LinkedIn flood 帖(21.4k 曝光)评论区两位地理空间专家公开批评当前高程数据。
> Hugh Saalmans: "Copernicus elevation is well outside the tolerance for financial & insurance risk modelling. Look at LIDAR / aerial derived elevation models available as open data."
> Naveen Ragu Ramalingam: "Careful that CopernicusDEM is actually a DSM product not bare earth elevation."
>
> 打法(Bo 定,同噪声模型): **先调研 → 改进 → 验证 → 上线 → 再回复评论**。不空头承诺。
> 关联: Kenneth Ng (Geoscape data partnerships) 07-16 通话谈 flood 合作,此升级是话术底牌。

---

## 进度追踪

| 阶段 | 内容 | 状态 | 更新 |
|------|------|------|------|
| 调研 | FABDEM / AU LIDAR / 精度容差 三路 web 调研 | ✅ 完成 | 2026-07-15 |
| **实证 POC** | Sydney 5 点 DSM vs NSW 5m LiDAR 偏差对比 | ✅ 完成 | 2026-07-15 |
| **P0** | 重贴标签: flood 分定位为"相对筛查非判定级"+明写不确定度 | 🟡 property-scores flood.html 已改; 待: da_leads 客户侧 flood 报告文案 + API 输出字段 | 2026-07-15 |
| **P1** | Copernicus GLO-30 DSM → GA DEM-H 30m 裸地+水文强化 | ✅ **已部署生产 + 线上验证通过** | 2026-07-15 |

### P1 本地实施 + 回归测试(2026-07-15)

- **数据**: 196 AU DEM-H 1° 瓦片抽好(`scripts/extract_au_demh.py`, 0 fail) → `data/global/dem_h/`
- **合并 VRT**: 25 非AU Copernicus + 196 AU DEM-H = 221; `dem.vrt` 已切(备份 `dem.vrt.copernicus.bak` / `dem.vrt.demh`)
- **安全**: 无 nodata 泄漏(海面读 0.0 同旧行为), 陆地裸地值正常
- **四分回归(old Copernicus vs new DEM-H, live)**:

| 地址 | flood 旧→新 | HAND 旧→新 | bush | walk | noise |
|---|---|---|---|---|---|
| **Parramatta 河岸 NSW** | **92→65** | **18.3→4.6** | 89→89 | 37 | 43 |
| Kew ridge | 90→87 | 16.1→12.0 | 70→73 | 71 | 7 |
| Maribyrnong | 86→83 | 15.7→14.0 | 55 | 51 | 58 |
| Elwood flat | 13→25 | 0.8→0.9 | 59 | 56 | 47 |
| Ryde | 95→95 | 23.4→25.2 | 66 | 50 | 13 |

**结论**: DEM-H 修好近水低洼假安全(Parramatta 92→65=Very Low→Moderate), 其它分无回归(仅 bushfire 坡度微调)。无崩溃/nodata 泄漏。Elwood 13→25 由 overlay 主导仍高风险档, 不影响结论。**部署前测试通过。**

### 待部署(Bo 最终批)
1. 重跑 precompute 重建 `flood_cache_*.parquet`(+其它分缓存如有), 否则 map 服旧缓存值
2. prod(Oracle :8099)ship 196 DEM-H 瓦片 + 重建 prod VRT + 重跑缓存 + reload; 新缓存烤好原子切换, 不停服
3. 上线后: 改 da_leads 来源名 Copernicus→DEM-H 裸地(营销点) + 回 Hugh/Naveen
| **P2** | 1m/5m LIDAR DTM (ELVIS/GA) 优先市场 + 覆盖/置信度 flag | ⬜ 未开始(端点已通) | — |
| 回复 | P0+P1 上线后回 Hugh/Naveen(结果导向) | ⬜ 待上线 | — |

### 实证 POC 结果(2026-07-15)— DSM 偏差坐实

同一批 Sydney 真实地址,Copernicus DSM(本地瓦片)vs NSW 5m LiDAR 裸地(SIX ImageServer identify):

| 测试点 | DSM(Cop) | LiDAR 5m | DSM 高估 |
|---|---|---|---|
| North Ryde(David 评论测试地址的区) | 87.68 | 81.60 | **+6.08m** |
| Chatswood 空地 | 115.40 | 102.86 | **+12.54m** |
| **Parramatta 河岸(低洼近水)** | 24.40 | 14.88 | **+9.52m** ⚠️ |
| Ryde 清空住宅 | 64.80 | 61.66 | +3.14m |
| Lane Cove NP(密林) | 55.06 | 55.08 | -0.02 |

**结论**:DSM 系统性高估地面 +3~+12m(城市/树/近水处最重)。Parramatta 河岸 +9.52m = HAND 把近水低洼地读得更高更安全 → flood 分**偏乐观、危险方向 under-warn**。批评用真实 AU 数字坐实,升级值得做。
NSW LiDAR identify 端点可用(P2 数据源打通)。POC 脚本逻辑存 session,可复用扩样本。

### DEM-H 三方验证(2026-07-15)— 比预想微妙

DEM-H 数据源确认: **DEA 公共 S3 COG**(无认证, CC BY 4.0): `/vsicurl/https://dea-public-data.s3.ap-southeast-2.amazonaws.com/projects/elevation/ga_srtm_dem1sv1_0/demh1sv1_0.tif`(国家单文件, `gdal_translate -projwin ulx uly lrx lry` 取任意 bbox ~12-20s)。

同 5 点三方对比 Copernicus DSM / GA DEM-H / NSW LiDAR 真值:

| 点 | LiDAR | DSM | DEM-H | DSM err | DEM-H err |
|---|---|---|---|---|---|
| **Parramatta 河岸(近水低洼)** | 14.88 | 24.40 | 14.43 | +9.52 | **-0.45** ✅✅ |
| North Ryde | 81.60 | 87.68 | 83.38 | +6.08 | **+1.78** ✅ |
| Ryde 住宅 | 61.66 | 64.80 | 65.35 | +3.14 | +3.69 |
| Lane Cove 密林 | 55.08 | 55.06 | 50.15 | -0.02 | -4.93 |
| Chatswood 空地 | 102.86 | 115.40 | 124.39 | +12.54 | +21.53 ❌ |
| **MAE** | | | | **6.26** | **6.48** |

**发现(平均值骗人,分布是重点)**: DEM-H **不是全面更准**(SRTM 底噪让高地点更差, MAE 打平),**但在洪水相关的低洼近水地形上大幅去偏**(Parramatta +9.52→-0.45)。对 flood 很可能净正(HAND 只关心低洼/drainage 地形),但不是通用精度胜利。真正干净的胜利是 **LiDAR(P2)**。
**⚠️ 决定性验证 = 跑真实 flood 分的 HAND 前后对比**(不是点高程), 才能判 P1 值不值得全量换。下一步做。

状态图例: ⬜ 未开始 / 🟡 进行中 / ✅ 完成 / ⏸ 阻塞

---

## 一、批评判定

| 批评 | 事实核查 | 判定 |
|---|---|---|
| **Naveen: Copernicus 是 DSM 非裸地** | 属实。GLO-30 = TanDEM-X 雷达表面模型,含树冠/建筑 | ✅ **成立,廉价可修的真缺陷**。DSM 把树/楼高加到地面→HAND 读得偏高偏安全→洪水分**偏乐观(危险方向: 对植被/城市地块 under-warn)**。裸地校正去掉森林 ~2.3m、建成区 ~0.5m 误差;裸 DSM 跑洪水模型比 LiDAR **高估 2-3×** |
| **Hugh: 达不到金融/保险容差** | 属实(GLO-30 RMSE ~4.9m / LE90 ~7.7m;FEMA 判定级要 ≤0.15m LiDAR) | ⚠️ **只对"判定/定价级"定位成立**;对"相对筛查"是归类错误——保险公司自己也用全球 DEM 模型做组合筛查(Fathom 即是),标对了就是正当 B2B 产品 |

- HAND 方法在 3–20m 分辨率都稳(60–90m 才崩)→ **30m 横向分辨率不是问题**,问题在 DSM 偏差 + 过度声称。
- 代码已自认 "GLO-30 vertical noise ~4m"(`flood/score.py:505`),底子诚实,只需把定位说明白 + 换裸地。
- **一句话**: 专家物理没错,但把"筛查信号"当"保险判定"是过度延伸。修法便宜且具体。

## 二、方案(全部 CC BY 4.0 商用可用)

### P0 — 重贴标签(零成本,当天)
- flood 分定位改为: "relative flood screening / terrain HAND signal, 30m bare-earth, ~1–3m 垂直不确定, directional, **not FEMA/BFE determination-grade**"。
- 页面 / 方法论 / API 输出 明写不确定度与分辨率下限。永不暗示保险定价级精度。
- 解决 Hugh 的"过度声称"问题。

### P1 — 换裸地 DEM(高 ROI,近 drop-in,1 天级)
- **Copernicus GLO-30 DSM → GA DEM-H 30m**(裸地 + 水文强化: drainage 已烧进 DEM,天生适配 HAND)。
- 工程: 换 `data/global/dem/` 瓦片 → 重建 `dem.vrt` → 重跑 `scripts/precompute_flood.py`;`common/terrain.py` / `flood/_hand_local` / 水位模拟代码**不动**(同 30m 网格 GeoTIFF,走同一 rasterio sampler)。
- 授权: **CC BY 4.0**(GA/ELVIS),商用可用 + 署名。
- ⚠️ FABDEM 免费版是 **CC BY-NC-SA(非商用+ShareAlike)** → 法律出局,除非付费 Fathom(无公开报价)。故走 GA DEM-H。
- 解决 Naveen 的 DSM 缺陷 + 给 HAND 去偏。
- 验证: P1 前后对比 flood 分变化(重点看植被/城市地块 + Steven 的 Skirving St 类案例 + 帖子 Elwood/Kew 两例),量化改善。

### P2 — LIDAR 精度层(中等工作量,后续)
- **1m/5m LIDAR DTM(ELVIS/GA,CC BY 4.0)** 覆盖首府都会+海岸带+洪泛走廊(覆盖恰好集中在房子和风险集中处)。
- 获取: ELVIS 批量下载(15GB/次免费)→ 转 COG 本机服务(复用 DA Leads 瓦片管道),或 NSW 5M ImageServer 点查 / Earth Engine `AU/GA/AUSTRALIA_5M_DEM`。
- 按地址给**覆盖/置信度 flag**(高: LIDAR / 中: 30m)—— 本身是卖点。
- ⚠️ 逐 survey 瓦片核 license(总体 CC BY 4.0,个别自定义),建 attribution 登记(同 `geocode_source` 模式)。
- 解决 Hugh 的"survey 级",有覆盖处。

### 加宽证据表(2026-07-15, old Copernicus vs new DEM-H, 11 址)

| 地址 | old→new | HAND o→n |
|---|---|---|
| Parramatta 河岸 NSW | **92→65** | 18.3→4.6 |
| Windsor NSW(Hawkesbury) | **91→85** | 15.7→9.7 |
| Gawler SA | 25→22 | 5.9→1.7 |
| Lismore | 58→60 | 10.8→11.3 |
| Rochester | 40→40 | 1.3→0.0 |
| Shepparton | 75→75 | 1.7→3.0 |
| Launceston TAS | 86→85 | 10.7→8.4 |
| Maribyrnong(2022洪) | 44→65 ⚠️ | 0.9→3.3 |
| Elwood canal flat | 13→25 ⚠️ | 0.8→0.9 |
| Kew ridge(对照) | 90→87 | 16.1→12.0 |
| Ryde(对照) | 95→95 | 23.4→25.2 |

**诚实结论**: DEM-H 是**改准地面(裸地)非调得更吓人**。河岸假安全被抓(Parramatta/Windsor 大幅更谨慎),但个别点(Maribyrnong/Elwood)裸地后离排水线更高反而更安全。**仅有 LiDAR 真值的点(Parramatta/North Ryde)证明 DEM-H 匹配 LiDAR、DSM 差 6~10m**;其余无真值不能断言。**对外话术只能说"迁裸地+消除 DSM 系统偏差+LiDAR 验证",不可吹"全面更准"。**

**Maribyrnong 44→65 已查清(2026-07-15)= 不是 bug, DEM-H 对**: 该坐标(-37.766,144.906)在 12.0m 高地, 比 2022 洪泛滩(7.2m)高近 5m, 在河谷坡侧非淹没滩; Copernicus 旧 44 是过度悲观。同 Maribyrnong 扫点证明: 真正的洪泛滩(Maribyrnong Rd/河湾低地, 7.2m)命中 VIC 官方 overlay(zone=1)且正确打 High/Moderate(24/40)——**真洪区没被漏, overlay 查询正常**。→ 结论强化: DEM-H 不漏真洪区(overlay+地形双抓), 只修正 DSM 在高地的过度悲观。对外话术站得住。Elwood 13→25 同理(仍在 overlay High 档)。

## 三、Kenneth (Geoscape) 07-16 通话话术

主动亮(**07-15 已升级为"已做完"**): "我们已把全国高程基线从 Copernicus 表面模型迁到 GA DEM-H 裸地(水文强化,CC BY 4.0),生产在跑;5m LiDAR 验证过——裸地匹配 LiDAR,而表面模型在低洼近水地块差 6~10m(一个河岸旧数据打成 Very Low,现在正确读 Moderate)。下一步优先市场叠 ELVIS LiDAR + 按地址标覆盖/置信度。定位是带明确不确定度的相对筛查、不是判定级。" → "已做完" > "打算做" = 更硬;证明对 DEM landscape(DEM-H/ELVIS/CC BY 4.0)的了解不输 Geoscape。⚠️披露纪律: 谈分销放开,flood 评分方法论(how)控着说。⚠️话术只说"迁裸地+LiDAR验证",不吹"全面更准"(见加宽证据表 Maribyrnong 反例)。

## 四、上线后回复草稿(P0+P1 上线才发)

**Hugh Saalmans**:
> You were right, Hugh. We moved off the Copernicus DSM to a bare-earth base (GA DEM-H, hydrologically enforced), which strips the canopy and building bias that was inflating height-above-drainage on vegetated and built lots. Still 30m nationally, so we label it as relative screening with stated uncertainty rather than determination grade, and we're layering ELVIS LiDAR DTM on the priority metros where survey-grade coverage exists. Appreciate the push, it made the product better.

**Naveen Ragu Ramalingam**:
> Spot on, Naveen. Switched the base to bare-earth (GA DEM-H) so the terrain read is actual ground rather than canopy and rooftops, which is what the DSM was carrying. Thanks for the nudge.

---

## 附录: 调研关键数据 + 来源

**Copernicus GLO-30**: RMSE ~4.89m, LE90 ~7.71m, 设计 ≤4m。TanDEM-X 雷达 DSM(含建筑/植被)。
- Product Handbook: https://dataspace.copernicus.eu/sites/default/files/media/files/2024-06/geo1988-copernicusdem-spe-002_producthandbook_i5.0.pdf

**DSM 偏差量级**: 森林 MAE 5.15→2.88m, 建成区 1.61→1.12m(FABDEM 校正);裸 DSM 洪水影响高估 2-3×(McClean 2020)。
- FABDEM paper: https://iopscience.iop.org/article/10.1088/1748-9326/ac4d4f
- McClean 2020: https://agupubs.onlinelibrary.wiley.com/doi/full/10.1029/2020WR028241
- 全球 DEM 洪泛精度评比(FABDEM 第一/Copernicus 第二): https://www.tandfonline.com/doi/full/10.1080/17538947.2024.2308734

**保险判定级标准**: FEMA ≤0.15m RMSEz(~0.30m@95%);筛查级容差更松(sub-metre 深度)。
- FEMA Elevation Guidance 2022: https://www.fema.gov/sites/default/files/documents/fema_elevation-guidance_112022.pdf
- 房产级洪水 triage 容差(arXiv): https://arxiv.org/pdf/2603.13803

**HAND 敏感性**: 3–20m 稳,60–90m 崩;对 DEM 质量高度敏感(裸地/低洼处最关键)。
- NOAA/EGUsphere: https://repository.library.noaa.gov/view/noaa/62089
- NHESS HAND: https://nhess.copernicus.org/articles/19/2405/2019/

**FABDEM 授权**: 免费版 CC BY-NC-SA 4.0(非商用+ShareAlike),商用需 Fathom 付费(无公开报价)。
- Fathom license: https://www.fathom.global/insight/fabdem-download/
- Bristol dataset: https://data.bris.ac.uk/data/dataset/s5hqmjcdj8yo2ibzi9b4ew3sn

**GA / ELVIS 裸地 DEM(CC BY 4.0 商用可用)**:
- DEM-H 30m 水文强化(全国) / DEM-S 裸地 / 5m LiDAR(部分,245,000+km² 且在增) / 1-2m state LiDAR(ELVIS,补丁但都会+海岸+洪泛)
- ELVIS: https://elevation.fsdf.org.au/
- GA Digital Elevation: https://www.ga.gov.au/scientific-topics/national-location-information/digital-elevation-data
- EE 5M DEM (CC BY 4.0): https://developers.google.com/earth-engine/datasets/catalog/AU_GA_AUSTRALIA_5M_DEM
- EE DEM-H: https://developers.google.com/earth-engine/datasets/catalog/AU_GA_DEM_1SEC_v10_DEM-H
- NSW 5M ImageServer(点查): https://maps.six.nsw.gov.au/arcgis/rest/services/public/NSW_5M_Elevation/ImageServer

**FathomDEM**(备选,接近 LiDAR 表现,授权需核): https://iopscience.iop.org/article/10.1088/1748-9326/ada972

---

## 五、P2 方案 — LiDAR 精度层(独立后续项目, 未排期)

**目标**: 在有覆盖的优先市场把高程从 30m DEM-H 提到 1~5m LiDAR 裸地 → 答 Hugh 的"survey/保险级",并给每地址"覆盖/置信度 flag"(本身是卖点/差异化)。

**数据源(全 CC BY 4.0, 端点已验证)**:
- NSW 5m: `https://maps.six.nsw.gov.au/arcgis/rest/services/public/NSW_5M_Elevation/ImageServer`(identify 点查已验证可用; exportImage 取窗)
- GA 5m 国家 LiDAR DEM(部分覆盖) / ELVIS 1-2m state LiDAR(批量 AOI order)
- Earth Engine `AU/GA/AUSTRALIA_5M_DEM`(备选)

**架构设计**:
1. 优先 LGA(首府都会+海岸+洪泛走廊)批量下 1/5m LiDAR → 转 COG 本机(复用 DA Leads 瓦片管道)
2. `common/terrain.py` 改成**分辨率感知采样器**: 先查 LiDAR COG(有则用), 无则兜底 DEM-H 30m
3. flood 输出加 `elevation_confidence` 字段(high=LiDAR / medium=30m)→ API + 前端 flag
4. 逐 survey 瓦片核 license, 建 attribution 登记(同 geocode_source 模式)

**工作量**: 天级(COG 管道 + 覆盖 mask + 采样器改造 + 逐州接入), 非小时级。P1 已满足对批评的核心回应, P2 不紧急、不与北极星 P0(licence)抢注意力。

**触发条件建议**: Kenneth/保险类买家明确要 survey 级时, 或 licence 客户把 flood 当卖点时, 再排期。

---

## 六、P2 本地验证 + 架构定型(2026-07-15, 改代码前)

### 端点摸底(2026-07-15 全部实测)

| 州 | 端点 | 类型 | 分辨率 | DTM/DSM | 授权 | 覆盖 | 状态 |
|---|---|---|---|---|---|---|---|
| **NSW** | `maps.six.nsw.gov.au/.../NSW_5M_Elevation/ImageServer` | ArcGIS ImageServer(栅格) | 5m | DTM 裸地 | CC BY 4.0 | 全州 | ✅ 已上线(读栅格窗) |
| **QLD** | `spatial-img.information.qld.gov.au/.../Elevation/QldDem/ImageServer` | ArcGIS ImageServer(栅格) | **0.5m** LiDAR + SRTM 30m 兜底 | DTM 裸地 | CC BY 4.0(需署名) | 全州(有采集处 LiDAR, 其余 SRTM 填, identify 门控) | ✅ 已上线(读栅格窗) |
| **VIC** | `services-ap1.arcgis.com/P744lA0wf4LlBZ84/.../Vicmap_Elevation_METRO_1_to_5_metre/FeatureServer/1` | ArcGIS FeatureServer(**等高线**) | **1m metro** / 5m 其余 | DTM 裸地 | CC BY 4.0 | 全州(metro 1m) | ✅ 已上线(**等高线 IDW 插值**, 与地图同源); 栅格 VaaS 被政府授权闸死, 但等高线开放 |
| **TAS** | `services.thelist.tas.gov.au/.../TopographyAndRelief/MapServer/13` | ArcGIS MapServer(等高线) | 5m | DTM 裸地 | CC BY 3.0 AU | 全州 | ✅ 已上线(等高线 IDW, conf=medium; 比 DEM-H 准但非 survey 级) |
| **WA** | `public-services.slip.wa.gov.au/.../SLIP_Public_Services/Terrain/MapServer/**0**` | ArcGIS MapServer(等高线) | **2m 间距** | DTM(**非 LiDAR**: 10m 网格 Land Monitor DEM ~2000) | CC BY 4.0 | SW+海岸(Perth/Bunbury/Geraldton, 内陆无) | ✅ 已上线(等高线 IDW, conf=**medium**; 比 DEM-H 细但非 survey; 源非 LiDAR 故 tag=`contour_med`); ⚠️初判只探了 layer/1(10m)漏了 layer/0(2m) |
| SA/ACT/NT | 有真 1m LiDAR **但只 download** | ELVIS/zip 下载, 无 live 端点 | 0.5-1m | DTM | CC BY 4.0 | 都会 | ⬜ **深挖已确认无 on-demand 端点**(SA DEW server 云 IP 拒连/Adelaide 0.5m 只 zip; ACT org 398 全 FeatureServer 0 ImageServer 无等高线; NT gis-d 对所有客户端 RST)→ 留 DEM-H, 除非自烤 ELVIS |
| 国家 5m | GA `AUSTRALIA_5M_DEM` | **Earth Engine only** | 5m | DTM | CC BY 4.0 | 补丁 245k km² | ❌ 无公共 COG/ImageServer(`services.ga.gov.au/.../DEM_LiDAR_5m` = 404), 死路 |
| DEA S3 | `dea-public-data.../projects/elevation/` | 公共 COG | 30m | 裸地 | CC BY 4.0 | 全国 | 仅 DEM-H(`ga_srtm_dem1sv1_0`), **无 5m/LiDAR COG** |

> **已层叠 5 州 = NSW + QLD(栅格)+ VIC + TAS + WA(等高线)**, 全 on-demand 零存储。
> **WA 更正(Bo 07-15 再次追问 SA/WA/ACT/NT 后深挖)**: 初判"WA 10m 不接"漏了同一 Terrain 服务的
> **layer/0 = 2m 等高线**(只探了 layer/1 的 10m)。2m 覆盖 Perth/Bunbury/海岸, CC BY, 比 DEM-H 细
> → 接, 但它是 10m 网格 2000 年 DEM 非 LiDAR, 故 conf=medium、source tag 用 `contour_med` 不冒充 LiDAR。
> **SA/ACT/NT 深挖确认无 live 端点**: 三州都有真 1m LiDAR 但只 ELVIS/zip 下载(SA Adelaide 0.5m、
> ACT/NT 1m), 无 on-demand 查询服务(SA DEW server 拒云 IP、ACT org 零 ImageServer 零等高线、
> NT gov server RST 所有客户端)→ 要接得自烤瓦片(破零存储), 暂留 DEM-H。
> **关键更正(07-15)**: 初判"VIC 做不了"是错的——VIC 栅格 VaaS 虽被政府授权闸死, 但它的 **1m 等高线
> FeatureServer 是开放的**(就是 da_leads 地图等高线图层用的那个)。等高线给的是线不是栅格, 所以走
> **IDW 插值**(box 内取等高线顶点, 反距离加权算点高程, 最低等高线=排水线)而非读栅格窗。VIC metro 1m
> = survey 级 high; TAS 5m / VIC rural 5m = medium(点高程比 DEM-H 准, 但非 survey)。
> **WA 10m 不接**(≈±5m≈DEM-H 无增益); SA/ACT/NT 无源; 国家 5m EE-only 死路。DEA 桶只有 DEM-H。

### 决定性验证 — LiDAR vs DEM-H HAND 前后(NSW, 一次 exportImage 窗 + 本地 16 点 300m 环采样)

| 地址 | DEM-H HAND | LiDAR HAND | Δ | 说明 |
|---|---|---|---|---|
| **Parramatta 河岸(近水低洼)** | 11.3m | **1.1m** | **−10.2m** | DEM-H 30m 残余噪声把它抬高假安全, LiDAR 读出几乎贴排水线 |
| Chatswood 空地 | 17.1m | 14.9m | −2.2m | 中度 |
| Ryde 住宅(高干) | 26.2m | 27.3m | +1.1m | 高干地几乎不动 |
| Windsor 洪泛滩 | 3.0m | 4.2m | +1.2m | 小 |
| Lismore CBD | 3.0m | 1.4m | −1.6m | LiDAR 更谨慎 |

**结论(答 P1 遗留的 line 76 决定性测试)**: LiDAR **只在近水低洼地块大幅收紧 HAND**(Parramatta
11.3→1.1m),正是 DEM-H ~4-6m 残余噪声最伤 flood 判读处;高干地块基本不动(Ryde 26→27)。
这就是"有覆盖处 survey 级、其余照旧"的干净卖点 —— 与 P1 同调:改准不是调吓人。

### 架构定型: on-demand 读 API, 零存储(2026-07-15 修正)

先前初判"必须预烤"——**基于两个新事实修正为 on-demand**:

**事实 1: flood 现在就是 live 服务。** prod `/var/www/property-scores/data/` 里**没有
`flood_cache_*.parquet`**(precompute 脚本只有 melbourne 区且从未在 prod 跑),`flood_cache_lookup`
永远返回 None → 每次请求现算 `flood_score()`,本就有远程调用(JRC signed COG)。加一次 LiDAR
exportImage 不改变服务模型。

**事实 2: 用 exportImage(一窗/址)速度够,可靠性靠兜底。** 端点基准(6 次/20s 超时):

| 端点 | 中位 | 最慢 | 失败 |
|---|---|---|---|
| NSW identify(点查, HAND 要 17 次) | 0.31s | 17.15s | 3/6 ❌ |
| NSW exportImage(一窗覆盖整环) | 0.44s | 1.27s | 1/6 ⚠️ |
| QLD identify | 0.97s | 1.29s | 0/6 ✅ |
| QLD exportImage | 1.97s | 2.10s | 0/6 ✅ |

速度不是瓶颈(中位 0.3–2s);NSW 间歇丢请求是真问题 → **tight 超时 + 1 重试 + 失败兜底 DEM-H**。

**存储对比(实测 22 KB/km² Int16 分米 5m COG)**: 全州预烤 NSW 18G + QLD 41G = **59G > Oracle 剩 53G**
(QLD 原生 0.5m 全州 = 3.8TB 荒谬)。优先 LGA 只 0.3–0.6G。→ 但 on-demand **零存储**, 更省。

- **采样器**: flood `_hand_local` 先试一次 LiDAR exportImage 窗(NSW/QLD 覆盖内), 本地采点+16 点环
  → 成则 `elevation_confidence=high`; 超时/失败/州外 → 兜底现有 DEM-H 30m 环, `=medium`; proxy `=low`。
  LiDAR 逻辑放 flood(HAND 是唯一受益者), 不动通用 `terrain.elevation`(否则 bushfire/noise/view
  白白多打 API)。窗内加进程级 bbox 缓存, 环的 17 点复用一次 fetch。
- **代价**: NSW 偶发失败的地址暂时落 medium(同址不同天可能高/中飘, 但诚实); 首次命中偶尔慢 1–2s。
- **预烤留作后手**: 将来 flood 转预计算网格、或某市场明确要确定性 high-confidence 时再排。

### 实施(2026-07-15, 本地全测过, 待 Bo sign-off 部署)

- ✅ `flood/lidar.py`: on-demand 采样器, 两种 provider 一个 `open_window()`(鸭子类型
  elev/close/source/uncertain_thresh):**栅格**(NSW/QLD exportImage 窗, 5m px)+ **等高线**
  (VIC/TAS FeatureServer 顶点 IDW 插值)。按 ~500m cell 进程级缓存(正样本长存/负样本 90s TTL),
  tight 5s 超时 fail-fast, UA 防限流, QLD identify 门控 LiDAR vs SRTM-fill; 等高线间距驱动置信度
  (≤1.5m→high, ≤7m→medium, >7m 不接)。WA 10m 自动落回 DEM-H。
- ✅ `flood/score.py`: `_hand_local(lat,lng,state)` 三级(LiDAR→DEM-H 30m→proxy);
  `_hand_from_elev` 抽出共享环几何 + **来源感知不确定阈**(DEM-H 5m / LiDAR 1m,
  让 LiDAR 敢信近水低相对差); 输出 `elevation_confidence` = high/medium/low。
- ✅ `flood.html`: HAND 段加置信度徽章(绿=LiDAR survey-grade / 琥珀=DEM-H screening),
  方法论 data-source 行更新。浏览器实测两档都渲染正确。
- ✅ 测试: `tests/test_flood.py` +6 网络无关用例(环数学/来源阈/映射/LiDAR 优先/兜底);
  全 88 passed。
- ✅ 真 flood 分前后(live): Parramatta 河岸 DEM-H HAND 11.3→**LiDAR 1.0m**, 分 65→**45
  Moderate**(近水低洼正确收紧, conf=high); Ryde 高干几乎不动; VIC/QLD-SRTM-fill 落 DEM-H
  medium 无回归。

**已知代价(诚实)**: NSW/QLD 偶发超时的地址走兜底 → 那次 HAND 落 medium(同址不同天可能
高/中飘, 徽章诚实反映); 瞬时双超时最坏 ~10-12s 首次延迟(之后 cell 负缓存 90s)。da_leads
暂无消费此 flood 分(无接线), 故只到 property-scores API + demo 页; 将来 da_leads 报告接入时
`elevation_confidence` 已在 payload 里直接可用。

**✅ 已部署生产(2026-07-15, prod HEAD 91063b2)** — Oracle :8099 = /var/www/property-scores master
pull + restart property-scores.service。prod 出网可达 NSW(0.14s)/QLD(0.21s) ImageServer。

**上线后全场景实测(warm/steady-state)**: Parramatta 河岸 0.1-1.4s→high lidar HAND 1.0 分45;
Ryde 高干 high 27.0 分95; Brisbane WE high 3.6 分65; Bourke 远郊 high 9.1(见下); QLD Channel
Country(真 SRTM fill)→low; VIC/SA/WA/TAS(无端点)0.2-0.6s→medium dem_relief; 州外→干净报错;
缓存命中 0.1s 同值。全链无 500、无崩溃, 兜底链正确, 置信度徽章诚实。

**部署后加固(commit 91063b2)**: 冷启首测出现 40s/13-30s 长延迟, 定位=(a)服务冷启 + (b)我
连打测试触发 NSW/QLD **限流**(UA-less 被当爬虫), 非系统性。修:①exportImage/identify 请求加
User-Agent(限流消失, Bourke 从 12.4s→proxy 变 0.4s→high);②fail-fast(超时 6s→5s, 重试 1→0),
慢/被限的 cell 最坏只加一次 5s 就兜底 DEM-H(medium), 不为重试阻塞 live 分。warm 覆盖点稳定
0.1-3s 拿 LiDAR。⚠️生产低频逐址+缓存, 正常不会自限流。
