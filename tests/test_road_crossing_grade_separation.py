"""road_crossings must not treat a bridge/tunnel/stacked motorway as a ground barrier."""

import duckdb
import pytest

from property_scores.common.overture import road_crossings

LAT, LNG = -33.86877, 151.14501
TARGETS = [("east", 151.1465, -33.86877), ("west", 151.1445, -33.86877)]
# The motorway runs x=151.1455, y -33.871 (fraction 0) .. -33.867 (fraction 1);
# the property->east path crosses it at fraction ~0.5575.
FLAG_T = 'STRUCT("values" VARCHAR[], "between" DOUBLE[])[]'
LEVEL_T = 'STRUCT("value" INTEGER, "between" DOUBLE[])[]'


def _db():
    db = duckdb.connect()
    db.install_extension("spatial")
    db.load_extension("spatial")
    return db


def _parquet(tmp_path, flags="NULL", level="NULL", with_columns=True,
             road_class="motorway", extra_rows=""):
    path = tmp_path / "roads.parquet"
    db = _db()
    cols = ("id, subtype, class, geometry, bbox"
            + (", road_flags, level_rules" if with_columns else ""))
    extra = (f", ({flags})::{FLAG_T}, ({level})::{LEVEL_T}"
             if with_columns else "")
    db.execute(f"""
        CREATE TABLE roads AS
        SELECT * FROM (VALUES
          ('m', 'road', '{road_class}',
           ST_GeomFromText('LINESTRING(151.1455 -33.871,151.1455 -33.867)'),
           struct_pack(xmin := 151.1455, xmax := 151.1455,
                       ymin := -33.871, ymax := -33.867){extra}
          ){extra_rows}
        ) AS t({cols})
    """)
    db.execute("COPY roads TO ? (FORMAT PARQUET)", [str(path)])
    return str(path)


def _hit(tmp_path, **kw):
    return road_crossings(_db(), LAT, LNG, TARGETS, source=_parquet(tmp_path, **kw))


def test_at_grade_motorway_blocks_only_the_far_side(tmp_path):
    assert _hit(tmp_path) == {"east"}


@pytest.mark.parametrize("flag", ["is_bridge", "is_tunnel"])
def test_whole_segment_bridge_or_tunnel_does_not_block(tmp_path, flag):
    assert _hit(tmp_path, flags=f"[{{'values': ['{flag}'], 'between': NULL}}]") == set()


def test_full_range_between_counts_as_whole_segment(tmp_path):
    flags = "[{'values': ['is_tunnel'], 'between': [0.0, 1.0]}]"
    assert _hit(tmp_path, flags=flags) == set()


def test_stacked_level_rule_does_not_block_but_level_zero_does(tmp_path):
    assert _hit(tmp_path, level="[{'value': 1, 'between': NULL}]") == set()
    assert _hit(tmp_path, level="[{'value': -1, 'between': NULL}]") == set()
    assert _hit(tmp_path, level="[{'value': 0, 'between': NULL}]") == {"east"}


def test_partial_bridge_only_counts_where_the_path_crosses_it(tmp_path):
    away = "[{'values': ['is_bridge'], 'between': [0.0, 0.2]}]"
    over = "[{'values': ['is_bridge'], 'between': [0.4, 0.7]}]"
    assert _hit(tmp_path, flags=away) == {"east"}
    assert _hit(tmp_path, flags=over) == set()


def test_unrelated_flags_do_not_grade_separate(tmp_path):
    flags = "[{'values': ['is_under_construction'], 'between': NULL}]"
    assert _hit(tmp_path, flags=flags) == {"east"}


def test_bridge_does_not_hide_a_second_at_grade_motorway_on_the_path(tmp_path):
    other = """, ('g', 'road', 'trunk',
           ST_GeomFromText('LINESTRING(151.1460 -33.871,151.1460 -33.867)'),
           struct_pack(xmin := 151.1460, xmax := 151.1460,
                       ymin := -33.871, ymax := -33.867),
           NULL::STRUCT("values" VARCHAR[], "between" DOUBLE[])[],
           NULL::STRUCT("value" INTEGER, "between" DOUBLE[])[])"""
    flags = "[{'values': ['is_bridge'], 'between': NULL}]"
    assert _hit(tmp_path, flags=flags, extra_rows=other) == {"east"}


def test_parquet_without_flag_columns_keeps_conservative_2d_behaviour(tmp_path):
    assert _hit(tmp_path, with_columns=False) == {"east"}
