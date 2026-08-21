"""ArcGIS 代理 item 轮换的解析。

背景: 2026-07-23 QLD bushfire 代理 item 死掉(HTTP 200 带 error body,
"Error generating token"), 实际上是发布方重建了 item, 同一个服务两周后在新 id
下活着。手工找到它花了一下午, 中途还把"没有替代端点"这个错误结论写进了代码注释。

这个工具有两种有害方式, 都比它诊断的故障更糟, 测试按这两类组织:
  A. 说"没有替代"而其实有 —— 上次那句错误注释。截断、按标题过滤、按类型过滤
     都会造出这个假象。
  B. 推荐错的层 —— 同名不算数(新旧同 owner 同标题)、更新不算数(可以留个坏的
     新 item)、空图层答得很干净但会把全 QLD 评成"不是火险区"。

假 ArcGIS 必须至少和真的一样严格。第一版它忽略搜索条件, 让"按标题过滤"变异
静默存活; 第二版仍缺分页/被查询项自身/网络失败/headers, 又放过 5 个变异。
现在按实测的真实响应形状建模。
"""
import importlib.util
import re
import sys
import urllib.parse
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "resolve_arcgis_proxy",
    Path(__file__).resolve().parent.parent / "scripts" / "resolve_arcgis_proxy.py")
resolver = importlib.util.module_from_spec(_SPEC)
sys.modules["resolve_arcgis_proxy"] = resolver
_SPEC.loader.exec_module(resolver)

DEAD = "8ac1ba8eccee472fbd0e7a57bf3ad320"
LIVE_ID = "3ec80e95fa084ef9901205df0a7a74ec"
OWNER = "PublicSafetyQld_Data"
TITLE = "Bushfire Prone Area (BPA) Dynamic Limited"
TOKEN_ERROR = {"error": {"code": 500, "messageCode": "CONT_0044",
                         "message": "Error invoking service",
                         "details": ["Error generating token"]}}


def _proxy(item_id):
    return (f"https://utility.arcgis.com/usrsvcs/servers/{item_id}"
            f"/rest/services/Hosted/BPA/FeatureServer")


def _item(iid, *, title=TITLE, owner=OWNER, typ="Feature Service",
          url=None, created=2_000_000_000_000):
    return {"id": iid, "title": title, "owner": owner, "type": typ,
            "url": _proxy(iid) if url is None else url, "created": created}


