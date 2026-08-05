# 本对话改动说明

## 基本信息
- 分支名: heat-island-coastal-fix
- 从哪切的: master
- worktree 路径: /Users/bwwan3/Documents/GitHub/property-scores-heat-island-coastal-fix
- demo 端口(如有): 无

## 这个对话做了什么(功能说明)
修生产活故障:滨水地址的 heat_island 分数恒返回 `score: null / "Data unavailable"`。

根因是 MODIS LST 1km 产品在源头做水体掩膜——凡是被判为水的 1km 像元整格写成
填充值。海边地址(DA Leads sandbox 的 1/1 Cavill Avenue, Surfers Paradise)正好
落在这种像元上,中心点采样得 NaN,整个热岛分数就没了。不是上游挂了,不是限流,
也不是我们的 bbox/日期窗口有 bug:同一次调用里,该地址 2km 窗口内还有 10 个
有数的陆地像元。

改法:中心像元有数时行为完全不变;中心像元没读数时,按环由近及远在 2km 内找有数
的像元,取最近那一环的均值(同距离多个像元求平均,避免固定方位偏好),夜间读数取自
同一批像元。这条路径不报 UHI,也不报 `modis_area_c`,payload 带
`lst_source` / `lst_offset_m` / `lst_pixels_averaged`,disclaimer 按实际像元数
写清温度是从多远、几个像元读来的。ESA WorldCover 判定该点本身是水面时不回退。

**没有为任何坐标写特例。**

## 改了哪些文件(★Bo 合并时最看重这个)
```
 CHANGES.md                                 |  (本文件)
 docs/heat-island.md                        |   +字段表与 Known Limitation #2 更新
 property_scores/api/static/heat_island.html|   +空值守卫 + "Read from" 行
 property_scores/heat_island/score.py       |   +回退路径 / CLI 渲染拆出可测
 tests/test_heat_island.py                  |   +10 个用例
```
（准确清单跑 `git diff master..heat-island-coastal-fix --stat`）

## 有没有要注意的
- **对现在就有分数的地址是 no-op**:生产实测 6000 个真实 DA 坐标,5848 个原本
  有分数的地址结果一字不差(只多了 `lst_source: "pixel"` 这个新键),0 个改变。
  所以 DA Leads 那边 `scores:v7` 不需要 bump;失败态本来就不入缓存
  (`_score_component_bad`),修完下一次请求即生效。
- 新增可选字段 `lst_source` / `lst_offset_m` / `lst_pixels_averaged`;
  `modis_area_c` 在回退路径上不再出现(消费方本来就该 null-check,DA Leads 的两个
  渲染层都有)。
- 已知未做(记在 limon-ops 日志的「尾巴」里):DA Leads 的
  `static/scores/score-map-widget.js` 不渲染 disclaimer,滨水地址在那个小组件上
  只会显示 `—` 而没有解释;老的 sea/forest 抑制路径仍然发 `modis_area_c`,
  GenericScore.vue 会据此自己算一个 "vs surroundings",那是渲染层的老问题,
  本次刻意没动(动它会改到现在正确的 payload)。
- 不涉及依赖、迁移、环境变量。

## 验证情况
- 单元测试:该文件 17 个用例(新增 10 个),全套 194 passed / 7 skipped。
  注错验红:两轮共 10+ 处人为注入,每一处都让对应用例变红(含独立 review agent
  自己注入的 6 处)。
- 生产实测(Oracle,真 MODIS 栅格):Surfers 由 null 变成 score 48
  "Moderate Heat"(32.1°C,927m 外两个像元的均值);6000 个 DA 坐标里 152 个
  地址自己的像元没读数,149 个恢复(142 个在 927m 环,7 个在 1310m 环),3 个
  仍然 "Data unavailable"。
- 两轮独立 code review(general-purpose agent,有 Bash + 生产访问),第二轮在
  第一轮的修订里又抓出 2 个 blocker(CLI 打印崩在本分支新增的那条路径上;
  CLI 里仍写死"water-masked"这个未经证实的成因),已修并补了覆盖 CLI 的用例。
- 详细根因/验证记录见 limon-ops `logs/da-leads/2026-08-05_heat-island-surfers-fix.md`。
