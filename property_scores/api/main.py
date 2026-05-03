"""FastAPI entry point for property scores."""

import logging
import os
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import property_scores.common.config  # noqa: F401 — ensure .env is loaded
from property_scores.noise import noise_score, aircraft_noise_penalty
from property_scores.noise.cache import lookup as noise_cache_lookup
from property_scores.noise.debug import noise_debug
from property_scores.walkability import walkability_score
from property_scores.solar import solar_score
from property_scores.flood import flood_score
from property_scores.flood.cache import lookup as flood_cache_lookup
from property_scores.bushfire import bushfire_score
from property_scores.heat_island import heat_island_score
from property_scores.view_quality import view_quality_score
from property_scores.contamination import contamination_score

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

DISCLAIMER = (
    "Scores are estimates based on open data and are not professional assessments. "
    "Do not rely on these scores for insurance, legal, or financial decisions. "
    "Flood, bushfire, and contamination scores do not replace site-specific investigations."
)

VIEW_QUALITY_CAVEAT = (
    "Based on proximity to landscape features and building density, "
    "not actual line-of-sight analysis. A high score does not guarantee unobstructed views."
)

app = FastAPI(
    title="Property Scores API",
    description="Open-data property intelligence scoring engine",
    version="0.1.0",
)


@app.get("/api/config")
def get_config():
    return {"mapbox_token": os.getenv("MAPBOX_TOKEN", "")}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/solar")
def solar_page():
    return FileResponse(STATIC_DIR / "solar.html")


@app.get("/noise")
def noise_page():
    return FileResponse(STATIC_DIR / "noise.html")


@app.get("/noise/debug")
def noise_debug_page():
    return FileResponse(STATIC_DIR / "noise-debug.html")


@app.get("/walkability")
def walkability_page():
    return FileResponse(STATIC_DIR / "walkability.html")


@app.get("/flood")
def flood_page():
    return FileResponse(STATIC_DIR / "flood.html")


@app.get("/bushfire")
def bushfire_page():
    return FileResponse(STATIC_DIR / "bushfire.html")


@app.get("/heat-island")
def heat_island_page():
    return FileResponse(STATIC_DIR / "heat_island.html")


@app.get("/view-quality")
def view_quality_page():
    return FileResponse(STATIC_DIR / "view_quality.html")


@app.get("/contamination")
def contamination_page():
    return FileResponse(STATIC_DIR / "contamination.html")


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
        "disclaimer": DISCLAIMER,
        "noise": noise_score(lat, lng, source=source_roads),
        "walkability": walkability_score(lat, lng, source=source_pois),
        "solar": solar_score(lat, lng),
        "flood": flood_score(lat, lng),
        "bushfire": bushfire_score(lat, lng),
        "heat_island": heat_island_score(lat, lng),
        "view_quality": view_quality_score(lat, lng),
        "contamination": contamination_score(lat, lng),
    }


@app.get("/scores/noise")
def get_noise(
    lat: float = Query(...), lng: float = Query(...),
    radius: int = Query(1000), source: str | None = Query(None),
    nocache: bool = Query(False),
):
    try:
        if not nocache and not source:
            cached = noise_cache_lookup(lat, lng)
            if cached:
                return cached
        return noise_score(lat, lng, radius, source=source)
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    except Exception as e:
        logger.exception("noise score failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/scores/walkability")
def get_walkability(
    lat: float = Query(...), lng: float = Query(...),
    radius: int = Query(1500), source: str | None = Query(None),
):
    try:
        return walkability_score(lat, lng, radius, source=source)
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    except Exception as e:
        logger.exception("walkability score failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/scores/solar")
def get_solar(
    lat: float = Query(...), lng: float = Query(...),
    roof_area: float | None = Query(None),
    orientation: str = Query("optimal"),
):
    return solar_score(lat, lng, roof_area_m2=roof_area, orientation=orientation)


@app.get("/scores/flood")
def get_flood(lat: float = Query(...), lng: float = Query(...),
              nocache: bool = Query(False)):
    try:
        if not nocache:
            cached = flood_cache_lookup(lat, lng)
            if cached:
                return cached
        return flood_score(lat, lng)
    except Exception as e:
        logger.exception("flood score failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/scores/bushfire")
def get_bushfire(lat: float = Query(...), lng: float = Query(...)):
    try:
        return bushfire_score(lat, lng)
    except Exception as e:
        logger.exception("bushfire score failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/scores/heat-island")
def get_heat_island(lat: float = Query(...), lng: float = Query(...)):
    try:
        return heat_island_score(lat, lng)
    except Exception as e:
        logger.exception("heat island score failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/scores/view-quality")
def get_view_quality(lat: float = Query(...), lng: float = Query(...)):
    try:
        return view_quality_score(lat, lng)
    except Exception as e:
        logger.exception("view quality score failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/scores/contamination")
def get_contamination(lat: float = Query(...), lng: float = Query(...)):
    try:
        return contamination_score(lat, lng)
    except Exception as e:
        logger.exception("contamination score failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/scores/noise/debug")
def get_noise_debug(
    lat: float = Query(...), lng: float = Query(...),
    radius: int = Query(500),
):
    try:
        return noise_debug(lat, lng, radius)
    except Exception as e:
        logger.exception("noise debug failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/scores/aircraft-noise")
def get_aircraft_noise(lat: float = Query(...), lng: float = Query(...)):
    """Query airport noise overlay (MAEO/AEO) for a coordinate."""
    try:
        return aircraft_noise_penalty(lat, lng)
    except Exception as e:
        logger.exception("aircraft noise query failed")
        return JSONResponse({"error": str(e)}, status_code=500)


app.mount("/css", StaticFiles(directory=STATIC_DIR / "css"), name="css")
