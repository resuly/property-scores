"""QLD 火险真值哨兵: BPA 2026 矢量瓦片上的点在多边形判定。

背景: 旧代理条目 3ec80e95... 已被上游删除 (400 CONT_0001 "Item does not
exist")。PublicSafetyQld_Data 于 2026-10-29 发布 "BPA Whole of State"
(BPA_2026), 只有矢量瓦片, 没有可 query 的要素服务。所以哨兵改为取 z14
瓦片, 解码后做点在多边形判定, 并核对 schema 指纹 (source-layer 名与
_symbol 取值集合), 不只看 HTTP 200。

夹具是 2026-11-06 从上游取的真实瓦片字节, 不是自己编码的:
  tamborine_z14_9519_15164.pbf      Tamborine 所在瓦片, 4841 字节
  brisbane_cbd_z14_9493_15156.pbf   Brisbane CBD 所在瓦片, HTTP 200 + 0 字节
"""
import gzip
import importlib.util
import json
import sys
from pathlib import Path

import mapbox_vector_tile
import pytest

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "qld_bpa_2026"
TAMBORINE = (FIX / "tamborine_z14_9519_15164.pbf").read_bytes()
CBD = (FIX / "brisbane_cbd_z14_9493_15156.pbf").read_bytes()

_SPEC = importlib.util.spec_from_file_location(
    "score_truth_probes", ROOT / "scripts" / "score_truth_probes.py")
probes = importlib.util.module_from_spec(_SPEC)
sys.modules["score_truth_probes"] = probes
_SPEC.loader.exec_module(probes)

SERVICE = "/BPA_2026/VectorTileServer/"


def _qld_canaries():
    canaries = json.loads((ROOT / "data" / "truth_anchors" / "canaries.json").read_text())
    return [c for c in canaries if SERVICE in c["url"]]


def _by_expect(want):
    out = [c for c in _qld_canaries() if c["assert"] == want]
    assert len(out) == 1, out
    return out[0]


POS = None
NEG = None


@pytest.fixture(autouse=True)
def _canaries():
    global POS, NEG
    POS = _by_expect("value=2")
    NEG = _by_expect("empty")


def _reencode(body, rename_layer=None, symbol=None, drop_attr=False, extra_layer=False):
    """Re-encode the REAL tile with one deliberate schema drift."""
    d = mapbox_vector_tile.decode(body)
    (name, layer), = d.items()
    feats = []
    for f in layer["features"]:
        props = dict(f["properties"])
        if drop_attr:
            props.pop("_symbol", None)
        elif symbol is not None:
            props["_symbol"] = symbol
        feats.append({"geometry": f["geometry"], "properties": props})
    layers = [{"name": rename_layer or name, "features": feats}]
    if extra_layer:
        layers.append({"name": "WOS_PIB_sp", "features": feats[:1]})
    return mapbox_vector_tile.encode(
        layers,
        default_options={"extents": layer["extent"]})


# ---- canaries.json wiring -------------------------------------------------

def test_no_active_canary_left_on_the_deleted_qfd_item():
    canaries = json.loads((ROOT / "data" / "truth_anchors" / "canaries.json").read_text())
    dead = [c for c in canaries if c.get("active", True)
            and ("3ec80e95" in c["url"] or "8ac1ba8e" in c["url"])]
    assert dead == []


def test_qld_has_a_positive_and_a_negative_tile_canary():
    qld = _qld_canaries()
    assert len(qld) == 2 and all(c["active"] for c in qld)
    for c in qld:
        assert c["kind"] == "vector_tile_point"
        assert c["z"] == 14
        assert c["source_layer"] == "WOS_BPA_sp"
        assert c["attribute"] == "_symbol"
        assert sorted(c["allowed_values"]) == [0, 1, 2]
    assert (POS["lat"], POS["lng"]) == (-27.97564, 153.20378)
    assert probes.tile_url(POS) == (
        "https://tiles.arcgis.com/tiles/vkTwD8kHw2woKBqV/arcgis/rest/services/"
        "BPA_2026/VectorTileServer/tile/14/9519/15164.pbf")
    assert probes.tile_url(NEG).endswith("/tile/14/9493/15156.pbf")


# ---- point-in-polygon on the real bytes -----------------------------------

def test_tamborine_hits_very_high():
    ok, note = probes.check_vector_tile_point(POS, TAMBORINE)
    assert ok, note
    assert "_symbol=2" in note


def test_cbd_empty_tile_passes_the_negative():
    assert CBD == b""
    ok, note = probes.check_vector_tile_point(NEG, CBD)
    assert ok, note


def test_negative_assert_fails_when_the_point_is_inside_a_polygon():
    # Tamborine coords + negative assert: a hit must turn the canary red.
    ok, note = probes.check_vector_tile_point({**POS, "assert": "empty"}, TAMBORINE)
    assert not ok, note


def test_positive_fails_on_an_empty_tile():
    # A rebuilt or emptied service answering 200 + 0 bytes everywhere must not
    # read as alive.
    ok, note = probes.check_vector_tile_point(POS, b"")
    assert not ok, note


def test_positive_fails_when_the_point_lands_in_a_different_class():
    ok, note = probes.check_vector_tile_point({**POS, "assert": "value=1"}, TAMBORINE)
    assert not ok and "_symbol=2" in note, note


