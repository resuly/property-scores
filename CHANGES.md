# 本对话改动说明

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
