"""Terrain screening attenuation for noise propagation.

Samples DEM elevation along the source-receiver path to detect hills/ridges
that block sound. Uses the same Maekawa barrier formula as building screening.
"""

import math
import requests

OPEN_METEO_ELEV = "https://api.open-meteo.com/v1/elevation"
SOURCE_HEIGHT = 0.5
RECEIVER_HEIGHT = 1.5
SOUND_WAVELENGTH = 0.34
MAX_TERRAIN_ATTEN_DB = 15.0
N_SAMPLES = 7
MIN_DISTANCE_M = 100
MIN_BARRIER_HEIGHT_M = 3.0  # terrain must rise 3m+ above sight line to count


def terrain_attenuation(source_lat: float, source_lng: float,
                        receiver_lat: float, receiver_lng: float,
                        distance_m: float) -> float:
    """Check if terrain blocks line-of-sight between source and receiver.

    Samples elevation at N_SAMPLES points along the path. If any intermediate
    point rises above the direct sight line, computes Maekawa barrier attenuation.

    Only triggered for distances > 200m (terrain screening is negligible at
    short range and the API call adds ~200ms latency).

    Returns attenuation in dB (positive = noise reduction, 0 = no screening).
    """
    if distance_m < MIN_DISTANCE_M:
        return 0.0

    lats = []
    lngs = []
    for i in range(N_SAMPLES):
        t = i / (N_SAMPLES - 1)
        lats.append(source_lat + t * (receiver_lat - source_lat))
        lngs.append(source_lng + t * (receiver_lng - source_lng))

    try:
        resp = requests.get(OPEN_METEO_ELEV, params={
            "latitude": ",".join(f"{x:.6f}" for x in lats),
            "longitude": ",".join(f"{x:.6f}" for x in lngs),
        }, timeout=5)
        if not resp.ok:
            return 0.0
        elevations = resp.json().get("elevation", [])
        if len(elevations) < N_SAMPLES:
            return 0.0
    except (requests.RequestException, ValueError, KeyError):
        return 0.0

    src_elev = elevations[0] + SOURCE_HEIGHT
    rcv_elev = elevations[-1] + RECEIVER_HEIGHT

    best_atten = 0.0

    for i in range(1, N_SAMPLES - 1):
        ground_elev = elevations[i]
        if ground_elev is None:
            continue

        frac = i / (N_SAMPLES - 1)
        sight_line_elev = src_elev + frac * (rcv_elev - src_elev)

        barrier_height = ground_elev - sight_line_elev
        if barrier_height < MIN_BARRIER_HEIGHT_M:
            continue

        dist_src = distance_m * frac
        dist_rcv = distance_m * (1 - frac)
        over_src = math.sqrt(dist_src ** 2 + barrier_height ** 2)
        over_rcv = math.sqrt(dist_rcv ** 2 + barrier_height ** 2)
        detour = over_src + over_rcv - distance_m

        if detour <= 0:
            continue

        fresnel_n = 2 * detour / SOUND_WAVELENGTH
        atten = min(10 * math.log10(3 + 20 * fresnel_n ** 2), MAX_TERRAIN_ATTEN_DB)

        if atten > best_atten:
            best_atten = atten

    return best_atten