@pytest.fixture
def ago(monkeypatch):
    """按实测的真实 AGO 形状建模, 一个真实请求都不发。

    刻意保留的真实行为: /search 按 q 过滤、分页带 total/nextStart、**结果里
    包含被查询项自身**、部分 item 没有 url、探活可返回 None(网络/WAF 失败)、
    带 Referer 才答的 host。
    """
    world = {
        "items": {DEAD: _item(DEAD, created=1_700_000_000_000),
                  LIVE_ID: _item(LIVE_ID)},
        "live": {LIVE_ID: 2_556_671},   # id -> feature count
        "search": [],                   # 本 owner 的全部 item(含被查询项)
        "page": 100,                    # 每页条数, 用来造截断
        "needs_referer": set(),         # 这些 id 不带 Referer 就 403
        "unreachable": set(),           # 这些 id 的请求直接失败
        # 服务元数据形状。默认是普通 FeatureServer; 真实世界还有 GeocodeServer
        # (完全没有 layers)、第一层是 Group Layer 的 MapServer、错误体。
        # 第一版 fake 对所有 ?f=json 都返回同一种形状, 于是整个元数据分支
        # 一个变异都测不到 —— 而两个 live bug 恰好都在那里。
        "meta": {},                     # id -> 元数据 dict 或 None
        "query": {},                    # id -> 覆盖 query 响应
        "calls": [],
    }

    def _service_id(url):
        # 不用 resolver.item_id_from 当预言机: 那会让 regex 变异同时改掉
        # 假账本, 变异和被测代码一起动 = 测不出来。
        return url.split("/servers/")[1].split("/")[0] if "/servers/" in url else None

    def fake_get(url, headers=None, timeout=None, attempts=2):
        world["calls"].append((url, dict(headers or {})))
        if "/content/items/" in url:
            iid = url.split("/content/items/")[1].split("?")[0]
            return world["items"].get(iid) or {"error": {"code": 400}}
        if "/search?" in url:
            qs = urllib.parse.parse_qs(url.split("?", 1)[1])
            q = urllib.parse.unquote(qs["q"][0])
            num = int(qs.get("num", ["10"])[0])
            start = int(qs.get("start", ["1"])[0])
            hits = []
            for r in world["search"]:
                if f"owner:{r['owner']}" not in q:
                    continue
                tm = re.search(r'type:"([^"]+)"', q)
                if tm and tm.group(1) != r.get("type"):
                    continue
                nm = re.search(r'title:"([^"]+)"', q)
                if nm and nm.group(1) != r.get("title"):
                    continue
                hits.append(r)
            hits.sort(key=lambda r: -(r.get("created") or 0))
            per = min(num, world["page"])
            window = hits[start - 1:start - 1 + per]
            nxt = start + per
            return {"total": len(hits), "start": start, "num": len(window),
                    "nextStart": nxt if nxt <= len(hits) else -1,
                    "results": window}
        sid = _service_id(url)
        if sid in world["unreachable"]:
            return None
        if sid in world["needs_referer"] and "Referer" not in (headers or {}):
            return None                      # 真实是 403 → _get 返回 None
        if url.endswith("?f=json"):          # 服务元数据
            if sid in world["meta"]:
                return world["meta"][sid]
            return {"layers": [{"id": 0, "name": "BPA"}]}
        if "/query?" in url:
            # 真 API 会看 query string。假账本必须也看, 否则最危险的一个变异
            # ——去掉 returnCountOnly——测不出来: 那会对约 174 个候选逐个拉全量
            # 要素, 其中有 250 万和 347 万要素的图层。
            qs = urllib.parse.parse_qs(url.split("?", 1)[1])
            if qs.get("f", [""])[0] != "json":
                raise AssertionError(f"must ask for f=json: {url}")
            if not qs.get("where"):
                return {"error": {"code": 400,
                                  "message": "Invalid or missing input parameters."}}
            if qs.get("returnCountOnly", [""])[0] != "true":
                # 真 API 这时返回的是要素体, 不是计数
                return {"features": [{"attributes": {}} for _ in range(1000)]}
            if sid in world["query"]:
                return world["query"][sid]
            if sid in world["live"]:
                return {"count": world["live"][sid]}
            return TOKEN_ERROR
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(resolver, "_get", fake_get)
    return world


# ── A 类: 不能谎称"没有替代" ────────────────────────────────────────────


def test_finds_the_live_replacement_behind_an_identical_title(ago):
    """真实那对: 同 owner 同标题, 一个死一个活, 只能靠探活分辨。"""
    ago["search"] = [_item(DEAD, created=1_700_000_000_000), _item(LIVE_ID)]
    out = resolver.resolve(DEAD)
    assert out["ok"] and out["queried"]["verdict"] == resolver.DEAD
    assert [r["id"] for r in out["replacements"]] == [LIVE_ID]
    assert out["search_complete"] is True


def test_a_renamed_replacement_is_still_found(ago):
    """按标题过滤的话, 重建时改了名就返回空, 而空会被读成'图层没了'。"""
    renamed = _item(LIVE_ID, title="Bushfire Prone Area (BPA) 2026")
    ago["items"][LIVE_ID] = renamed
    ago["search"] = [_item(DEAD, created=1_700_000_000_000), renamed]
    out = resolver.resolve(DEAD)
    assert [r["id"] for r in out["replacements"]] == [LIVE_ID]
    assert out["replacements"][0]["same_title"] is False


def test_a_replacement_published_as_a_different_type_is_still_found(ago):
    """按 type 过滤有和按标题过滤一样的失败模式: 这个 owner 有 14 种 type,
    重建时换成 Map Service 就会从结果里消失, 又变成假的'没有替代'。"""
    remapped = _item(LIVE_ID, typ="Map Service")
    ago["items"][LIVE_ID] = remapped
    ago["search"] = [_item(DEAD, created=1_700_000_000_000), remapped]
    out = resolver.resolve(DEAD)
    assert [r["id"] for r in out["replacements"]] == [LIVE_ID]


