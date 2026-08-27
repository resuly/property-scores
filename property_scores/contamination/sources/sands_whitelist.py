"""Sands 历史行业 -> 污染活动白名单。

定稿依据: limon-ops research/2026-08-27_sands-pollution-whitelist.md
(CBD+Footscray 6,000 条 + Footscray-Brooklyn/Richmond 工业带 10,000 条实测样本)。

原则(tracker 架构决定):
- 白名单不做全量映射: 绝大多数行业(会计/杂货/理发)无污染含义, 默认忽略;
- A 级 = 行业本身就是 EPA 登记册上的经典污染前科, 参与打分(信号不是判定);
- B 级 = 机理弱一档, 只进 evidence 展示, 不参与分数;
- 流动作业者(油漆匠/水管工/电工/建筑商)明确排除: 登记地址是门店或住所,
  不代表该场地发生过污染活动, 算进去就是误伤;
- 匹配用规范化子串: 同一行业跨年份措辞会变("Dry Cleaners & Dyers"/"Dyers & Cleaners")。

NSW Contaminated Sites List 的 contaminationactivitytype 映射到同一套分类
(NSW_ACTIVITY_CLASS), 两源共用一个 taxonomy。

对外措辞红线: 跟 Sands 官方免责口径, directory 记录不是污染证据, 文案说
"appears in historical directories as a service station", 永不说 "contaminated"。
"""

import re

# A 级: (正则模式, 分类)。模式对 lower() 后的 business_type 做 search。
TIER_A: list[tuple[re.Pattern, str]] = [(re.compile(p), c) for p, c in [
    (r"service station", "fuel_storage"),
    # \bpetrol 前缀式: "Petrol" 与 "Petroleum Products" 都要中 (review: 词尾
    # 边界曾把 Petroleum 挡死)
    (r"\bpetrol", "fuel_storage"),
    # 负向断言: "Garage Doors" 是卖门的, 不是修车的 (review 误伤守卫)
    (r"\bgarage(?!\s*door)", "fuel_storage"),
    (r"motor engineer", "fuel_storage"),
    (r"(coal|wood) (& (wood|coal) )?(yard|merchant)", "fuel_storage"),
    (r"fuel merchant", "fuel_storage"),
    (r"\boil (merchant|refin|compan)", "fuel_storage"),
    (r"dry.?cleaner", "solvents"),
    (r"\bdyer", "solvents"),
    (r"\btanner", "heavy_metals"),
    (r"currier", "heavy_metals"),
    # "Brass Founders"/"Founders"/"Foundry" 全形态 (review: foundr 打不到
    # Founders, 中间隔个 e)
    (r"found(er|r)", "heavy_metals"),
    (r"iron worker", "heavy_metals"),
    (r"electro.?plat", "heavy_metals"),
    (r"galvani[sz]", "heavy_metals"),
    (r"smelt", "heavy_metals"),
    # 真实词表值是 "Batteries" / "Battery Engineers", 不带 manufacturer 后缀
    (r"\bbatter(y|ies)\b", "heavy_metals"),
    (r"chemical manufactur", "chemicals"),
    (r"gas.?works", "gasworks"),
    # 只认独立的 Tip/Tips 或带限定词的, "Tip Top Bakeries" 不许中 (review)
    (r"^tips?$", "landfill"),
    (r"(rubbish|garbage|municipal|sanitary) (depot|tip)", "landfill"),
]]

# B 级: 只进 evidence, 不打分。
TIER_B: list[tuple[re.Pattern, str]] = [(re.compile(p), c) for p, c in [
    (r"blacksmith", "metals_workshop"),
    (r"engineers?( -| &) (general|machinist)", "metals_workshop"),
    (r"machinist", "metals_workshop"),
    (r"printer", "solvents_light"),
    (r"lithograph", "solvents_light"),
    (r"laundr", "solvents_light"),
    (r"motor (cars? & trucks?|wrecker)", "fuel_storage_light"),
    (r"\bwrecker", "fuel_storage_light"),
]]


def classify(business_type: str | None) -> tuple[str, str] | None:
    """Return (tier, activity_class) or None when the trade carries no
    contamination meaning. Tier A wins over B when both match."""
    text = str(business_type or "").lower()
    if not text:
        return None
    for pattern, cls in TIER_A:
        if pattern.search(text):
            return "A", cls
    for pattern, cls in TIER_B:
        if pattern.search(text):
            return "B", cls
    return None


# NSW register activity types -> 同一 taxonomy(register 记录本身已是污染判定,
# 这张表只做展示分类, 不改变其打分语义)。
NSW_ACTIVITY_CLASS = {
    "service station": "fuel_storage",
    "other petroleum": "fuel_storage",
    "gasworks": "gasworks",
    "landfill": "landfill",
    "chemical industry": "chemicals",
    "metal industry": "heavy_metals",
    "cattle dip": "chemicals",
    "other industry": "industrial_other",
    "unclassified": "unclassified",
}
