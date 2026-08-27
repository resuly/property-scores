"""Sands 白名单分类表的钉死测试（review P1-4：此前 21 条 TIER_A 删掉 20 条
全套测试仍绿，判断成分最重的表反而是唯一零覆盖的部分）。

值全部来自真实词表（Footscray-Brooklyn + Richmond-Collingwood bbox 实测，
595 个 distinct business_type，见 limon-ops research/2026-08-27_sands-pollution-whitelist.md）。
"""

import pytest

from property_scores.contamination.sources.sands_whitelist import (
    NSW_ACTIVITY_CLASS,
    TIER_A,
    TIER_B,
    classify,
)

# 每条 TIER_A 模式至少一个真实词表值命中: 删掉任何一条模式必有测试变红。
TIER_A_REAL_VALUES = [
    ("Service Stations", "fuel_storage"),
    ("Petroleum Products", "fuel_storage"),        # review 漏网: \bpetrol\b 曾挡死
    ("Motor Depots & Garages", "fuel_storage"),
    ("Motor Garages", "fuel_storage"),
    ("Motor Engineers", "fuel_storage"),
    ("Wood & Coal Yards", "fuel_storage"),
    ("Coal & Wood Yards", "fuel_storage"),
    ("Coal & Wood Merchants", "fuel_storage"),
    ("Fuel Merchants", "fuel_storage"),
    ("Oil Merchants", "fuel_storage"),
    ("Dry Cleaners & Dyers", "solvents"),
    ("Dry Cleaners", "solvents"),
    ("Dyers, Scourers, & Clothes Cleaners", "solvents"),
    ("Tanners & Curriers", "heavy_metals"),
    ("Tanners", "heavy_metals"),
    ("Curriers", "heavy_metals"),
    ("Iron Founders & Iron Workers", "heavy_metals"),
    ("Ornamental Iron Workers", "heavy_metals"),
    ("Brass Founders", "heavy_metals"),            # review 漏网: foundr 打不到 Founders
    ("Electroplaters & Gilders", "heavy_metals"),
    ("Galvanisers", "heavy_metals"),
    ("Smelters", "heavy_metals"),
    ("Batteries", "heavy_metals"),                 # review 漏网: 词表值不带 manufacturer
    ("Battery Engineers", "heavy_metals"),
    ("Chemical Manufacturers", "chemicals"),
    ("Gas Works", "gasworks"),
    ("Gasworks", "gasworks"),
    ("Tips", "landfill"),
    ("Rubbish Depots", "landfill"),
]

TIER_B_REAL_VALUES = [
    ("Blacksmiths", "metals_workshop"),
    ("Engineers - General", "metals_workshop"),
    ("Engineers & Machinists", "metals_workshop"),
    ("Printers and/or Lithographers", "solvents_light"),
    ("Laundries", "solvents_light"),
    ("Motor Cars & Trucks - Used", "fuel_storage_light"),
]

# 高频无害行业(工业带词表 top 值): 一个都不许中。
HARMLESS = [
    "Accountants - Professional", "Fruiterers & Green Grocers",
    "Grocers - Retail", "Confectioners - Retail & Milk Bars", "Hairdressers",
    "Butchers & Meat Salesmen", "Hotels & Public Houses", "Tailors", "Bakers",
    "Physicians & Surgeons", "Estate Agents", "Chemists & Druggists",
    "Fishmongers", "News Agents", "Dentists", "Restaurants",
    "Boot & Shoe Makers & Dealers", "Dressmakers & Milliners",
    # 明确排除的流动作业者(白名单文档"明确排除"节)
    "Painters, Glaziers & Paperhangers", "Plumbers & Gasfitters",
    "Electrical Contractors & Engineers", "Builders & Contractors",
    "Carpenters & Joiners",
]

# 误伤守卫(review 实测复现过的两个 false positive)
FALSE_POSITIVE_GUARDS = ["Tip Top Bakeries", "Garage Doors", "Garage Door Makers"]


@pytest.mark.parametrize("value,expected_class", TIER_A_REAL_VALUES)
def test_tier_a_real_vocabulary(value, expected_class):
    assert classify(value) == ("A", expected_class), value


@pytest.mark.parametrize("value,expected_class", TIER_B_REAL_VALUES)
def test_tier_b_real_vocabulary(value, expected_class):
    assert classify(value) == ("B", expected_class), value


@pytest.mark.parametrize("value", HARMLESS)
def test_harmless_trades_never_match(value):
    assert classify(value) is None, value


@pytest.mark.parametrize("value", FALSE_POSITIVE_GUARDS)
def test_false_positive_guards(value):
    assert classify(value) is None, value


def test_every_tier_a_pattern_is_load_bearing():
    """删掉任何一条 TIER_A 模式, TIER_A_REAL_VALUES 必有值失配。
    这是"实质零覆盖"再也不能发生的结构性保证。"""
    for idx in range(len(TIER_A)):
        remaining = TIER_A[:idx] + TIER_A[idx + 1:]

        def classify_without(text):
            t = text.lower()
            for pat, cls in remaining:
                if pat.search(t):
                    return ("A", cls)
            return None

        broken = [v for v, want in TIER_A_REAL_VALUES
                  if classify_without(v) != ("A", want)]
        assert broken, (f"TIER_A[{idx}] ({TIER_A[idx][0].pattern!r}) 没有任何"
                        "真实词表值依赖它: 要么补值, 要么删模式")


def test_empty_and_none_are_safe():
    assert classify(None) is None
    assert classify("") is None


def test_nsw_activity_taxonomy_covers_measured_distribution():
    # NSW FeatureServer 实测的全部 activitytype 值(2026-08-27, 1,991 条)
    measured = ["Service Station", "Other Industry", "Other Petroleum",
                "Unclassified", "Landfill", "Metal Industry", "Gasworks",
                "Chemical Industry", "Cattle Dip"]
    for value in measured:
        assert value.lower() in NSW_ACTIVITY_CLASS, value
