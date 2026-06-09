"""Break transfer_feats into its 3 duckdb queries + raster, on a persistent conn."""
import math
import statistics
import time

from property_scores.common.overture import get_db
from property_scores.noise import transfer as T
from property_scores.noise import raster_sample as rs

LAT, LNG = -37.81, 144.96
db = get_db()
mpd = 111_320 * math.cos(math.radians(LAT))
deg = 0.013

ROADS = str(T._DATA_DIR / "overture_roads.parquet")
BLDG = str(T._DATA_DIR / "overture_buildings.parquet")
POIS = str(T._DATA_DIR / "overture_pois.parquet")


def q_roads():
    db.execute(f"""SELECT class, ST_Distance(geometry, ST_Point({LNG},{LAT}))*{mpd} d
        FROM read_parquet('{ROADS}')
        WHERE bbox.xmin BETWEEN {LNG-deg} AND {LNG+deg} AND bbox.ymin BETWEEN {LAT-deg} AND {LAT+deg}
          AND subtype='road' AND ST_Distance(geometry, ST_Point({LNG},{LAT})) < {1000/mpd}""").fetchall()

def q_bldg():
    db.execute(f"""SELECT COALESCE(height,6.0), ST_X(ST_Centroid(geometry)), ST_Y(ST_Centroid(geometry))
        FROM read_parquet('{BLDG}')
        WHERE bbox.xmin BETWEEN {LNG-0.004} AND {LNG+0.004} AND bbox.ymin BETWEEN {LAT-0.003} AND {LAT+0.003}""").fetchall()

def q_pois():
    db.execute(f"""SELECT ST_X(geometry), ST_Y(geometry) FROM read_parquet('{POIS}')
        WHERE bbox.xmin BETWEEN {LNG-0.006} AND {LNG+0.006} AND bbox.ymin BETWEEN {LAT-0.0045} AND {LAT+0.0045}""").fetchall()

def q_raster():
    rs.sample(T.DEM, LAT, LNG, default=float("nan"))
    rs.window_stats(T.DEM, LAT, LNG, 300)
    rs.window_stats(T.LC, LAT, LNG, 300, categorical=True, classes=list(T.LC_CLASSES.keys()))
    rs.window_stats(T.LC, LAT, LNG, 100, categorical=True, classes=[50])


def t(fn, n=10):
    fn(); fn()
    xs = []
    for _ in range(n):
        a = time.perf_counter(); fn(); xs.append((time.perf_counter()-a)*1000)
    return statistics.median(xs)


for name, fn in [
    ("transfer roads read_parquet",     q_roads),
    ("transfer buildings read_parquet", q_bldg),
    ("transfer pois read_parquet",      q_pois),
    ("transfer raster DEM+LC (4 reads)", q_raster),
]:
    print(f"{name:38s} median {t(fn):7.2f} ms")
