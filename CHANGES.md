# 本对话改动说明

## 基本信息
- 分支名: surface-layers
- 从哪切的: master
- worktree 路径: /Users/bwwan3/Documents/GitHub/property-scores-surface-layers
- demo 端口(如有): 8098（本地测试用；正式端口不变，仍是 8099）

## 这个对话做了什么(功能说明)

给评分服务加了一个**噪声面网格端点** `GET /scores/noise/surface?lat&lng&radius&cells`，
返回一个地址周边的建模 Lden 网格（默认 7×7 = 49 个节点，覆盖 ±1500m）。

这是给 DA Leads 商业 API 的 `?include_surfaces=1` 用的（对应分支 `da_leads/surface-layers`），
客户 Foundit 要"网站为一个地址画的一切都进 API"。

**为什么网格要在这个服务里算，而不是让 da_leads 逐点调 `/scores/noise`：**

1. `/scores/noise` 有 30 次/分钟 per-IP 限流，一个 49 点的网格必然 429。
2. 更重要的是模型路径。`noise/transfer.py:35` 的 `_DATA_DIR` 是按自己文件的相对位置
   解析 parquet 输入的，**完全无视 `DATA_DIR` 环境变量**。任何非部署目录的 checkout
   （包括 git worktree）都读不到 `overture_roads.parquet`，`transfer_lden` 抛异常，
   `noise_score` 就**静默落回 physics 链**。本轮开工时当场踩到：同一坐标本机
   `65.5 physics` / 生产 `65.3 transfer`，而 `NOISE_TRANSFER=1` 和 `transfer._load()`
   都是正常的——env 全对，数据路径断了，任何检查 env 的 preflight 都发现不了。
   把网格放在拥有模型的进程里算，这条路就结构性关掉了。

端点同时**自己断言跑对了模型**：逐节点记录 `lden_source`，输出 `model_path`（计数）、
`model_path_expected`（由 env 推出）、`model_path_as_configured`（实际是否全部走了配置的那条）。
**关键：均匀 ≠ 正确**——落回 physics 的网格是完整、均匀、数值合理的，`model_path_uniform`
会是 True，所以判据必须是"和配置的模型一致"。da_leads 侧收到 `False` 会直接丢掉整层。

其他实现要点：
- **不走预烤网格缓存**（`noise.cache.lookup`）。那个是 220m quincunx 面积均值，
  且会拿最远 150m 外的节点回答，和 `scores.noise` 的点值是两个量（墨尔本内城同一坐标
  实测差 6.1 dB）。改走 `noise_score()`，它底下仍有 24h 结果缓存。
- 6 路并发（生产 2 worker / MemoryMax=1500M；实测 8 并发冷点 3.0s、24 并发 5.5s 且
  RSS 涨 ~1.5GB，取 6 留余量），25s deadline，超时的节点给 null 并标 `partial`。
- 窗口按**每轴**换算米→度（经度按 cos(lat) 缩水），共用一个系数会把窗口做成椭圆。
- 行 0 = 北边，列 0 = 西边，和地图已经在消费的 landcover 网格同一朝向。
- 限流 20 次/分钟（一次调用是一整个网格，不是一个点）。

## 改了哪些文件(★Bo 合并时最看重这个)

```
 property_scores/api/main.py      |  31 +++++++
 property_scores/noise/surface.py | 180 +++++++++++++++++++++++++++++++++++++
 tests/test_noise_surface.py      | 187 +++++++++++++++++++++++++++++++++++++++
 3 files changed, 398 insertions(+)
```

- `property_scores/noise/surface.py` — 新增。网格生成 + 模型路径断言
- `property_scores/api/main.py` — 新增 `GET /scores/noise/surface` 路由
- `tests/test_noise_surface.py` — 新增，16 条

## 有没有要注意的

- **这个分支必须先于 da_leads 的 `surface-layers` 部署。** 端点不存在的话，da_leads 那边
  的 noise 层会**静默缺失**（不报错，就是那一层不出现在响应里）。
- 部署要 **`systemctl restart property-scores.service`**，不是 reload（长驻进程启动时
  就把代码载入内存了）。
- **纯新增，没有改任何既有代码路径。** `noise_score` / `noise_cache` / `transfer` 一行没动，
  既有端点行为逐字节不变，不需要 bump `NOISE_MODEL_VERSION`，不需要重烤任何东西。
