"""Isolate per-call duckdb connection + spatial-extension + parquet-metadata overhead.

Answers: does get_db() open a NEW connection / re-read parquet metadata every call?
Quantifies the per-call connection/metadata cost in ms.
"""
import statistics
import time

import duckdb

from property_scores.common.overture import get_db, roads_near
from property_scores.common.config import data_path

ROADS = str(data_path("overture_roads.parquet"))
LAT, LNG = -37.81, 144.96


def t(fn, n=10):
    fn()  # warm
    xs = []
    for _ in range(n):
        a = time.perf_counter()
        fn()
        xs.append((time.perf_counter() - a) * 1000)
    return statistics.median(xs), min(xs), max(xs)


# 1) bare duckdb.connect()
def bare_connect():
    duckdb.connect().close()


# 2) connect + load spatial extension (what get_db does every call)
def connect_load_spatial():
    db = duckdb.connect()
    db.load_extension("spatial")
    db.close()


# 3) full get_db()
def full_get_db():
    get_db().close()


# 4) On a PERSISTENT connection: re-run roads_near query (forces read_parquet
#    metadata read each time on the SAME connection)
_persistent = get_db()
def query_persistent():
    roads_near(_persistent, LAT, LNG, 500)


# 5) read_parquet metadata only (no spatial predicate) on persistent conn
def parquet_meta_persistent():
    _persistent.execute(f"SELECT count(*) FROM read_parquet('{ROADS}') WHERE bbox.xmin BETWEEN {LNG-0.01} AND {LNG+0.01} AND bbox.ymin BETWEEN {LAT-0.01} AND {LAT+0.01}").fetchall()


# 6) fresh connection each query (current production behaviour): connect+load+query
def fresh_conn_query():
    db = get_db()
    roads_near(db, LAT, LNG, 500)
    db.close()


for name, fn in [
    ("1. bare duckdb.connect()",            bare_connect),
    ("2. connect + load_extension(spatial)", connect_load_spatial),
    ("3. full get_db()",                     full_get_db),
    ("4. roads_near on PERSISTENT conn",     query_persistent),
    ("5. parquet meta count PERSISTENT",     parquet_meta_persistent),
    ("6. fresh get_db()+roads_near+close",   fresh_conn_query),
]:
    med, lo, hi = t(fn, 10)
    print(f"{name:42s} median {med:8.2f} ms  (min {lo:6.2f} max {hi:6.2f})")

print()
print("Per-call connection overhead (get_db, line 3) is paid on EVERY noise_score call.")
print("Compare line 4 (query on persistent conn) vs line 6 (fresh conn + query).")
