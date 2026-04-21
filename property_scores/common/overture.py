"""Overture Maps data loading helpers via DuckDB."""

import duckdb

_OVERTURE_S3 = "s3://overturemaps-us-west-2/release/2025.03.0/theme={theme}/type={type}/*"


def get_db() -> duckdb.DuckDBPyConnection:
    db = duckdb.connect()
    db.install_extension("spatial")
    db.load_extension("spatial")
    db.install_extension("httpfs")
    db.load_extension("httpfs")
    db.execute("SET s3_region='us-west-2'; SET s3_no_sign_request=true;")
    return db


def roads_near(db: duckdb.DuckDBPyConnection, lat: float, lng: float,
               radius_m: int = 1000, *, source: str | None = None) -> list[tuple]:
    if source:
        table = f"read_parquet('{source}')"
    else:
        table = f"read_parquet('{_OVERTURE_S3.format(theme='transportation', type='segment')}')"

    delta = radius_m / 111_000 * 1.5
    sql = f"""
        SELECT class,
               ST_Distance_Spheroid(geometry, ST_Point({lng}, {lat})) AS dist_m
        FROM {table}
        WHERE bbox.xmin BETWEEN {lng - delta} AND {lng + delta}
          AND bbox.ymin BETWEEN {lat - delta} AND {lat + delta}
          AND ST_Distance_Spheroid(geometry, ST_Point({lng}, {lat})) < {radius_m}
          AND subtype = 'road'
    """
    return db.sql(sql).fetchall()


def pois_near(db: duckdb.DuckDBPyConnection, lat: float, lng: float,
              radius_m: int = 1500, *, source: str | None = None) -> list[tuple]:
    if source:
        table = f"read_parquet('{source}')"
    else:
        table = f"read_parquet('{_OVERTURE_S3.format(theme='places', type='place')}')"

    delta = radius_m / 111_000 * 1.5
    sql = f"""
        SELECT categories.primary AS category,
               ST_Distance_Spheroid(geometry, ST_Point({lng}, {lat})) AS dist_m
        FROM {table}
        WHERE bbox.xmin BETWEEN {lng - delta} AND {lng + delta}
          AND bbox.ymin BETWEEN {lat - delta} AND {lat + delta}
          AND ST_Distance_Spheroid(geometry, ST_Point({lng}, {lat})) < {radius_m}
    """
    return db.sql(sql).fetchall()
