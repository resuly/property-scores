"""Generic Overture ``school`` records are promoted to primary_school only on real school evidence.

GOLD holds the 24 records of the real (2026-04) Overture POI file that the
old rule promoted because any website contained "primary".  The labels were
made by reading each record's name, alternate taxonomy and URL (not by
running the classifier), so the classifier is judged against them, not the
other way round.  Single-labeller gold: the two ambiguous cases are marked.
"""

import duckdb
import pytest

from property_scores.common.overture import pois_near_detailed

ELEM = "elementary_school"
# (name, alternate categories, websites, is_real_primary_school)
GOLD = [
    ("St Paul's Primary School", [ELEM, "education"],
     ["http://www.mn.catholic.edu.au/schools/region-map/lakes/gateshead-st-pauls-primary-school"], True),
    ("Zithathele Primary School", None, ["http://www.zithatheleprimaryschool.co.za/"], False),  # .co.za school geolocated in Sydney
    ("St. Patricks School", ["private_school", ELEM],
     ["https://www.wf.catholic.edu.au/find-a-school/st-patricks-primary-school-trundle/"], True),
    ("Wulagi Family Centre", ["home_service", "community_center"], ["http://wulagiprimary.nt.edu.au/"], False),
    ("Manunda Terrace Pre-School", [ELEM, "education"], ["http://www.manundaterraceprimary.nt.edu.au"], False),
    ("The Springfield Anglican College", ["college_university", "education"],
     ["http://tsac.qld.edu.au/our-college/primary-schooling"], True),
    ("St Anthony’s Uniform Shop", ["uniform_store", "education"],
     ["https://www.wearitto.com.au/schools/st-anthonys-catholic-primary-school"], False),
    ("Our Lady's Catholic Primary School", [ELEM, "education"],
     ["https://www.rok.catholic.edu.au/our-lady-s-catholic-primary-school-longreach/"], True),
    ("Illawarra Primary School", ["public_school"], ["http://education.tas.edu.au/illawarraprimaryschool"], True),
    ("Illawarra Primary School Association", [ELEM], ["https://illawarraprimaryschool.education.tas.edu.au/"], False),
    ("Goulburn Street Primary School", None, ["https://goulburnstreetprimary.education.tas.edu.au/"], True),
    ("East Tamar Primary", ["education"], ["http://easttamarprimary.education.tas.edu.au"], True),
    ("Riverside Primary School, Launceston", [ELEM, "education"], ["https://riversideprimary.education.tas.edu.au/"], True),
    ("Scottsdale Primary School", ["public_school"], ["https://scottsdaleprimary.education.tas.edu.au/"], True),
    ("Kulap Primary School", None, ["https://www.schoolbuildings.vic.gov.au/kulap-primary-school"], True),
    ("Kerribana Primary School", None, ["https://www.schoolbuildings.vic.gov.au/kerribana-primary-school"], True),
    ("Meerlieu Primary School", [ELEM, "education"], ["http://www.meerlieups.vic.edu.au/Meerlieu_Primary_School/Home.html"], True),
    ("Westmeadows Primary School", [ELEM], ["http://au.localize123.com/westmeadows-primary-school-in-westmeadows-vic"], True),
    ("2018 Monday after-school Fun French (INTERMEDIATE LEVEL) for primary kids - Girton Grammar", ["education"],
     ["http://lcfclubs.com.au/product/2017-monday-after-school-fun-french-intermediate-level-for-primary-kids-girton-grammar"], False),
    ("French on Thursdays 5-12 yrs at Princes Hill Primary School, Princes Hill, Melbourne", ["education"],
     ["http://lcfclubs.com.au/store/french-on-thursdays-5-12-yrs-at-princes-hill-primary-school-princes-hill-melbourne"], False),
    ("Munglinup P&C", None, ["http://www.munglinupprimaryschool.wa.edu.au/"], False),
    ("Chapman Valley Primary School", [ELEM], ["http://www.chapmanvalleyprimaryschool.com.au/"], True),
    # ambiguous: name lacks "primary"; only the association's directory URL says PrimarySchools
    ("Rockingham John Calvin School", ["education"],
     ["http://www.frsa.asn.au/PrimarySchools/Rockingham/tabid/61/language/en-AU/Default.aspx"], True),
    ("Wickham Primary School", ["education"], ["http://www.wickhamprimaryschool.wa.edu.au/"], True),
]

