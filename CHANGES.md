# 本对话改动说明

## 2026-08-25 API 服务进程管理从 uvicorn multiprocess 换成 gunicorn+UvicornWorker

### 问题（生产实证，2026-08-25 调研闭环）

uvicorn 0.30+ 的 multiprocess supervisor 每 0.5s 对 worker 做 pipe ping，
`timeout_worker_healthcheck` 默认 5s 收不到 pong 就对 worker `process.kill()`
（SIGKILL）。生产上从 8-19 起 worker 每 ~7.4h 被静默击杀一次（journal 只有
"Child process died"，无 traceback、无 core、无内核记录），三次死亡前 90 秒
journal 全是 noise 密集请求。机制：noise 冷请求单次 ~7.6s 重计算 + cgroup 骑上
memory.high(3200M) 后分配路径被内核 direct-reclaim 限速（拿着 GIL 卡顿），
pong 线程被饿死超过 5s。被杀瞬间在手的请求直接断连，重启后首个 noise 请求
又要 7.6s 冷加载，正好都落在请求高峰。另一半问题是 worker RSS 无限增长
（实测 +130MB/天，uvicorn 无回收机制），是 cgroup 爬上 memory.high 的根因。

### 改法

- `gunicorn_conf.py`（新增）：2 workers、`uvicorn_worker.UvicornWorker`、
  `max_requests 500 + jitter(0..100)`（gunicorn jitter 只加不减，实际
  500-600 请求即约 16.7-20h graceful 回收一次，两 worker 相位错开）、
  `accesslog "-"`（gunicorn 默认丢弃请求行，而 journal 请求行正是本次
  破案的证据链）、`timeout 60`（超时 arbiter 报 `WORKER TIMEOUT` + SIGABRT，
  有定性与 pid；`post_worker_init` 在 Gunicorn 重置信号后启用
  `faulthandler(all_threads=True)`，SIGABRT 会留下所有 Python 线程现场）、
  `control_socket_disable=True`（systemd-only 管理，不开放未使用的 gunicornc
  control socket）、不 preload
  （rf.pkl 是懒加载，preload 共享不到）。参数依据全部写在该文件注释里。
- `pyproject.toml`：`[api]` extra 要求 `gunicorn>=26.2`、`uvicorn-worker>=0.4`。
  实测组合：本地 gunicorn 26.2.0 + uvicorn-worker 0.4.0 + uvicorn 0.45.0；
  生产机隔离端口 uvicorn 0.47.0（回收行为在两个版本上都验证过）。
- 更简单的替代方案存在且被权衡过：uvicorn 0.45+ 自带
  `--timeout-worker-healthcheck`（一个 flag 消灭 5s 误杀）+ systemd
  `RuntimeMaxSec` 低峰整体重启管内存。gunicorn 方案的真实增量是：回收无
  listen 空窗（worker 逐个换）、僵死有 `WORKER TIMEOUT` 定性日志、避免
  整体 restart 后两个 worker 同时 7.6s 冷载。按此取舍选了 gunicorn。

### 部署影响（部署前必读）

- 生产 unit `ExecStart` 需改为
  `/var/www/property-scores/.venv/bin/gunicorn property_scores.api.main:app -c /var/www/property-scores/gunicorn_conf.py`（conf 用绝对路径，不依赖 WorkingDirectory）
  （unit 不入库；改前备份已存在 `~/property-scores.service.bak-20260825`）。
- 服务器 venv 需 `pip install 'gunicorn>=26.2' 'uvicorn-worker>=0.4'`。
- 输出数值零变化：不动模型、不动 env、不动版本串，restart 后 model_stamp 只随
  code hash 变（本改动不改 python 评分代码，code hash 不变）。
- 回滚：unit 恢复备份 + restart 即回到 uvicorn。

### 规则（新增，写在 score.py 的 NOISE_MODEL_VERSION 上方）

任何会改变 noise 输出数值的模型改动，必须二选一：**要么 bump
`NOISE_MODEL_VERSION` 里的日期段，要么改完立刻把所有区域的预计算格重烤**
（`scripts/precompute_noise.py`）。没有第三个选项，也没有“回头再烤”：在改动和
重烤之间，`cache.py` 会继续吐出版本串相同、数值不同的格点，全链路没有任何地方
会报警。

依据是 2026-08-04 的实证：melbourne-inner 的格是 06-12 烤的，07-26 的模型改进
既没动 env 也没 bump 版本，于是六周里地图 overlay 对外发的分数与实时模型最多差
3 分，而所有健康检查都是绿的。

