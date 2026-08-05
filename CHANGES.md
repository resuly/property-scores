# 本对话改动说明

## 基本信息
- 分支名: heat-island-coastal-fix
- 从哪切的: master
- worktree 路径: /Users/bwwan3/Documents/GitHub/property-scores-heat-island-coastal-fix
- demo 端口(如有): 无

## 这个对话做了什么(功能说明)
修生产活故障:滨水地址的 heat_island 分数恒返回 `score: null / "Data unavailable"`。

根因是 MODIS LST 1km 产品在源头做了水体掩膜——凡是被判为水的 1km 像元整格写成
填充值。海边地址(如 DA Leads sandbox 的 1/1 Cavill Avenue, Surfers Paradise)
正好落在这种被掩膜的像元上,中心点采样得 NaN,整个热岛分数就没了,而它西边 926m
的陆地像元有数(32.3°C)。不是上游挂了,也不是我们的 bbox/日期窗口有 bug。

改法:中心像元有数时走法完全不变;中心像元被掩膜时,在 2km 内按环由近及远找有数
的陆地像元,取最近那一环的均值(同距离的多个像元求平均,避免固定的方位偏好),夜间
读数取自同一批像元。这条路径上不报 UHI(借来的像元本身就在它要被比较的邻域里),
payload 带 `lst_source` / `lst_offset_m`,面向客户的 disclaimer 明说温度是从
Xm 外的陆地像元读的。ESA WorldCover 判定该点本身是水面时不做回退(防止地理编码
落海的点被安上一个陆地温度)。

## 改了哪些文件(★Bo 合并时最看重这个)
```
 CHANGES.md                           | (本文件)
 property_scores/heat_island/score.py | 195 +++++++++++++++++++++++++++++----
 tests/test_heat_island.py            | 194 ++++++++++++++++++++++++++++++++++
```

## 有没有要注意的
- 对现在就有分数的地址是 no-op(生产实测逐字段比对),所以 DA Leads 那边
  `scores:v7` 缓存不需要 bump。失败态本来就不入缓存(`_score_component_bad`),
  修完下一次请求即生效。
- 新增字段 `lst_source` / `lst_offset_m` 只在有 MODIS 读数时出现;`uhi_delta_c`
  本来就可能缺席(sea/forest 邻域时),消费方不应假设它必然存在。
- 不涉及依赖、迁移、环境变量。

## 验证情况
- 单元测试:新增 8 个用例,全套 189 passed / 7 skipped。注错验红:6 处人为
  注入(去掉水面守卫 / 把 2km 上限改成 3km / 取首个而不取均值 / 去掉调用侧
  UHI 抑制 / 恢复旧的"中心 NaN 就放弃" / 夜间读数改回地址本身)各自都让对应
  用例变红。
- 生产实测(Oracle,真 MODIS 栅格):Surfers 地址由 null 变成有分数;20000 个
  真实 DA 坐标里 513 个(2.57%)落在被掩膜像元上,其中 504 个能在 2km 内取到
  陆地像元(平均 941m,最远 1310m)。
- 详细根因/验证记录见 limon-ops `logs/da-leads/2026-08-05_heat-island-surfers-fix.md`。