def test_the_replacement_is_found_beyond_the_first_page(ago):
    """真实 owner 有 67 个 item 而单页上限 100; 一旦超过就必须翻页。
    不翻页时它今天还能work 纯粹因为按 created 倒序恰好排在前面——
    那等于偷偷依赖了'取最新', 而 docstring 明说那不是规则。"""
    filler = [_item(f"{i:032x}", title=f"unrelated {i}", created=9_000_000_000_000 - i)
              for i in range(1, 8)]
    for f in filler:
        ago["items"][f["id"]] = f
    # 活的那个 created 最老 → 一定落到第二页
    ago["search"] = filler + [_item(DEAD, created=1_700_000_000_000),
                              _item(LIVE_ID, created=1_000_000_000_000)]
    ago["page"] = 3
    out = resolver.resolve(DEAD)
    assert out["search_complete"] is True
    assert LIVE_ID in [r["id"] for r in out["replacements"]]


def test_a_truncated_search_never_claims_no_replacement_exists(ago, capsys):
    """搜不完时说的必须是'没找完', 不是'不存在'——后者正是上次那句错误注释。"""
    ago["search"] = [_item(DEAD, created=1_700_000_000_000)]

    real = resolver._get

    def flaky(url, headers=None, timeout=None, attempts=2):
        if "/search?" in url:
            return None                    # 搜索整个失败 = 不完整
        return real(url, headers, timeout, attempts)

    resolver._get = flaky
    try:
        rc = resolver.main(["prog", DEAD])
    finally:
        resolver._get = real
    assert rc == 2, "没找完不能返回'找过了没有'的 1"
    err = capsys.readouterr().err
    assert "TRUNCATED" in err and "did not finish looking" in err


def test_a_complete_search_with_nothing_live_still_hedges(ago, capsys):
    ago["search"] = [_item(DEAD, created=1_700_000_000_000)]
    rc = resolver.main(["prog", DEAD])
    assert rc == 1
    assert "before concluding the layer is gone" in capsys.readouterr().err


# ── B 类: 不能推荐错的层 ────────────────────────────────────────────────


def test_a_dead_newer_item_is_not_offered(ago):
    """'取最新'会选到这个坏的。只有探活能挡掉。"""
    newer_dead = "f" * 32
    ago["items"][newer_dead] = _item(newer_dead, created=9_000_000_000_000)
    ago["search"] = [_item(newer_dead, created=9_000_000_000_000),
                     _item(DEAD, created=1_700_000_000_000), _item(LIVE_ID)]
    out = resolver.resolve(DEAD)
    assert [r["id"] for r in out["replacements"]] == [LIVE_ID]


def test_an_empty_layer_is_never_offered_as_a_replacement(ago, capsys):
    """空图层答得很干净, 换上去会把全 QLD 评成'不是火险区'——
    静默的错答案比看得见的故障更糟。真实跑里就出现过一个 count=0 的候选。"""
    empty = "e" * 32
    ago["items"][empty] = _item(empty, title="LGAs Firebans Public View")
    ago["live"][empty] = 0
    ago["search"] = [_item(empty), _item(DEAD, created=1_700_000_000_000)]
    out = resolver.resolve(DEAD)
    assert out["replacements"] == []
    assert [e["id"] for e in out["empty_candidates"]] == [empty]
    resolver.main(["prog", DEAD])
    assert "EMPTY candidate" in capsys.readouterr().err


def test_an_unreachable_probe_is_not_evidence_of_death(ago, capsys):
    """SA 那类端点不带 Referer 就 403。把'我够不着'当成'它死了',
    就会给一个完全健康的层推荐替代品。"""
    ago["needs_referer"].add(DEAD)
    out = resolver.resolve(DEAD)
    assert out["queried"]["verdict"] == resolver.UNKNOWN
    assert out["replacements"] == [], "够不着的时候一个候选都不能给"
    rc = resolver.main(["prog", DEAD])
    assert rc == 2
    assert "not evidence of a dead layer" in capsys.readouterr().err


