# 本对话改动说明

## 基本信息
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

## 改了哪些文件

`git diff master..defect-fixes --stat`（master 自本分支切出后**没有**推进，
仍在 `275cf4da`，所以这个基线是准的）：

```
 property_scores/api/static/noise.html |   2 +-
 property_scores/common/overture.py    |  88 ++++++++---
 property_scores/noise/debug.py        |   9 +-
 property_scores/noise/score.py        |  56 ++++++-
 scripts/eis_aadt_diagnose.py          |   2 +-
 scripts/experiment_retrain_noise.py   |   4 +-
 scripts/export_noise_grid_csv.py      | 108 +++++++++++--
 tests/test_aadt_source_labels.py      | 286 ++++++++++++++++++++++++++++++++++
 9 files changed, 612 insertions(+), 45 deletions(-)
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
- 单测 21 条全过；并**逐条注入 6 种缺陷验证会红**（两个 blocker + 原始 vicroads 硬编码 +
  删整个 VicRoads 许可块 + 删 Overture 的 source_state + 空发布方集合改回报错），
  还原后全绿。
- 全量测试：`pytest tests/ --ignore=tests/test_noise.py` → **111 passed, 7 skipped**。
  `tests/test_noise.py` 在本机无法收集（缺 `rasterio`），**已确认 master 上同样如此，
  是既有环境问题不是本分支引入**；服务器上没装 pytest，故该文件本轮未能跑。

### 未验证 / 需注意
- `tests/test_noise.py` 本轮没跑（见上）。它里面有一处 `monkeypatch` 把 `aadt_near`
  换成返回 `[]`，与元组长度无关，预计不受影响，但没实跑过。
- 生产缓存中 76 条 `dominant_road` 标签错误（见运营日志），本分支不动生产数据。