- 已知遗留（本轮**没有**处理，建议单独排）：
  1. `NOISE_MODEL_VERSION` 不含 transfer 标志，所以预烤网格的版本闸门原则上分不出
     physics 烤的和 transfer 烤的。已核当前 `noise_cache_melbourne_inner.parquet` 的
     `model_version` 与运行时一致。加后缀要重烤全部预算格，是重活。
  2. `noise/transfer.py:35` 的 `_DATA_DIR` 无视 `DATA_DIR`（上面说的根因）。改它会动
     生产噪声模型的加载路径，属独立改动 + 独立 review。
- 本地测试若在 worktree 里跑，`data/` 需要逐项软链到主仓库的 `data/`，否则就会落回
  physics（正是上面那条根因）。

## 验证情况

- **本机 = 生产同模型路径已证明**：软链 data 后重启，六个跨州坐标逐点对拍生产
  （Brisbane ×2 / Hobart / Perth / Melbourne / Sydney），`lden_db` 和 `lden_source`
  **逐位一致，全部 transfer**。之后的实测才算数。
- 端点实跑：Brisbane CBD 7×7 网格，49/49 节点全部 transfer，冷跑 10.0s（本机单 worker），
  值域 55–80dB，中心节点 65.3 与该点 `/scores` 的 `lden_db` 一致。
- 经 da_leads `:8010` 端到端跑完 12 个 sandbox 地址（全八州），**12/12 网格满格 49/49
  且全部 transfer**。
- 测试 16 条全绿；**变异测试**故意注入 13 个 bug 逐个确认变红（模型路径断言摘除/换成
  均匀性/恒 True、每轴度数缩放退回共用系数、南北翻转、东西翻转、bbox 经纬互换、
  cells/radius 不钳制、格距算错、缺失节点不计数、`estimated_db` 兜底删除）。
  过程中抓到一条**永远不会失败的测试**并修了：朝向测试原本让 stub 原样返回自己的 lat，
  但网格对数值 round 到 1 位小数，3km 窗口内所有行都被舍成同一个 -37.8，南北翻转照样全绿。
- 全量回归：`162 passed`。
- ⚠️ 未验证：生产机上的真实并发表现（本机是单 worker）；生产部署后需实跑一次
  sandbox 地址确认 `model_path_as_configured: true`。

## ★ 独立 review 后的修改（第二轮）

review 在本 repo 侧抓出 **1 个 blocker**，已修：

**模型路径保证没闭合。** 原来的 `_EXPECTED_PATH = "transfer" if _TRANSFER_ENABLED
else "physics"` 是**用本进程自己的配置去判断本进程的配置对不对**，只防得住
"配置了但静默失败"，防不住"压根没配"。reviewer 实测：`NOISE_TRANSFER` 未设时，
physics 网格带着 `model_path_as_configured=True` + `model_path_uniform=True` 全绿出货，
**所有诚实字段互相印证，且全部是错的**。

修法是让期望**从进程外部来**，两条腿：

1. **`require_path` 由调用方给。** 商业 API pin `"transfer"`（那是生产在跑的），
   评分服务不再自证。`model_path_expected_source` 报期望到底来自 caller 还是 process_env，
   且 **不认识的 require_path 会退回 env 并如实标 `process_env`**（原实现会一边退回
   一边仍标 `caller`，正好谎报这个字段存在的意义）。
2. **`transfer_inputs_ok()` 读文件不读环境变量**：`transfer._load()` + 真的去看
   `overture_roads.parquet` 存不存在、非不非空。这样"开关没开"和"开了但读不到数据"
   能分开——2026-08-05 那次事故恰恰是 env 全对而数据路径断了，任何查 env 的探针都会报全绿。

**实机验证**（同一份代码，起两个服务）：
- `NOISE_TRANSFER=1`：`model_path {'transfer': 25}`、`expected transfer`、
  `source caller`、`as_configured True`
- **未设 env**：不带 `require_path` 时自证版给 `expected physics` / `as_configured **True**`（全绿，错的）；
  带 `require_path=transfer` 时 `as_configured **False**`（抓到），
  且 `transfer_inputs_ok True` 指明是"没开"而不是"读不到数据"