def test_a_host_that_needs_a_referer_is_probed_with_one(ago):
    """HOST_HEADERS 得真的用上, 否则 SA 的层永远判死。"""
    sa_id = "a" * 32
    sa_url = ("https://lsa2.geohub.sa.gov.au/arcgis/rest/services/SAPPA/"
              "PropertyPlanningAtlasV18/MapServer/135")
    ago["items"][sa_id] = _item(sa_id, url=sa_url)
    hdrs = [h for u, h in ago["calls"]]
    resolver.probe(sa_url)
    used = [h for u, h in ago["calls"] if "geohub.sa.gov.au" in u]
    assert used and all("Referer" in h for h in used), used


def test_a_still_live_item_resolves_to_nothing(ago):
    """没坏就别换。search 里故意放一个活的同名候选: 少了早退它就会被报出来,
    诱人去换掉一个本来好好的生产 URL。"""
    ago["live"][DEAD] = 2_500_000
    ago["search"] = [_item(DEAD, created=1_700_000_000_000), _item(LIVE_ID)]
    out = resolver.resolve(DEAD)
    assert out["queried"]["verdict"] == resolver.LIVE
    assert out["replacements"] == []


def test_the_queried_item_is_never_offered_as_its_own_replacement(ago):
    """真实 /search 会把被查询项本身也返回。"""
    ago["search"] = [_item(DEAD, created=1_700_000_000_000), _item(LIVE_ID)]
    out = resolver.resolve(DEAD)
    assert DEAD not in [r["id"] for r in out["replacements"]]


def test_an_item_without_a_service_url_is_skipped(ago):
    """真实结果里有不带 url 的 item(网页、PDF 之类)。"""
    no_url = "b" * 32
    entry = _item(no_url, url="")
    entry["url"] = None
    ago["items"][no_url] = entry
    ago["search"] = [entry, _item(DEAD, created=1_700_000_000_000), _item(LIVE_ID)]
    out = resolver.resolve(DEAD)
    assert [r["id"] for r in out["replacements"]] == [LIVE_ID]


# ── 探活本身 ────────────────────────────────────────────────────────────


def test_the_probe_does_not_blindly_append_layer_zero(ago):
    """实测这个 owner 49 个 Feature Service 里有 5 个 url 已经带层号,
    再补 /0 得到 .../MapServer/6/0/query → error 400, 和真死一模一样。"""
    layered = "c" * 32
    # 多位层号: 真实的 SA 图层是 .../MapServer/135, 单位数的 /6 会让
    # "/\d+$" 退化成 "/\d$" 的变异活下来。
    url = _proxy(layered) + "/135"
    ago["items"][layered] = _item(layered, url=url)
    ago["live"][layered] = 3359
    verdict, _why, count = resolver.probe(url)
    assert verdict == resolver.LIVE and count == 3359
    assert not any("/135/0/query" in u for u, _h in ago["calls"]), ago["calls"]


def test_an_error_body_over_http_200_is_dead_not_live(ago):
    """本次事故的形态: HTTP 200 + error body, 没有任何异常抛出。"""
    verdict, why, _ = resolver.probe(_proxy(DEAD))
    assert verdict == resolver.DEAD
    assert "Error generating token" in why


# ── 输入解析 ────────────────────────────────────────────────────────────


def test_it_accepts_a_full_proxy_url_not_just_an_id(ago):
    ago["search"] = [_item(DEAD, created=1_700_000_000_000), _item(LIVE_ID)]
    out = resolver.resolve(_proxy(DEAD) + "/0")
    assert out["queried"]["id"] == DEAD


def test_a_longer_hex_run_is_not_mistaken_for_an_item_id():
    """40 位 sha 里截 32 位会得到一个不存在的 id, 然后报'读不到'——
    看起来像 AGO 挂了, 其实是我们自己切错了。"""
    sha = "da39a3ee5e6b4b0d3255bfef95601890afd80709"
    assert resolver.item_id_from(f"https://x/{sha}/y") is None


