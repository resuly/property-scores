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