- 端到端：da_leads 侧收到 mismatch 后**整层不发**，`meta.surfaces.unavailable.noise.reason
  = model_path_mismatch`

顺带（H5 的上游一半）：`/scores/noise/surface` 的限流桶改成
**按调用方**（`X-Surface-Caller` 头，缺省退回 x-real-ip / client host），
桶名加 `surface:` 前缀与点端点隔离。否则 da_leads 所有客户共用 127.0.0.1 一个桶，
一个客户打满会让另一个客户**静默丢掉整个噪声层**。

**测试**：16 → **19 条**。第二轮变异注入 10 个 bug，1 个存活后补了断言：
`transfer_inputs_ok` 的探针测试原本两个断言都指向同一个不存在的目录，
路径检查先失败把模型加载检查完全遮住（删掉 `_load` 检查照样全绿）；
改成用 `tmp_path` 造真目录逐个隔离三种失败模式，再补一条"全都在 → True"
防止探针恒 False 也算通过。

**回归**：`165 passed`。

---

- 分支名: defect-fixes
- 从哪切的: master（本仓库默认分支是 master，不是 main）
- worktree 路径: /Users/bwwan3/Documents/GitHub/property-scores-defect-fixes
- demo 端口: 无（本仓库无本地前端；验证在生产服务器 /tmp/ps-defect-fixes 只读跑）
- 配套改动: da_leads 仓库同名分支 `defect-fixes`

## 这个对话做了什么（功能说明）

修 Foundit 缺陷清单里两条与「交通数据来源标签」相关的问题。

**1. 全国交通计数器被一律标成维州（vicroads）**

`aadt_near()` 用 glob 一次读进 5 个州的 `data/aadt_*.parquet`，但
`noise/score.py:658` 在每一行上硬编码 `"source": "vicroads"`。结果：悉尼
Pacific Highway 上一个 Transport for NSW 的计数器，对外发布成 VicRoads 的数据。
五个州里四个标错。

这不只是标签难看——`scripts/export_noise_grid_csv.py` 正是**按这个标签挑
CC-BY 署名块**的，所以对外发布的噪声网格 CSV 会把 TfNSW / QLD TMR / SA DIT /
Main Roads WA 的数据署成维州政府的。而且当某次导出没有公布任何街道名时，
署名代码走 `used_src = name_src or {"vicroads"}` 这个静默兜底，直接凭空署给
VicRoads。

改法：标签跟着数据走。`aadt_near()` 用 DuckDB 的 `filename` 列拿到行来自哪个
parquet，经 `AADT_SOURCE_BY_STATE` 映射成真实发布机构；返回值从 6 元组变 7 元组。
未登记的州得到 `aadt_<state>`，该标签**故意不在**导出脚本的 `_AADT_LICENSOR` 里，
所以新加一个州却忘了登记许可方时，导出会报错拒绝出文件，而不是悄悄署错人。

**2. ACT 的计数器被报成 NSW**

NFDH 全国文件的 `state` 列记的是**上报机构的辖区**，不是计数器的位置：ACT 境内
15 行（Majura Parkway / Federal Hwy，clientid `nswwim`/`nswrms`）全部写着 NSW。
另有 22 行在 NSW/QLD、NSW/VIC 边界上同样错位。

改法：新增 `_source_state(src_lat, src_lng)`，一律用**数据源自己的坐标**过
`detect_state()` 算，永不读上游 state 列。所有道路源（州 AADT / NFDH / Overture）
都带上 `source_state`。

## ⚠️ 独立 review 抓出两个 blocking 缺陷（已修，务必看这段）

第一轮提交完之后派了独立 review，**抓出两个会直接搞坏生产的缺陷**，两个都是我自己的
测试完全看不见的。**如果只看第一轮的 commit，这个分支是不能合的。**

| # | 缺陷 | 后果 |
|---|------|------|
| 1 | `score.py:728` 的 `measured_distances` 仍按 6 元组解包 | **凡是附近有实测计数器的点，`noise_score()` 直接 ValueError** —— 正好是本分支的主题场景。生产实测：Richmond(32条)/Pacific Hwy/Brisbane/Adelaide/Perth 全崩，只有零计数器的点能活 |
| 2 | 导出脚本把 `dominant_road.source` 无条件当发布方 | 但附近没计数器时该字段是 `"overture"`，不在许可表里 → **每一份没有计数器的网格导出都会报错拒绝出文件**。旧代码只是「碰巧安全」（Overture 行没有 road_name） |