### 配置这一半改成机制，不再靠人守规则

`NOISE_MODEL_VERSION` 原本只带 aadt / quiet / rail 三个开关的**有无**，盖不住
`NOISE_TRANSFER`、`NOISE_ML_CORRECTION`、`NOISE_RAIL_RECAL_DB`。也就是说一台
transfer 配置的服务，可以照单全收一份 physics 配置烤出来的格，两边版本串完全
相等。现在版本串按顺序拼接：

`2026-06-09-quincunx[-aadt{K}][-nswquiet][-nswrail{dB}][-transfer][-{模型id}][-ml]`

- 数值型 tunable 按**取值**入串（`-nswrail8` / `-aadt4`），不按开关入串：
  `NOISE_RAIL_RECAL_DB=8` 和 `=5` 是同一个开关下的两组分数，布尔后缀会让它们
  互相顶替。
- transfer 打开时还带上**实际解析到的模型 id**（`-eu.transfer.v1.dff726`：id 里的
  dash 换成点以免撞分隔符，后面接原始 id 的 6 位 blake2s，保证不同 id 不会因为
  只差标点而映射成同一个戳）。换模型是
  唯一一个任何 env 都看不见的改数值动作：`scripts/noise_model.py activate` 只改
  `registry.json` 然后重启，环境变量一个字都没变。所以这个 token 走
  `model_registry.resolve()`，与真正加载模型的是同一个来源；解析失败（例如
  `NOISE_MODEL_ID` 打错，registry 按设计直接 raise）落到 `-mdlerr`，不与健康的
  transfer 进程共用同一个戳。transfer 关闭时该 token 为空（物理路径根本不读 RF）。
- 每个后缀在对应 flag 关闭时为空串，所以**全默认配置下版本串与改动前逐字节相同**
  （`2026-06-09-quincunx`），默认配置的部署不会因为这次改动而失效重烤。

### 只收紧格子这一层，不牵动下游缓存

sqlite 结果缓存的 `_CONFIG_SIG` 改用**折叠前**的版本串组合
（`_CONFIG_SIG_VERSION`），因此**任何配置下 `_CONFIG_SIG` 与改动前逐字节相同**，
`api/stamp.py` 的 model stamp 也不变，DA Leads 按 stamp 缓存的 per-parcel 分数
不会被冲掉。理由：这次改动不改任何数值，只是收紧"哪些格子可以被采信"；让它
去冲结果缓存和下游 parcel 缓存，等于全网重算一遍得到完全相同的数字。
`_CONFIG_SIG` 本来就用自己的 t/m/r/k token 守着这些 env，不损失任何保护。

日期段本身是**同一个常量 `_MODEL_DATE`**，两个串共同消费：所以"为真实打分改动
bump 日期"必然同时作废预计算格、结果缓存和下游 model stamp，"只 bump 一半"在
代码里无法表达。第一版把日期写了两遍并在注释里请求后人保持同步，那种口头约定
正是 08-04 事故的成因，已被复审打掉。

### 部署影响（部署前必读）

生产 `property-scores.service` 带 `NOISE_TRANSFER=1 NOISE_QUIET_RECAL=1
NOISE_RAIL_RECAL=1`，版本串因此从
`2026-06-09-quincunx-nswquiet-nswrail` 变成
`2026-06-09-quincunx-nswquiet-nswrail8-transfer-eu.transfer.v1.dff726`
（本机对着生产同款 registry 实测得到的串）。

- **所有现存 `noise_cache_*.parquet` 在部署后一律判失效**，`cache.py` 跳过，
  overlay 每格退回实时计算（结果正确，只是慢：transfer 密集区 1.7-2.2s/点，
  原缓存 0.0015s/cell）。这正是这次改动要暴露的那批格。
- 除 transfer 外，只跑 `NOISE_AADT_ADJUST=1` 或 `NOISE_RAIL_RECAL=1` 的部署也会
  失效一次，因为这两个后缀由布尔改成了带取值（`-aadt`→`-aadt4`，
  `-nswrail`→`-nswrail8`）。只跑 `NOISE_QUIET_RECAL=1` 或全默认的不受影响。
- 因此部署必须与重烤配对，挑无重活窗口：先 `precompute_noise.py`（**带 service
  同款 env**，否则新烤的格照样对不上）再 restart，或接受一段实时计算期。
- 结果缓存与下游 parcel 缓存不受影响（见上一节）。

