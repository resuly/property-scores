"""FastAPI entry point for property scores."""

from fastapi import FastAPI, Query

from property_scores.noise import noise_score
from property_scores.walkability import walkability_score
from property_scores.solar import solar_score

app = FastAPI(
    title="Property Scores API",
    description="Open-data property intelligence scoring engine",
    version="0.1.0",
)


@app.get("/scores")
def get_all_scores(
    lat: float = Query(..., description="Latitude (WGS84)"),
    lng: float = Query(..., description="Longitude (WGS84)"),
    source_roads: str | None = Query(None, description="Local roads parquet"),
    source_pois: str | None = Query(None, description="Local POI parquet"),
):
    return {
        "lat": lat,
        "lng": lng,
        "noise": noise_score(lat, lng, source=source_roads),
        "walkability": walkability_score(lat, lng, source=source_pois),
        "solar": solar_score(lat, lng),
    }


@app.get("/scores/noise")
def get_noise(
    lat: float = Query(...), lng: float = Query(...),
    radius: int = Query(1000), source: str | None = Query(None),
):
    return noise_score(lat, lng, radius, source=source)


@app.get("/scores/walkability")
def get_walkability(
    lat: float = Query(...), lng: float = Query(...),
    radius: int = Query(1500), source: str | None = Query(None),
):
    return walkability_score(lat, lng, radius, source=source)


@app.get("/scores/solar")
def get_solar(
    lat: float = Query(...), lng: float = Query(...),
    roof_area: float | None = Query(None),
    orientation: str = Query("optimal"),
):
    return solar_score(lat, lng, roof_area_m2=roof_area, orientation=orientation)