**缺陷 1 的成因值得单独记**：这个修复我**第一轮就写对了**，然后自己把它删了 ——
做缺陷注入验证时用 `git checkout <file>` 还原注入的改动，把**旁边那个还没 commit 的
真修复一起还原掉了**。同一个错误我在 da_leads 那边犯过并且已经写进日志，结果在
property-scores 又犯了一次，而且这次**带进了一个 commit message 里声称修好了它的 commit**
（`6af87ff` 三条声明里有两条是假的：Overture 的 source_state、CLI 文案，也是这么没的）。

教训已固化：**注入验证前先 commit**；`git checkout` 不是「撤销我刚才那条 sed」的工具。

### 连带修的（同为 review 发现）

- `scripts/experiment_retrain_noise.py:49,60` 也是 6 元组解包，第一轮漏了。
- 导出脚本的许可校验原本跑在 `write_docs` 里、**在 README.md 已经落盘之后**，
  拒绝路径会留下一份没有署名的半成品。已挪到 `export()` 的其他 ship-blocking 校验旁边。
- 「没有任何实测发布方」现在**不再报错**：那是合法情况（市中心 500m 网格可能一个计数器
  都没有），此时署名块如实写「本次导出没有实测计数器数据覆盖，车流量由 Overture
  道路等级建模」，而不是拒绝出文件（会挡住真实业务）也不是瞎署给 VicRoads。
- 两处数字被 review 证伪，已按实测改正：缓存「~8k」实为 **16,628 行 / 15,712 条在 24h
  TTL 内 / 其中 8,037 条带 vicroads 标签**；「另有 22 行在 NSW/QLD 和 NSW/VIC 边界」实为
  那 22 行里只有 20 行在这两组边界上（另 2 行是 VIC/SA）。还删掉了「曾经有个 src1」这个
  我编出来的说法 —— 签名里从来没有过这个 token。

### 测试也按 review 重写了

原来那套**纯函数测试对这两个 blocker 完全免疫**，更糟的是：把最初那个
`"source": "vicroads"` 硬编码原样改回去，**整套测试照样全绿** —— 也就是说它根本没在
守本分支要修的东西。现在补了：

- **集成层测试**：用 stub DB 驱动真正的 `noise_score()`，元组形状变了、标签被写死、
  Overture 行少了 `source_state`，都会红。
- **collection 层测试**：`_measured_publishers()` 直接被测，而不是只静态检查许可表 ——
  blocker 2 恰恰发生在收集那一步，静态检查看不见。
- 许可表测试**不再做源码字符串匹配**：review 把整个 VicRoads 许可块删掉，旧测试
  照样绿（因为 `"vicroads"` 这个字符串在同文件的注释里还出现两次）。现在比对字典本身。

六种注入（两个 blocker + 原始硬编码 + 删许可块 + 删 source_state + 空集合改回报错）
逐条验证会红，还原后全绿。

## 第二轮 review：我的修复本身又被抓出两个 blocking

按「修 review 意见 ≠ 修完了」的规矩，对**上面那轮修复本身**又派了一次独立 review。
**又抓出两个 blocking** —— 都是我修第一轮 blocker 时新引入的。

**B3｜我把「崩溃」换成了「假话」。** 署名集合是从 `dominant_road.source` 收的，
但 `dominant_road` 只是**最响的那个源**。实测计数器可以参与算出这一格的分贝值却不是最响的，
于是**它就没人署名了**。生产实测（Brisbane CBD 3×3）：
**9 个点全部有 `qld_tmr`/`nfdh` 计数器参与，而 9 个点的 `dominant_road.source` 全是
`overture`** —— 也就是说导出文件会在一份**建立在 CC BY 计数器数据之上**的网格上，
白纸黑字写「本次导出没有实测计数器数据覆盖」。**少署名正是这整块代码存在的唯一目的。**
（修 blocker 2 之前是直接报错，虽然烦人但从没发出过假声明；我把它改成了会发假声明。）

修法：`noise_score()` 新增 `measured_traffic_sources` —— **所有**参与该点计算的实测发布方
（取自 `aadt_levels`，结构上就不含 Overture）。导出按它署名，不再看谁最响。

