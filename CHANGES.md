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

## 改了哪些文件

`git diff master..defect-fixes --stat`（master 自本分支切出后**没有**推进，
仍在 `275cf4da`，所以这个基线是准的）：

```
 property_scores/api/static/noise.html |   2 +-
 property_scores/common/overture.py    |  81 ++++++++++++++++++++------
 property_scores/noise/debug.py        |   9 ++-
 property_scores/noise/score.py        |  40 +++++++++++--
 scripts/export_noise_grid_csv.py      |  33 ++++++++++-
 tests/test_aadt_source_labels.py      | 106 ++++++++++++++++++++++++++++++++++
 6 files changed, 240 insertions(+), 31 deletions(-)
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
- 单测 `tests/test_aadt_source_labels.py` 13 条全过；并**逐条注入缺陷验证会红**：
  ① nsw 改回 vicroads ② `_source_state` 改成信上游 state 列
  ③ 删掉 tfnsw 的署名块 ④ ACT 强制返回 NSW —— 各自打红对应用例，还原后全绿。
- 全量测试：`pytest tests/ --ignore=tests/test_noise.py` → 103 passed, 7 skipped。
  `tests/test_noise.py` 在本机无法收集（缺 `rasterio`），**已确认 master 上同样如此，
  是既有环境问题不是本分支引入**；服务器上没装 pytest，故该文件本轮未能跑。

### 未验证 / 需注意
- `tests/test_noise.py` 本轮没跑（见上）。它里面有一处 `monkeypatch` 把 `aadt_near`
  换成返回 `[]`，与元组长度无关，预计不受影响，但没实跑过。
- 生产缓存中 76 条 `dominant_road` 标签错误（见运营日志），本分支不动生产数据。