# Synthetic edge cases beyond the 24 (label reasoning in the comment).
EXTRA = [
    # exact elementary_school taxonomy + primary URL, name without either word
    ("Greenfields Campus", [ELEM], ["https://greenfields.example.edu.au/primary"], True),
    # primary URL but nothing else says school: name is a generic campus, no taxonomy
    ("Greenfields Campus", None, ["https://greenfields.example.edu.au/primary"], False),
    # no primary anywhere: never promoted
    ("Riverbend College", [ELEM], ["https://riverbend.example.edu.au/"], False),
    ("Riverbend Primary School", ["tutoring_service"], ["https://riverbend.example.edu.au/primary"], False),
    ("Riverbend Primary School Before School Care", None, ["https://riverbend.example.edu.au/primary"], False),
    ("Riverbend Primary School", None, ["https://shop.example.com.au/shop/primary-uniforms"], False),
    ("Riverbend Primary School", None, ["https://riverbend.example.org.uk/primary"], False),
    ("Riverbend Primary School", None, None, False),
    # name-only signals (no alternate taxonomy to fall back on)
    ("Riverbend Primary School Uniforms", None, ["https://riverbend.example.edu.au/primary"], False),
    ("Riverbend Family Centre", None, ["https://riverbendprimaryschool.example.edu.au/"], False),
]


def _classify(tmp_path, monkeypatch, rows):
    path = tmp_path / "pois.parquet"
    db = duckdb.connect()
    db.install_extension("spatial")
    db.load_extension("spatial")
    db.execute("""CREATE TABLE pois (categories STRUCT("primary" VARCHAR, alternate VARCHAR[]),
                 names STRUCT("primary" VARCHAR), websites VARCHAR[], geometry GEOMETRY,
                 bbox STRUCT(xmin DOUBLE, xmax DOUBLE, ymin DOUBLE, ymax DOUBLE))""")
    for i, (name, alt, sites, _want) in enumerate(rows):
        x, y = 144.9600 + i * 0.0004, -37.8100
        db.execute(
            "INSERT INTO pois VALUES ({'primary': 'school', 'alternate': ?}, {'primary': ?}, ?,"
            " ST_Point(?, ?), {'xmin': ?, 'xmax': ?, 'ymin': ?, 'ymax': ?})",
            [alt, name, sites, x, y, x, x, y, y])
    db.execute("COPY pois TO ? (FORMAT PARQUET)", [str(path)])
    monkeypatch.setattr("property_scores.common.overture.data_path", lambda _n: path)
    got = {round((r[2] - 144.9600) / 0.0004): r[0]
           for r in pois_near_detailed(db, -37.8100, 144.9600, 5000)}
    assert len(got) == len(rows)
    return [got[i] for i in range(len(rows))]


def test_gold_24_real_candidates(tmp_path, monkeypatch):
    assert len(GOLD) == 24
    cats = _classify(tmp_path, monkeypatch, GOLD)
    wrong = [(g[0], c) for g, c in zip(GOLD, cats) if (c == "primary_school") != g[3]]
    assert not wrong, wrong
    assert sum(1 for c in cats if c == "primary_school") == 16
    assert all(c == "school" for c, g in zip(cats, GOLD) if not g[3])


def test_synthetic_edges(tmp_path, monkeypatch):
    cats = _classify(tmp_path, monkeypatch, EXTRA)
    wrong = [(g[0], g[2], c) for g, c in zip(EXTRA, cats) if (c == "primary_school") != g[3]]
    assert not wrong, wrong


def test_exact_taxonomy_categories_are_left_alone(tmp_path, monkeypatch):
    """Only the generic ``school`` label is rewritten; other categories pass through."""
    path = tmp_path / "pois.parquet"
    db = duckdb.connect()
    db.install_extension("spatial")
    db.load_extension("spatial")
    db.execute("""CREATE TABLE pois AS SELECT
        struct_pack("primary" := 'elementary_school', alternate := ['education']::VARCHAR[]) AS categories,
        struct_pack("primary" := 'Some Campus') AS names, ['https://x.example.edu.au/']::VARCHAR[] AS websites,
        ST_Point(144.96, -37.81) AS geometry,
        struct_pack(xmin := 144.96, xmax := 144.96, ymin := -37.81, ymax := -37.81) AS bbox""")
    db.execute("COPY pois TO ? (FORMAT PARQUET)", [str(path)])
    monkeypatch.setattr("property_scores.common.overture.data_path", lambda _n: path)
    assert pois_near_detailed(db, -37.81, 144.96, 500)[0][0] == "elementary_school"