**B4｜那个「遇到未登记发布方就拒绝出文件」的护栏根本不会触发。**
因为 `_measured_publishers()` 已经先把集合过滤成「已登记的」了，`unknown` 恒为空 ——
护栏是装饰品，而且和我自己写在 `overture.py` 里的 docstring 直接矛盾。
唯一覆盖它的测试是直接塞 `facts` 进去的，**绕开了收集那一步**（正是隔壁那条测试专门
警告过的形状）。修法：`_measured_publishers` 不再过滤，只按名字剔掉 `overture`
（它在 ODbL 那块单独署名）；测试改成**从收集器驱动**。

### 同轮一并修的

- `write_docs` 里还留着一份**重复的 `_AADT_LICENSOR`**（18 行）：文本已是死代码，
  但它的 keys 还在被用 —— 两份可以静默漂移。已删，并加测试钉死「全文件只能有一份」。
- **vintage 闸门仍在 CSV 落盘之后才报错**，拒绝路径会留下没有 README 的孤儿 CSV。
  已抽成 `_require_vintage()` 并挪到其他 ship-blocking 校验旁边。
  生产实测：QLD 导出**报错且一个文件都没写**。
- 记录了一个**本轮不修但要知道**的事实：`_VINTAGE` **只有 VIC**，所以 NSW/QLD/SA/WA
  的导出目前一律出不去 —— 新加的四个州许可条目**在维州以外还无法端到端跑通**。
  要放开得先跟各发布方核实调查年份（不该我拍脑袋填）。已写进 `_require_vintage` docstring。

## ⚠️ 还误删过两个与本分支无关的文件（已还原）

三样东西**曾被本分支删掉**，master 上一直有：
`property_scores/noise/surface.py`、`tests/test_noise_surface.py`（258 行），
以及 `property_scores/api/main.py` 里**整个 `/scores/noise/surface` 线上端点**（40 行）
—— 模块、路由、测试**成套消失**，所以一条测试都不会红。是上面那个「注入验证还原」翻车的第三种变体：
备份用的 `cp -r` 静默失败没建出目标目录，还原步骤照跑 `rm -rf property_scores scripts tests`
却什么都没拷回来，我用 `git checkout .` 抢救 —— 但那之前的某次 `git add -A` 已经把删除
暂存了，于是它们「被还原成了删除状态」。

**测试套件对此毫无反应**（成套没了，自然没有失败）。发现过程本身也是个教训：
先用 `--diff-filter=D` 只查到那两个文件，**端点那 40 行没查出来**（文件还在，只是内容被删），
是把 `--stat` 整张扫一遍才看见的。三样都已从 master 原样还原。

> 教训不是「小心用 git checkout」，是：**还原不要靠删除**；
> **在宣称改动集是我想改的那些之前，跟基线分支把整张 `--stat` 和所有被删掉的行扫一遍**
> —— 只查「被删的文件」不够，被掏空的文件查不出来。
> 收尾时我把 `git diff master..defect-fixes` 里**每一条 `-` 行**都过了一遍，
> 现在全部是有意为之。Bo 合并前也建议扫一眼这张表。
>
> da_leads 那边同样查过：**零误删**，改动清单就是预期的 7 个文件。

## 改了哪些文件

`git diff master..defect-fixes --stat`（master 自本分支切出后**没有**推进，
仍在 `275cf4da`，所以这个基线是准的）：

```
 CHANGES.md                            | 318 ++++++++++++++++++++-------------
 property_scores/api/static/noise.html |   2 +-
 property_scores/common/overture.py    |  88 +++++++---
 property_scores/noise/debug.py        |   9 +-
 property_scores/noise/score.py        |  63 ++++++-
 scripts/eis_aadt_diagnose.py          |   2 +-
 scripts/experiment_retrain_noise.py   |   4 +-
 scripts/export_noise_grid_csv.py      | 152 ++++++++++++----
 tests/test_aadt_source_labels.py      | 321 ++++++++++++++++++++++++++++++++++
 9 files changed, 774 insertions(+), 185 deletions(-)
```

合并风险：低。master 未推进，无重叠改动。

## 有没有要注意的