def test_the_id_in_the_servers_segment_wins(ago):
    """URL 里可能有别的 32 位 hex 段(缓存键之类), 取 /servers/ 后面那个。"""
    other = "d" * 32
    url = f"https://utility.arcgis.com/{other}/usrsvcs/servers/{DEAD}/rest"
    assert resolver.item_id_from(url) == DEAD


def test_a_query_that_fails_after_metadata_succeeds_is_unknown_not_live(ago):
    """S4 的真实路径: 服务元数据答得上、只有 query 够不着(限流/超时/WAF)。

    上一版测试只覆盖了"元数据也够不着", 那会在更早一步就返回 unknown,
    所以"把 unreachable 当成活"这个变异从没被执行到。把 None 判成 live 的
    后果是最重的一类: 一个死层会被当成健康替代推荐出去。
    """
    flaky = "9" * 32
    ago["items"][flaky] = _item(flaky)
    real = resolver._get

    def only_query_fails(url, headers=None, timeout=None, attempts=2):
        if "/query?" in url and flaky in url:
            return None
        return real(url, headers, timeout, attempts)

    resolver._get = only_query_fails
    try:
        verdict, why, count = resolver.probe(_proxy(flaky))
    finally:
        resolver._get = real
    assert verdict == resolver.UNKNOWN, f"够不着不能算活: {verdict} {why}"
    assert count is None


def test_paging_does_not_stop_before_the_owners_items_run_out(ago):
    """S1: 页太小时 MAX_PAGES 会先到顶。真实 owner 有 67 个 item,
    num=1 配 MAX_PAGES=20 只能看到 20 个 —— 又一次"没找完"被当成"没有"。"""
    n = resolver.MAX_PAGES + 5
    filler = [_item(f"{i:032x}", title=f"unrelated {i}",
                    created=9_000_000_000_000 - i) for i in range(1, n)]
    for f in filler:
        ago["items"][f["id"]] = f
    ago["search"] = filler + [_item(DEAD, created=1_700_000_000_000),
                              _item(LIVE_ID, created=1_000_000_000_000)]
    ago["page"] = resolver.PAGE
    out = resolver.resolve(DEAD)
    assert out["search_complete"] is True, "一页装得下就该一次翻完"
    assert LIVE_ID in [r["id"] for r in out["replacements"]]


def test_the_search_asks_for_newest_first(ago):
    """S2: 翻页修好之后排序不再决定完整性, 但一旦撞上 MAX_PAGES 截断,
    它决定我们先看到谁。重建出来的替代总是较新, 所以倒序是有意义的默认。"""
    ago["search"] = [_item(DEAD, created=1_700_000_000_000), _item(LIVE_ID)]
    resolver.resolve(DEAD)
    searches = [u for u, _h in ago["calls"] if "/search?" in u]
    assert searches, "没发出搜索"
    assert all("sortField=created" in u and "sortOrder=desc" in u
               for u in searches), searches


# ── 元数据分支: 两个 live bug 都在这里 ─────────────────────────────────


def test_a_service_with_no_layers_is_unknown_not_dead(ago):
    """GeocodeServer 没有 layers, 而且会**忽略**补上的 /0/query 直接回显自己的
    元数据文档 —— 那份文档里没有 error 键。把"不是 error"当成活, 就会像 live
    run 里真实发生过的那样, 把 "Queensland Locator View" 当成火险图层的替代。"""
    geo = "7" * 32
    ago["items"][geo] = _item(geo, title="Queensland Locator View",
                              typ="Geocoding Service")
    ago["meta"][geo] = {"currentVersion": 10.91, "serviceDescription": "locator",
                        "spatialReference": {"wkid": 4326}}
    ago["query"][geo] = {"currentVersion": 10.91, "serviceDescription": "locator"}
    verdict, why, count = resolver.probe(_proxy(geo))
    assert verdict == resolver.UNKNOWN, f"{verdict}: {why}"
    assert count is None


def test_a_query_answering_without_an_integer_count_is_not_live(ago):
    """returnCountOnly 在真实图层上必然给出 int。给别的东西 = 这不是图层。"""
    odd = "8" * 32
    ago["items"][odd] = _item(odd)
    ago["query"][odd] = {"currentVersion": 11.0}      # 无 error 也无 count
    verdict, _why, _c = resolver.probe(_proxy(odd))
    assert verdict == resolver.UNKNOWN


