"""FastAPI entry point for property scores."""

import logging
import os
import time
from collections import defaultdict
from pathlib import Path
from threading import Lock

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import property_scores.common.config  # noqa: F401 — ensure .env is loaded
from property_scores.noise import noise_score, aircraft_noise_penalty
from property_scores.noise.cache import lookup as noise_cache_lookup
from property_scores.noise.debug import noise_debug
from property_scores.noise.terrain import elevation_profile
from property_scores.walkability import walkability_score
from property_scores.solar import solar_score
from property_scores.flood import flood_score
from property_scores.flood.cache import lookup as flood_cache_lookup
from property_scores.bushfire import bushfire_score
from property_scores.heat_island import heat_island_score
from property_scores.view_quality import view_quality_score
from property_scores.contamination import contamination_score
from property_scores.common.overture import building_footprint_m2, get_db

logger = logging.getLogger(__name__)


def _solar_with_footprint(lat: float, lng: float) -> dict:
    """Solar score with roof area auto-derived from the Overture building
    footprint containing the point, so estimated_annual_kwh populates without
    the caller supplying roof_area (it never did; the field was always null
    in the batch payload until 2026-07-16). Whole-building semantics for
    strata — labelled via roof_area_source."""
    roof_m2 = None
    try:
        roof_m2 = building_footprint_m2(get_db(), lat, lng)
    except Exception:
        logger.warning("building footprint lookup failed", exc_info=True)
    result = solar_score(lat, lng, roof_area_m2=roof_m2)
    if roof_m2:
        result["roof_area_m2"] = round(roof_m2)
        result["roof_area_source"] = (
            "building_footprint: the whole building containing this point "
            "(not a per-unit share), panels across the full footprint at 20% "
            "efficiency")
    return result


def _noise_for_batch(lat: float, lng: float, source: str | None = None,
                     detail: bool = False) -> dict:
    """Batch noise component, optionally carrying its per-source breakdown.

    The detail path skips the Overture road segments: they are ODbL-1.0 and the
    only consumer of this path (the licensed property feed) strips them anyway,
    so computing barrier screening for ~30 segments per address would be pure
    waste.
    """
    if not detail:
        return noise_score(lat, lng, source=source)
    debug = noise_debug(lat, lng, 500, include_overture_roads=False)
    result = dict(debug.get("score") or {})
    if debug.get("sources"):
        result["sources"] = debug["sources"]
    if debug.get("terrain_source"):
        result["terrain_source"] = debug["terrain_source"]
    return result

# ---------------------------------------------------------------------------
# Application-level rate limiter (supplements nginx 5r/s)
# ---------------------------------------------------------------------------
_rate_hits: dict[str, list[float]] = defaultdict(list)
_rate_lock = Lock()


def _check_rate(ip: str, window: int = 60, limit: int = 90) -> bool:
    now = time.monotonic()
    with _rate_lock:
        hits = _rate_hits[ip]
        cutoff = now - window
        _rate_hits[ip] = hits = [t for t in hits if t > cutoff]
        if len(hits) >= limit:
            return False
        hits.append(now)
        if len(_rate_hits) > 5000:
            stale = [k for k, v in _rate_hits.items() if not v or v[-1] < cutoff]
            for k in stale:
                del _rate_hits[k]
    return True


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

from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8000", "http://localhost:8001",
                    "http://127.0.0.1:8000", "http://127.0.0.1:8001",
                    "https://daleads.com.au", "https://www.daleads.com.au"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _check_bushfire_data():
    """Warn loudly if the WorldCover mosaic is missing: bushfire fuel silently
    falls back to the weaker Overture proxy, which monitoring would not catch."""
    try:
        from property_scores.bushfire.score import lc_vrt_available, _LC_VRT
        if not lc_vrt_available():
            logger.error("STARTUP: WorldCover lc.vrt missing at %s — bushfire fuel "
                         "will degrade to the building-density proxy", _LC_VRT)
        else:
            logger.info("STARTUP: WorldCover lc.vrt present — bushfire fuel uses 10m land cover")
    except Exception:
        logger.exception("STARTUP: bushfire land-cover readiness check failed")


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


# Overall wall-clock budget for the WHOLE /scores batch (shared deadline, not
# per-component): slow/stuck upstreams degrade to an error marker, not block.
_BATCH_DEADLINE_S = 25