## 2026-08-13 — PlanSA Flooding Evidence Required 不再当作安全证据

- 将 PlanSA `Hazards (Flooding Evidence Required)` layer 403 纳入 SA 官方
  洪水检查。
- 该控制是“需要地块级证据”的审批触发器，不是洪水范围或严重度。命中现在保留在
  `flood_zones`，但对分数作中性贡献；不会再因没有命中其余 SA 洪水层而得到
  `checked_no_hit` 对应的 90 分安全贡献。
- `official_layer_note` 明确说明风险未知、需要补充洪水证据。已有 General / Coastal
  等严重度层命中时仍按原规则计分。
## 2026-08-13 — Newcastle 洪水危险图层退出商业评分输入

- Newcastle 的 `nsw_hazard_flood_newcastle` 已从商业 flood score 输入白名单移除。
  该图层不再改变 Newcastle 地址的评分；评分继续使用其余获准的卫星、地形、水系与州级依据。
  这是授权边界修正，不删除源数据，也不改变 DA Leads 自有地图的图层展示。

## 基本信息
- 分支名: scores-model-stamp-20260806
- 从哪切的: master (6c89a8a)
- worktree 路径: /Users/bwwan3/Documents/GitHub/property-scores(原地,未开 worktree)
- demo 端口(如有): 无

## 这个对话做了什么(功能说明)

给这个服务加一个 `model_stamp`:一串短哈希,只要本服务的打分结果会变,它就变。

背景在下游。DA Leads 把 `/scores` 的结果按 parcel 缓存长期复用(组件键
`scores:v7`,TTL 原本 90 天)。缓存里的分数不带任何"是谁算的"记录,所以我们这边
换模型、翻 flag、替换输入产物之后,已经缓存的地址会继续吐旧数字,而**下游无从
察觉**。原本唯一的失效手段是人去改 DA Leads 源码里那个版本字符串再发一次版——
那行注释自己写着"否则错值最多存活 90 天"。跨仓库、靠人记,这不是机制。

改法:本服务公布自己的构建指纹,下游拿它当缓存判据(指纹不一致 = miss)。

指纹的组成(`components()` 原样列出,便于事后解释"为什么全体失效了"):

| 项 | 来源 | 什么时候会动 |
|---|---|---|
| `code` | `PROPERTY_SCORES_REV` 环境变量 → **打分代码的内容哈希**(`property_scores/**/*.py`,排除 `api/static/`) | 打分代码真的改了 |
| `noise_config` | `noise.score._CONFIG_SIG`(直接引用,不重新推导) | NOISE_TRANSFER / ML / 各州重标定 flag 及其调参 |
| `noise_model` | `model_registry.resolve()` 的 `id@source` | registry activate,或 NOISE_MODEL_ID 覆盖 |
| 各输入产物 | registry.json / calibration.json 走**内容哈希**;rf.pkl(114MB) / WorldCover lc.vrt 走 size+mtime | 换产物(含"文件不存在"这个状态本身) |

**它覆盖不到什么**(stamp.py 头注释里写清了):在 .vrt 底下就地重写的栅格瓦片,
以及不改 size 和 mtime 的原地修改。换镶嵌图是部署动作,配重启即可由 `code` 项兜住。

## 改了哪些文件(★Bo 合并时最看重这个)
```
 CHANGES.md                     |  (本文件)
 property_scores/api/stamp.py   |  新增。指纹计算 + components() 明细
 property_scores/api/main.py    |  +GET /version;/scores 响应加 model_stamp;
                                |    startup 事件里把 code revision 钉死
 tests/test_stamp.py            |  新增 20 个用例
```
(准确清单跑 `git diff master..scores-model-stamp-20260806 --stat`)

## 有没有要注意的

- **指纹由运行中的进程算,不由部署脚本写文件。** 部署脚本只知道"我拷了什么",
  不知道服务重启没有、这台机器上 `NOISE_MODEL_ID` 有没有覆盖 registry、拷过去的
  产物是不是真被加载了。这三种情况的后果都跟"根本没有指纹"一样。