def test_a_group_layer_at_index_zero_does_not_make_a_service_look_dead(ago):
    """SAPPA 那个 MapServer 的 layers[0] 就是个 Group Layer(Survey Marks),
    查它得到 error 400 —— 而这恰恰发生在 HOST_HEADERS 专门为之存在的那个 host。
    取 layers[0] 会把一个完全健康的服务判死。"""
    grp = "6" * 32
    ago["items"][grp] = _item(grp)
    ago["meta"][grp] = {"layers": [
        {"id": 0, "name": "Survey Marks", "type": "Group Layer",
         "subLayerIds": [1, 2]},
        {"id": 135, "name": "Bushfire - High Risk", "type": "Feature Layer",
         "subLayerIds": None}]}
    q, guessed = resolver._layer_query_url(_proxy(grp))
    assert q and q.endswith("/135"), q
    assert guessed is False


def test_a_group_is_skipped_on_sublayerids_alone(ago):
    """有的 MapServer 只给 subLayerIds 不给 type=Group Layer。
    两个判据要各自独立成立, 否则去掉一个另一个兜着, 变异测不出来。"""
    grp = "6a" + "0" * 30
    ago["items"][grp] = _item(grp)
    ago["meta"][grp] = {"layers": [
        {"id": 0, "name": "Survey Marks", "subLayerIds": [1, 2]},
        {"id": 135, "name": "Bushfire", "subLayerIds": None}]}
    q, guessed = resolver._layer_query_url(_proxy(grp))
    assert q and q.endswith("/135"), q
    assert guessed is False


def test_a_group_is_skipped_on_type_alone(ago):
    grp = "6b" + "0" * 30
    ago["items"][grp] = _item(grp)
    ago["meta"][grp] = {"layers": [
        {"id": 0, "name": "Survey Marks", "type": "Group Layer"},
        {"id": 135, "name": "Bushfire", "type": "Feature Layer"}]}
    q, guessed = resolver._layer_query_url(_proxy(grp))
    assert q and q.endswith("/135"), q
    assert guessed is False


def test_an_item_url_that_is_itself_a_feature_layer_is_probed_directly(ago):
    """AGO 上有的 item 的 url 直接就是一个 Feature Layer(不带层号也没有
    layers 列表)。少了这条分支它会被判成"没有可查层"而误报 unknown。"""
    lyr = "1a" + "0" * 30
    ago["items"][lyr] = _item(lyr)
    ago["meta"][lyr] = {"type": "Feature Layer", "name": "BPA", "id": 0}
    ago["live"][lyr] = 42
    q, guessed = resolver._layer_query_url(_proxy(lyr))
    assert q == _proxy(lyr), q
    assert guessed is False
    assert resolver.probe(_proxy(lyr))[0] == resolver.LIVE


def test_unreadable_service_metadata_does_not_fall_back_to_layer_zero(ago):
    """元数据够不着时补 /0 是在猜。猜错的代价是把好层判死。"""
    blind = "5" * 32
    ago["items"][blind] = _item(blind)
    ago["meta"][blind] = None
    assert resolver._layer_query_url(_proxy(blind))[0] is None
    verdict, why, _ = resolver.probe(_proxy(blind))
    assert verdict == resolver.UNKNOWN and "queryable layer" in why


def test_a_service_erroring_on_its_own_root_is_ruled_dead_not_unknown(ago):
    """★ 这条是这个工具存在的理由本身。

    真实那个死掉的 QLD 服务, **连服务根都返回** "Error generating token"。
    我修 F2 时把"元数据是 error 体"一并归成了"够不着", 结果对着真实事故跑出
    "cannot tell", 工具对它唯一要解决的问题失效了。服务自己报错 = 死亡的确凿
    证据, 和"网络够不着"必须分开。
    """
    ago["meta"][DEAD] = TOKEN_ERROR
    verdict, why, _ = resolver.probe(_proxy(DEAD))
    assert verdict == resolver.DEAD, f"{verdict}: {why}"
    assert "Error generating token" in why