- **`aadt_near()` 返回值从 6 元组变 7 元组**。仓库内所有消费方已逐个查过并改完；
  其余消费方（`scripts/experiment_retrain_noise.py`、`scripts/eis_aadt_diagnose.py`、
  `scripts/archive/noise/train_noise_model.py`）用的是下标取值（`s[3]`、`a[3]`、
  `seg[0]`），元组变长不影响。
- **API 响应新增 `source_state` 字段**（`dominant_road` 与各 road source 内）。
  纯新增，不改动已有字段。
- **噪声结果缓存签名新增 `src2` 标记**（`score.py` 的 `_CONFIG_SIG`）。这是刻意的：
  改动只变标签不变数字，原签名看不见，不 bump 的话已缓存的 ~16.6k 条会继续用旧标签
  再撑 24 小时。bump 之后缓存自动作废重算，**所以缓存清理脚本不是正确性必需**，
  只是想早点回收空间时用（清理方案见运营日志）。
- **导出脚本的行为从「兜底」改成「报错」**：没有任何可识别的 AADT 发布方时，
  `export_noise_grid_csv.py` 现在抛 RuntimeError 而不是署名给 VicRoads。这是有意的
  ——署错许可方比出不了文件严重。
- **未部署、未合并、未 push**。

## 验证情况

在生产服务器 `/tmp/ps-defect-fixes`（代码=本分支，`data/` 软链到生产真实 parquet）
只读跑，未部署、未重启任何服务：

- `aadt_near()` 逐州标签实测：NSW→tfnsw、VIC→vicroads、QLD→qld_tmr、
  SA→sa_dit、WA→mrwa，各点均只出一个来源，无混标。
- `noise_score()` 端到端：
  - 缺陷原始地址（-33.75457,151.15077，Pacific Highway）
    修前 `source: "vicroads"` → 修后 `source: "tfnsw"`, `source_state: "NSW"`。
  - ACT Majura Parkway（-35.2139,149.1880）：`state: "ACT"`,
    `dominant_road.source_state: "ACT"`（上游 NFDH 该行写的是 NSW）。
- `noise_debug()` 路径同样实测通过（该文件也有同一处硬编码）。
- **发现并修掉一个单测抓不到的连带 bug**：`score.py` 里 `measured_distances`
  仍按 6 元组解包，凡是有实测 AADT 覆盖的点全部报错。是「跑真东西」才暴露的，
  不是单测发现的。
- **修完 blocker 后的生产实测**（隔离环境 `/tmp/ps-v2`，`DATA_DIR` 指向只含 parquet 软链、
  **不含共享缓存**的私有目录，所以没往生产缓存写一个字节 —— review 特别提醒过这一点）：
  9 个点（含 VIC Richmond 32 条实测、NSW/QLD/SA/WA/ACT/TAS/NT）**全部正常出分**，
  标签逐州正确，`source_state` 每个 dominant_road 都有。
- **导出脚本两种情形都真跑了**：
  - 墨尔本 CBD（0 个计数器，就是原本会报错的情形）→ 正常出 3 个文件，署名块写
    「本次导出没有实测计数器数据覆盖，车流量由 Overture 道路等级建模」
  - Fitzroy（43 条实测）→ 正常出文件，署名 VicRoads（该点在维州，正确）
- 单测 23 条全过；并**逐条注入 6 种缺陷验证会红**（两个 blocker + 原始 vicroads 硬编码 +
  删整个 VicRoads 许可块 + 删 Overture 的 source_state + 空发布方集合改回报错），
  还原后全绿。
- 全量测试：`pytest tests/ --ignore=tests/test_noise.py` → **131 passed / 1 failed / 7 skipped**。
  唯一失败是 `test_noise_surface.py::test_transfer_inputs_probe_reads_files_not_the_environment`
  （本机缺 `rasterio`），**已确认 master 上逐字相同**，与本分支无关。
  `tests/test_noise.py` 在本机无法收集（缺 `rasterio`），**已确认 master 上同样如此，
  是既有环境问题不是本分支引入**；服务器上没装 pytest，故该文件本轮未能跑。

### 未验证 / 需注意
- `tests/test_noise.py` 本轮没跑（见上）。它里面有一处 `monkeypatch` 把 `aadt_near`
  换成返回 `[]`，与元组长度无关，预计不受影响，但没实跑过。
- 生产缓存中 76 条 `dominant_road` 标签错误（见运营日志），本分支不动生产数据。