- **code 项是打分代码的内容哈希,不是 repo 的 git HEAD**(第一版是 HEAD,review
  打掉了)。本 repo 里 docs/、CHANGES.md、scripts/、tests/、api/static/*.html 跟
  打分模块放在一起,近期 git log 里全是 docs-only 和静态页 commit。按 HEAD 算等于
  "改个 README 就把下游全部 parcel 的缓存分数清空"——每个地址 9 个模型重算一遍,
  而那次改动根本动不了任何数字。同理用内容哈希而不是 mtime:rsync / 重新 clone
  重写了一个没变的文件,不该算变更。`git HEAD` 仍在 `/version` 的 `repo_head`
  里(只给人看,不进指纹)。

- **code revision 在 startup 解析,不是第一次用的时候。** 部署 = `git pull` 然后
  重启,两者之间有个窗口:工作树已是新 commit,跑着的还是旧代码。在这个窗口里惰性
  解析会报出新 commit,下游于是清空缓存、**用旧代码重算一遍、盖上新指纹**;等真
  重启了指纹没变,那批旧代码算出来的值就被永久信任。
  `test_startup_pins_the_code_revision_before_first_use` 盯这条。

- **`/scores` 响应自带 model_stamp**,不是只让下游 poll `/version`。`/version` 回答
  "现在是什么",响应字段回答"这份数据是谁算的"——下游要存的是后者。poll 和实际打分
  之间隔着时间,中间可能重启过,两个必须分开。

- **`/version` 只对内。** 生产绑 127.0.0.1,且不在 DA Leads proxy 的 endpoint 白
  名单里(`web/property_scores_proxy.py`),所以 components 里的产物大小和 commit id
  不会公开出去。

- **指纹排序后再哈希**,所以往 components 里加一项的位置不会平白把下游缓存全清掉。

- 不涉及依赖、迁移。新增一个**可选**环境变量 `PROPERTY_SCORES_REV`(不设也能跑,
  退到打分代码内容哈希)。

## 验证情况

- `python3 -m pytest -q --ignore=tests/test_noise.py` → **170 passed, 2 failed**。
  两条 failed 都与本改动无关,都对着 master 复核过:
  ①`test_noise_surface.py::test_transfer_inputs_probe_reads_files_not_the_environment`
  —— 这台机器没装 rasterio(`tests/test_noise.py` 因同一原因 import 不了,故 ignore);
  ②`test_aircraft_anef.py::test_lga_comes_only_from_the_victorian_source`
  —— 实时打 VicPlan 外网,间歇性红(单跑也红,重跑就绿)。
- 新增用例逐条"先弄坏验红":把哈希改成常量、把"产物缺失"和"产物存在"映射成同一个
  token、删掉 `/scores` 的 model_stamp 字段、去掉 startup 钉死那步、把 repo HEAD
  塞回指纹、把静态页放回哈希、把 JSON 产物改回 mtime——各自都能让对应用例变红。
- 本机真产物实测 `components()`(连续调用指纹稳定):
  `code=py:cfd556ce1104ebc8 / noise_config=2026-06-09-quincunx:src2:t0:m0:r-:k-: /
  noise_model=eu-transfer-v1@registry / noise_registry=sha:… / noise_calibration=sha:… /
  noise_rf 与 lc.vrt 走 size:mtime`。
- **生产只读实测**(把 stamp.py 拷到 Oracle 临时跑,跑完删掉、`git status` 干净):
  生产 env 是 `NOISE_TRANSFER=1 NOISE_QUIET_RECAL=1 NOISE_RAIL_RECAL=1`,
  `noise_config` 因此是 `…-nswquiet-nswrail:src2:t1:m0:r8:k-:`,与本机不同 ——
  这正是指纹要抓的那类差异(2026-08-03 噪声导出事故同源)。

## 下游配套 + 发布顺序

DA Leads 分支 `cache-purge-hook-20260806`:`web/scores_stamp.py` poll `/version`
(每 worker 5 分钟一次,不在请求路径上)、给缓存 payload 套指纹信封、指纹不符即
miss(`surf:*` 面数据同样套信封 —— 它的噪声网格也来自本服务),并把 `scores:v7`
的 TTL 从 90 天降到 31 天(不是 14:按月计费下同址当月重查免费,TTL 短于最长月份
等于白算九个模型)。

**先发本服务,后发 DA Leads —— 这是成本问题,不是正确性问题。** DA Leads 拿不到
指纹时 fail-open(照常吃缓存),顺序反了不会坏。但生产 886 行缓存**全是旧格式的裸
payload**,没有信封,所以无论什么顺序,DA Leads 一上线它们就全部作废重算
(140 条 `scores:*` + 13 条 `surf:*`,每条首次访问要重跑九个模型)。先发本服务只是
省掉**第二次**重算:否则那批会先被写成 `wrap(scores, None)`,等 `/version` 能答了
再作废一次。