# ── 完整性判定 ──────────────────────────────────────────────────────────


def test_a_page_without_next_start_is_only_complete_if_total_agrees(ago):
    """缺 nextStart 会默认 -1 = "就这些了"。AGO 正常页确实都带它, 但
    start+num>10000 时它返回的是错误体, 所以这个假设是承重的。"""
    real = resolver._get

    def short(url, headers=None, timeout=None, attempts=2):
        if "/search?" in url:
            return {"total": 500, "start": 1, "num": 3,
                    "results": [_item(f"{i:032x}") for i in range(3)]}
        return real(url, headers, timeout, attempts)

    resolver._get = short
    try:
        items, complete = resolver.candidates(OWNER)
    finally:
        resolver._get = real
    assert len(items) == 3
    assert complete is False, "3 条对不上 total=500, 不能说找完了"


# ── F3 的头号保证: unknown 时根本不该去搜 ───────────────────────────────


def test_an_unknown_verdict_does_not_even_run_the_search(ago):
    """上一版这条测试断言 replacements 为空, 但它压根没往 search 里放东西,
    所以断言的是假账本的空集, 而不是代码的行为 —— 变异照样绿。"""
    ago["needs_referer"].add(DEAD)
    ago["search"] = [_item(DEAD, created=1_700_000_000_000), _item(LIVE_ID)]
    out = resolver.resolve(DEAD)
    assert out["queried"]["verdict"] == resolver.UNKNOWN
    assert out["replacements"] == []
    assert not any("/search?" in u for u, _h in ago["calls"]), \
        "够不着的时候连搜都不该搜"


# ── 排序 ────────────────────────────────────────────────────────────────


def test_same_title_candidates_rank_above_newer_differently_named_ones(ago):
    """live run 里正确那个排第一, 但那个性质之前没有任何测试钉住。"""
    newer_other = "3" * 32
    ago["items"][newer_other] = _item(newer_other, title="Something Else",
                                      created=9_000_000_000_000)
    ago["live"][newer_other] = 10
    ago["search"] = [_item(newer_other, title="Something Else",
                           created=9_000_000_000_000),
                     _item(DEAD, created=1_700_000_000_000),
                     _item(LIVE_ID, created=1_000_000_000_000)]
    out = resolver.resolve(DEAD)
    ranked = resolver._rank(out["replacements"])
    assert ranked[0]["id"] == LIVE_ID, "同标题的必须排在更新但不同名的前面"


# ── _get 自身(全程被 monkeypatch, 之前零覆盖) ──────────────────────────


def test_get_retries_once_and_merges_headers(monkeypatch):
    calls = []

    class FakeResp:
        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout=None):
        calls.append((req.get_full_url(), dict(req.headers)))
        if len(calls) == 1:
            raise TimeoutError("boom")
        return FakeResp(b'{"count": 7}')

    monkeypatch.setattr(resolver.urllib.request, "urlopen", urlopen)
    assert resolver._get("https://x/y", headers={"Referer": "r"}) == {"count": 7}
    assert len(calls) == 2, "第一次失败必须重试"
    assert calls[0][1].get("Referer") == "r"
    assert calls[0][1].get("User-agent") == resolver.UA["User-Agent"]


def test_get_gives_up_after_the_last_attempt(monkeypatch):
    calls = []

    def always_fail(req, timeout=None):
        calls.append(1)
        raise OSError("down")

    monkeypatch.setattr(resolver.urllib.request, "urlopen", always_fail)
    assert resolver._get("https://x/y") is None
    assert len(calls) == 2


# ── HOST_HEADERS 的 host 匹配 ───────────────────────────────────────────


def test_the_referer_is_matched_on_the_host_not_a_substring():
    ok = resolver._headers_for("https://lsa2.geohub.sa.gov.au/arcgis/rest")
    assert ok and "Referer" in ok
    spoof = resolver._headers_for("https://evil.example/geohub.sa.gov.au/x")
    assert spoof is None, "路径里出现 host 名不该拿到 Referer"


# ── 无 owner ────────────────────────────────────────────────────────────


