"""FastAPI entry point for property scores."""

import logging
import math
import os
import time
from collections import defaultdict
from pathlib import Path
from threading import Lock
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import property_scores.common.config  # noqa: F401 — ensure .env is loaded
from property_scores.noise import noise_score, aircraft_noise_penalty
from property_scores.noise.cache import lookup as noise_cache_lookup
from property_scores.noise.debug import noise_debug
from property_scores.noise.terrain import elevation_profile
from property_scores.walkability import walkability_score
from property_scores.solar import solar_score
from property_scores.flood import flood_score
from property_scores.flood.score import _detect_state as flood_detect_state
from property_scores.flood.cache import lookup as flood_cache_lookup
from property_scores.bushfire import bushfire_score
from property_scores.heat_island import heat_island_score
from property_scores.view_quality import view_quality_score
from property_scores.contamination import contamination_score
from property_scores.common.overture import building_footprint_m2, get_db

logger = logging.getLogger(__name__)


def _solar_with_footprint(lat: float, lng: float) -> dict:
    """Solar resource plus separately labelled building-footprint context.

    A footprint is not usable roof area: it has no planes, pitch, azimuth,
    setbacks, obstructions or shading.  The old batch path passed the whole
    footprint into ``solar_score`` as if panels covered 100% of it, producing a
    precise-looking annual-kWh estimate for detached houses and entire strata
    towers.  Keep the useful scalar, but never feed it into generation maths.
    """
    roof_m2 = None
    try:
        roof_m2 = building_footprint_m2(get_db(), lat, lng)
    except Exception:
        logger.warning("building footprint lookup failed", exc_info=True)
    result = solar_score(lat, lng)
    if roof_m2:
        result["building_context"] = {
            "building_footprint_m2": round(roof_m2),
            "source": "Overture Maps buildings",
            "licence": "mixed CC BY 4.0 and ODbL-1.0 inputs; derived scalar only",
            "attribution": (
                "Overture Maps Foundation and contributing data providers"),
            "semantics": (
                "whole-building ground footprint containing or nearest the "
                "point; not a per-unit share or usable roof area"),
            "used_in_generation_estimate": False,
        }
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
        # The radius travels with the sources so a consumer can tell an empty
        # group ("nothing within this radius") from missing coverage.
        radius = (debug.get("query") or {}).get("radius_m")
        if radius is not None:
            result["sources_radius_m"] = radius
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
def _resolve_score_stamp():
    """Pin the code revision to what THIS process loaded.

    Deploys are `git pull` then restart. Resolving the revision lazily on the
    first /version could land inside that window and report the new commit
    while the old code is still running -- see stamp.code_revision. Doing it
    here also puts the stamp in the startup log, so "why did every consumer
    cache flush" is answerable from the journal.
    """
    try:
        from property_scores.api import stamp
        logger.info("STARTUP: score model_stamp=%s components=%s",
                    stamp.model_stamp(), stamp.components())
    except Exception:
        logger.exception("STARTUP: score model stamp could not be resolved")


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


@app.on_event("startup")
def _warm_contamination_cadastre():
    """Move the shared 19 GB parcel DB cold-open out of request budgets."""
    try:
        from property_scores.contamination import parcel_attribution
        if parcel_attribution.warmup():
            logger.info("STARTUP: contamination cadastre ready")
        else:
            logger.warning("STARTUP: contamination cadastre unavailable; "
                           "parcel attribution will use radius fallback")
    except Exception:
        logger.exception("STARTUP: contamination cadastre warmup failed")


@app.get("/api/config")
def get_config():
    return {"mapbox_token": os.getenv("MAPBOX_TOKEN", "")}


@app.get("/version")
def get_version():
    """What this service is running, and the stamp consumers cache against.

    `model_stamp` is the contract: a consumer that cached a /scores payload
    under a different stamp must treat it as a miss. `components` is there so
    a stamp change can be explained rather than merely observed.

    Cheap on purpose because the consumer polls it. Measured on this repo:
    model_stamp() has a p50 of 0.08 ms (stats plus content hashes of three
    small artefacts; the code hash and the registry resolution behind it are
    both memoised per process, the code hash costing ~10 ms once over 51 files
    / 0.5 MB, at startup). /scores computes the same stamp per request --
    0.08 ms against that endpoint's 25 s batch deadline.

    Internal only: bound to 127.0.0.1 in production and absent from the
    da_leads proxy's endpoint allowlist (web/property_scores_proxy.py), so the
    artefact sizes and commit id in `components` are not published to the web.
    """
    from property_scores.api import stamp
    return {"model_stamp": stamp.model_stamp(),
            "components": stamp.components(),
            # Not a stamp input (a docs-only commit must not flush every
            # downstream cache -- see stamp.code_revision); here because it is
            # the first thing a human wants when asking why the stamp moved.
            "repo_head": stamp.repo_head()}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/solar")
def solar_page():
    return RedirectResponse(
        "https://daleads.com.au/property-scores/solar/", status_code=308)


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
    return RedirectResponse(
        "https://daleads.com.au/property-scores/heat-island/", status_code=308)