def test_a_point_outside_every_polygon_is_not_a_hit():
    # Same real tile, a point near its NW corner whose y-mirror IS inside a
    # polygon. Guards a y-axis flip (MVT rows run top-down; the library's
    # default flips them).
    probe = {**POS, "lat": -27.97595, "lng": 153.19443}
    assert probes.tile_xy(probe) == (15164, 9519)
    ok_neg, note = probes.check_vector_tile_point({**probe, "assert": "empty"}, TAMBORINE)
    ok_pos, _ = probes.check_vector_tile_point(probe, TAMBORINE)
    assert ok_neg and not ok_pos, note


def test_gzip_wrapped_tile_still_passes():
    ok, note = probes.check_vector_tile_point(POS, gzip.compress(TAMBORINE))
    assert ok, note
    assert "_symbol=2" in note


def test_unknown_assert_value_is_not_ok():
    ok, note = probes.check_vector_tile_point({**POS, "assert": "foo"}, TAMBORINE)
    assert not ok and "unknown" in note, note


def test_tile_xy_truncates_instead_of_rounding():
    # z14, n=16384. Pick the in-tile fractions fx=15164.8, fy=9519.9 (both > .5,
    # so round() would give (15165, 9520)). Invert web mercator:
    #   lng = fx/n*360-180 = 153.21094
    #   lat = degrees(atan(sinh(pi*(1-2*fy/n)))) = -27.99246
    # tile_xy must truncate to (15164, 9519), the same tile as Tamborine.
    c = {**POS, "lat": -27.99246, "lng": 153.21094}
    assert probes.tile_xy(c) == (15164, 9519)


# ---- schema fingerprint ---------------------------------------------------

@pytest.mark.parametrize("drift", [
    {"rename_layer": "BPA_2027_sp"},
    {"extra_layer": True},
    {"symbol": 5},
    {"drop_attr": True},
])
def test_schema_drift_fails_even_with_a_valid_tile(drift):
    body = _reencode(TAMBORINE, **drift)
    ok, note = probes.check_vector_tile_point(POS, body)
    assert not ok, (drift, note)
    assert "schema" in note


def test_reencoded_tile_without_drift_still_passes():
    # Proves the drift tests fail because of the drift, not the re-encode.
    ok, note = probes.check_vector_tile_point(POS, _reencode(TAMBORINE))
    assert ok, note


def test_schema_drift_on_the_negative_tile_also_fails():
    body = _reencode(TAMBORINE, symbol=7)
    probe = {**NEG, "lat": POS["lat"], "lng": POS["lng"]}
    ok, note = probes.check_vector_tile_point(probe, body)
    assert not ok and "schema" in note, note


def test_an_error_body_is_not_a_tile():
    body = b'{"error":{"code":400,"messageCode":"CONT_0001"}}'
    for c in (POS, NEG):
        ok, note = probes.check_vector_tile_point(c, body)
        assert not ok and "text body" in note, note


# ---- run_canaries end to end ----------------------------------------------

class _Resp:
    def __init__(self, body, status=200):
        self.body, self.status = body, status

    def read(self):
        return self.body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _run(monkeypatch, tmp_path, served):
    only = [POS, NEG]
    p = tmp_path / "canaries.json"
    p.write_text(json.dumps(only))
    monkeypatch.setattr(probes, "CANARIES", p)
    monkeypatch.setattr(probes.time, "sleep", lambda s: None)
    seen = []

    def fake(req, timeout=None):
        seen.append(req.full_url)
        return served(req.full_url)

    monkeypatch.setattr(probes.urllib.request, "urlopen", fake)
    return probes.run_canaries(), seen


def test_run_canaries_passes_both_on_the_real_tiles(monkeypatch, tmp_path):
    tiles = {probes.tile_url(POS): TAMBORINE, probes.tile_url(NEG): CBD}
    out, seen = _run(monkeypatch, tmp_path, lambda u: _Resp(tiles[u]))
    assert [r["status"] for r in out] == ["PASS", "PASS"], out
    assert seen == [probes.tile_url(POS), probes.tile_url(NEG)]


def test_run_canaries_fails_when_upstream_is_unreachable(monkeypatch, tmp_path):
    def dead(u):
        raise OSError("connection refused")
    out, _ = _run(monkeypatch, tmp_path, dead)
    assert [r["status"] for r in out] == ["FAIL", "FAIL"], out


def test_a_layer_with_no_features_is_drift_not_an_empty_tile():
    # Upstream emptying the layer must not let the negative canary pass as if
    # the tile were legitimately blank.
    body = mapbox_vector_tile.encode([{"name": "WOS_BPA_sp", "features": []}])
    if not mapbox_vector_tile.decode(body):
        pytest.skip("encoder drops empty layers; nothing to fingerprint")
    ok, note = probes.check_vector_tile_point(NEG, body)
    assert not ok and "schema" in note, note


def test_missing_dependency_fails_closed(monkeypatch):
    # The production box may lack the `probes` extra; that must read red.
    monkeypatch.setitem(sys.modules, "mapbox_vector_tile", None)
    for c in (POS, NEG):
        ok, note = probes.check_vector_tile_point(c, CBD)
        assert not ok and "dependency missing" in note, note


def test_check_exception_fails_that_canary_without_aborting_the_run(monkeypatch, tmp_path):
    real = probes.check_vector_tile_point

    def boom(c, body):
        if c is POS or c["name"] == POS["name"]:
            raise RuntimeError("decoder blew up")
        return real(c, body)

    monkeypatch.setattr(probes, "check_vector_tile_point", boom)
    tiles = {probes.tile_url(POS): TAMBORINE, probes.tile_url(NEG): CBD}
    out, _ = _run(monkeypatch, tmp_path, lambda u: _Resp(tiles[u]))
    assert [r["status"] for r in out] == ["FAIL", "PASS"], out
    assert "check error: RuntimeError" in out[0]["note"]