def test_an_item_without_an_owner_says_so_rather_than_truncated(ago, capsys):
    lone = "2" * 32
    ago["items"][lone] = _item(lone, owner="")
    rc = resolver.main(["prog", lone])
    assert rc == 2
    assert "no owner on AGO" in capsys.readouterr().err


def test_an_unknown_candidate_is_never_offered_as_a_replacement(ago):
    """★ 复审点名最重要的那个缺口。

    probe 层有测试保证"够不着 = unknown", 但 resolve 层从没有端到端钉住
    "unknown 的候选不会进推荐列表"。而线上那个 Geocoding Service 被推荐,
    正是这条链路的后半段。
    """
    murky = "0f" + "0" * 30
    ago["items"][murky] = _item(murky, title=TITLE, created=9_000_000_000_000)
    ago["needs_referer"].add(murky)          # 探不动 → unknown
    ago["search"] = [_item(murky, created=9_000_000_000_000),
                     _item(DEAD, created=1_700_000_000_000), _item(LIVE_ID)]
    out = resolver.resolve(DEAD)
    ids = [r["id"] for r in out["replacements"]] + \
          [e["id"] for e in out["empty_candidates"]]
    assert murky not in ids, "探不动的候选不能当替代品推出去"
    assert [r["id"] for r in out["replacements"]] == [LIVE_ID]


def test_the_probe_asks_only_for_a_count(ago):
    """去掉 returnCountOnly 会对每个候选拉全量要素 —— 这个 owner 名下有
    250 万和 347 万要素的图层, 而工具要逐个探活约 174 个候选。"""
    resolver.probe(_proxy(LIVE_ID))
    probes = [u for u, _h in ago["calls"] if "/query?" in u]
    assert probes
    for u in probes:
        assert "returnCountOnly=true" in u, u
        assert "where=" in u, u


def test_a_non_integer_count_is_rejected_not_just_a_missing_one(ago):
    """二轮修的是"没有 count 键", 但字符串/浮点同样不是一个真实图层的答案。"""
    for bad in ("2556671", 12.5, None, [], {}):
        odd = "0e" + "0" * 30
        ago["items"][odd] = _item(odd)
        ago["query"][odd] = {"count": bad} if bad is not None else {}
        verdict, _why, _c = resolver.probe(_proxy(odd))
        assert verdict == resolver.UNKNOWN, f"count={bad!r} 不该算活"


def test_a_queried_item_with_no_service_url_is_unknown_not_dead(ago):
    """没探过就宣告死亡, 然后去找替代 —— 这正是"换掉一个健康的"那条路。"""
    nourl = "0d" + "0" * 30
    entry = _item(nourl)
    entry["url"] = ""
    ago["items"][nourl] = entry
    ago["search"] = [_item(LIVE_ID)]
    out = resolver.resolve(nourl)
    assert out["queried"]["verdict"] == resolver.UNKNOWN
    assert out["replacements"] == []
    assert not any("/search?" in u for u, _h in ago["calls"])


def test_a_guessed_layer_zero_rejecting_the_query_is_unknown_not_dead(ago):
    """根节点报错时我们猜了 /0。猜错层会得到 400 "Invalid or missing input
    parameters" —— 那是"猜错了"的证据, 不是"服务死了"的证据。"""
    tricky = "0c" + "0" * 30
    ago["items"][tricky] = _item(tricky)
    ago["meta"][tricky] = {"error": {"code": 499, "message": "Token Required"}}
    ago["query"][tricky] = {"error": {"code": 400,
                                      "message": "Invalid or missing input parameters."}}
    verdict, why, _ = resolver.probe(_proxy(tricky))
    assert verdict == resolver.UNKNOWN, f"{verdict}: {why}"
    assert "guessed" in why


def test_an_empty_incumbent_is_flagged_rather_than_called_fine(ago, capsys):
    """空图层这条规则原来只施加在候选身上, 而现任才是真正在给生产打分的那个。"""
    ago["live"][DEAD] = 0
    rc = resolver.main(["prog", DEAD])
    assert rc == 1, "空的现任不该是干净的 0"
    err = capsys.readouterr().err
    assert "EMPTY" in err and "not in zone" in err