@app.get("/view-quality")
@app.get("/landscape-openness")
def view_quality_page():
    return RedirectResponse(
        "https://daleads.com.au/property-scores/view-quality/", status_code=308)


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
    # Stamped with what produced THIS payload, so a caching consumer records
    # provenance from the response itself rather than from a separate poll it
    # made at some other moment. /version answers "what is current"; this
    # answers "what made this", and the two together are what let a cache
    # decide. Computed before the batch runs so a component that times out
    # cannot leave the payload unstamped.
    from property_scores.api import stamp as _stamp
    out = {"lat": lat, "lng": lng, "disclaimer": DISCLAIMER,
           "model_stamp": _stamp.model_stamp()}
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
    roof_area: float | None = Query(None, gt=0),
    orientation: Literal["optimal", "east", "west", "suboptimal"] = Query(
        "optimal"),
):
    if roof_area is not None and not math.isfinite(roof_area):
        raise HTTPException(
            status_code=422,
            detail="roof_area must be a finite positive number",
        )
    return solar_score(lat, lng, roof_area_m2=roof_area, orientation=orientation)


@app.get("/scores/flood")
def get_flood(lat: float = Query(...), lng: float = Query(...),
              nocache: bool = Query(False)):
    try:
        # The legacy regional cache stores only a numeric score and cannot
        # represent PlanSA's Evidence Required / incomplete-check unknown
        # state.  Never let a stale numeric cache override live SA semantics.
        if not nocache and flood_detect_state(lat, lng) != "SA":
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
@app.get("/scores/landscape-openness")
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


@app.get("/scores/noise/surface")
def get_noise_surface(
    request: Request,
    lat: float = Query(...), lng: float = Query(...),
    radius: int = Query(1500), cells: int = Query(7),
    require_path: str | None = Query(
        None, description="Model path this deployment is supposed to run "
                          "('transfer' in production). The response reports "
                          "whether the grid actually used it."),
):
    """Modelled Lden on a grid around a point, for the licensed property API's
    per-property noise surface.

    Built in-process rather than by the caller fanning out over /scores/noise:
    one grid is up to 81 model runs, which trips that endpoint's per-IP rate
    limit, and keeping it here means a node cannot be computed under a model
    configuration production does not use (see noise/surface.py).

    Rate limited well below the point endpoint because one call is a grid, not
    a point. The bucket is keyed per caller where the caller identifies itself,
    so one API customer's grids cannot exhaust another's: this service listens
    on loopback only, and the header is set by our own web process.
    """
    from property_scores.noise.surface import noise_surface
    bucket = (request.headers.get("x-surface-caller")
              or request.headers.get("x-real-ip")
              or request.client.host)
    if not _check_rate(f"surface:{bucket}", limit=20):
        return JSONResponse({"error": "Rate limit exceeded."}, status_code=429)
    try:
        grid = noise_surface(lat, lng, radius_m=radius, cells=cells,
                             require_path=require_path)
        if not grid:
            return JSONResponse({"error": "no noise coverage"}, status_code=404)
        return grid
    except Exception as e:
        logger.exception("noise surface failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/scores/noise/terrain")
def get_noise_terrain(
    src_lat: float = Query(...), src_lng: float = Query(...),
    lat: float = Query(...), lng: float = Query(...),
):
    """DEM elevation profile from a source to the receiver. Local 30 m DEM only
    (GA DEM-H for AU tiles; the Open-Meteo out-of-coverage fallback was removed 2026-08-02:
    free tier is non-commercial ToS — see noise/terrain.py); split out from
    /noise/debug so the main response stays fast."""
    try:
        profile = elevation_profile(src_lat, src_lng, lat, lng)
        if not profile:
            return JSONResponse({"error": "elevation API unavailable"}, status_code=502)
        return profile
    except Exception as e:
        logger.exception("noise terrain failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/scores/elevation/contours")
def get_elevation_contours(
    # Bounded so that nan/inf (which FastAPI otherwise accepts as floats)
    # 422 at the door instead of blowing up window arithmetic deeper down,
    # where the ValueError is indistinguishable from a raster fault and was
    # reported as 503 "dem unreadable": a client typo must not page anyone.
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    radius: int = Query(1500),
    interval_m: float | None = Query(
        None, gt=0, le=200,
        description="Contour spacing in metres. Values below 1 m are raised "
                    "to 1 m: the source surveys were captured to a "
                    "specification requiring vertical accuracy of at least "
                    "0.30 m (95% confidence), which bounds the finest "
                    "defensible interval but is not a per-location accuracy "
                    "guarantee for the resampled 5 m grid; omitted means auto "
                    "(5 m default, widened over steep windows)."),
):
    """Contour LineStrings from the baked GA 5 m LiDAR DEM, for the licensed
    property API's contour surface (single registered source; see
    common/elevation_contours.py).

    404 means the window is outside the ~245,000 km2 LiDAR footprint: that is
    a real coverage boundary, and no substitute DEM is served in its place.
    503 means the baked VRT itself is missing on this node (an outage).
    """
    from property_scores.common.elevation_contours import (
        SourceReadError, contours, lidar_available)
    if not lidar_available():
        logger.error("elevation contours: au_lidar_5m.vrt missing on this node")
        return JSONResponse({"error": "lidar dem unavailable"}, status_code=503)
    try:
        out = contours(lat, lng, radius_m=max(200, min(radius, 2000)),
                       interval_m=interval_m)
        if out is None:
            return JSONResponse({"error": "no lidar coverage"}, status_code=404)
        return out
    except SourceReadError as e:
        # Fault, not fact: the raster exists but would not open or read
        # (corrupt VRT, truncated COG, I/O error). 404 would be cached
        # downstream as "this address has no coverage" for seven days; 503
        # is transient and retried.
        logger.error("elevation contours: source read fault: %s", e)
        return JSONResponse({"error": "lidar dem unreadable"}, status_code=503)
    except Exception as e:
        logger.exception("elevation contours failed")
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