@app.get("/scores")
def get_all_scores(
    lat: float = Query(..., description="Latitude (WGS84)"),
    lng: float = Query(..., description="Longitude (WGS84)"),
    source_roads: str | None = Query(None, description="Local roads parquet"),
    source_pois: str | None = Query(None, description="Local POI parquet"),
    noise_detail: bool = Query(False, description="Include road/rail source details"),
):
    from concurrent.futures import ThreadPoolExecutor

    components = {
        "noise": lambda: _noise_for_batch(lat, lng, source=source_roads,
                                          detail=noise_detail),
        "walkability": lambda: walkability_score(lat, lng, source=source_pois),
        "solar": lambda: _solar_with_footprint(lat, lng),
        "flood": lambda: flood_score(lat, lng),
        "bushfire": lambda: bushfire_score(lat, lng),
        "heat_island": lambda: heat_island_score(lat, lng),
        "view_quality": lambda: view_quality_score(lat, lng),
        "contamination": lambda: contamination_score(lat, lng),
        "aircraft_noise": lambda: aircraft_noise_penalty(lat, lng),
    }
    out = {"lat": lat, "lng": lng, "disclaimer": DISCLAIMER}
    pool = ThreadPoolExecutor(max_workers=len(components))
    futures: dict = {}
    try:
        futures = {name: pool.submit(fn) for name, fn in components.items()}
        deadline = time.monotonic() + _BATCH_DEADLINE_S
        for name, fut in futures.items():
            try:
                out[name] = fut.result(timeout=max(0.1, deadline - time.monotonic()))
            except Exception as e:
                logger.exception("batch /scores component %s failed", name)
                out[name] = {"error": str(e) or e.__class__.__name__}
    finally:
        # Don't join stragglers: a stuck upstream must not block the response.
        pool.shutdown(wait=False, cancel_futures=True)
        stuck = [name for name, fut in futures.items() if not fut.done()]
        if stuck:
            # Grep-able ops signal: leaked threads accumulate until restart.
            logger.error("STRAGGLER score threads abandoned at deadline: %s",
                         ", ".join(stuck))
    return out


@app.get("/scores/noise")
def get_noise(
    request: Request,
    lat: float = Query(...), lng: float = Query(...),
    radius: int = Query(500), source: str | None = Query(None),
    nocache: bool = Query(False),
    detail: bool = Query(False),
):
    ip = request.headers.get("x-real-ip") or request.client.host
    limit = 10 if detail else 30
    if not _check_rate(ip, limit=limit):
        return JSONResponse({"error": "Rate limit exceeded."}, status_code=429)
    try:
        if detail:
            return noise_debug(lat, lng, radius)
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


@app.get("/scores/flood/inundation")
def get_flood_inundation(lat: float = Query(...), lng: float = Query(...),
                         radius: int = Query(500)):
    """DEM grid relative to the local drainage line, for the map's water-level
    simulation overlay. Terrain fill illustration, not a hydraulic model."""
    from property_scores.flood.score import inundation_grid
    try:
        grid = inundation_grid(lat, lng, radius_m=max(200, min(radius, 1000)))
        if not grid:
            return JSONResponse({"error": "no elevation coverage"}, status_code=404)
        return grid
    except Exception as e:
        logger.exception("flood inundation failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/scores/bushfire")
def get_bushfire(lat: float = Query(...), lng: float = Query(...),
                 quick: bool = Query(False)):
    try:
        return bushfire_score(lat, lng, quick=quick)
    except Exception as e:
        logger.exception("bushfire score failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/scores/bushfire/landcover")
def get_bushfire_landcover(lat: float = Query(...), lng: float = Query(...),
                           radius: int = Query(500)):
    """WorldCover 10m land-cover grid around a point, for the fuel map overlay."""
    from property_scores.bushfire.score import landcover_grid, lc_vrt_available
    if not lc_vrt_available():
        logger.error("bushfire landcover: WorldCover lc.vrt missing — fuel degraded to proxy")
        return JSONResponse({"error": "land cover data unavailable"}, status_code=503)
    try:
        grid = landcover_grid(lat, lng, radius_m=max(100, min(radius, 1500)))
        if not grid:
            return JSONResponse({"error": "no coverage"}, status_code=404)
        return grid
    except Exception as e:
        logger.exception("bushfire landcover failed")
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


@app.get("/scores/noise/terrain")
def get_noise_terrain(
    src_lat: float = Query(...), src_lng: float = Query(...),
    lat: float = Query(...), lng: float = Query(...),
):
    """DEM elevation profile from a source to the receiver. Local Copernicus DEM
    first, open-meteo only as out-of-coverage fallback; split out from
    /noise/debug so the main response stays fast."""
    try:
        profile = elevation_profile(src_lat, src_lng, lat, lng)
        if not profile:
            return JSONResponse({"error": "elevation API unavailable"}, status_code=502)
        return profile
    except Exception as e:
        logger.exception("noise terrain failed")
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
