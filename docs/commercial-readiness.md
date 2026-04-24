# Commercial Readiness Assessment (2026-04-24, updated)

## Per-Score Assessment

| Score | Readiness | Key Improvements | Remaining Gap |
|-------|-----------|-----------------|---------------|
| Noise | **85%** | ANEF national, rail fix, terrain DEM, confidence ±dB, joint AADT, cache 0.87ms | QLD/NT AADT sparse; vegetation screening |
| Walkability | **70%** | Highway barrier penalty with direction check | Still straight-line base; no slope penalty |
| Flood | **60%** | Overlay+JRC+HAND+P95, cache pipeline, disclaimer | 10s latency; no depth estimation |
| View Quality | **55%** | 6 factors incl. 8-dir horizon angle analysis | No building-level occlusion; no floor level |
| Solar | 50% | Caveat added | Pure API passthrough; no roof analysis |
| Bushfire | **55%** | Overlay+Veg+Slope+FireHistory, 96s→33s | 33s still slow; MODIS coverage patchy |
| Heat Island | **50%** | MODIS day+night LST, night heat retention penalty | 1km coarse; 18s latency |
| Contamination | **50%** | EPA 3-state + POI deduped, disclaimer | POI proxy ≠ actual contamination |

## All Disclaimers Present: 8/8 ✅

Every score now returns a `disclaimer` or `caveat` field in the API response.

## Methodology Improvements Completed

| Item | Score | Effect |
|------|-------|--------|
| Rail excess attenuation 0.04 dB/m | Noise | Quiet residential 56→52 dB ✅ |
| Terrain screening (DEM Maekawa) | Noise | Hill detection up to 15 dB |
| Confidence interval ±dB | Noise | Returns range, wider for no-AADT areas |
| Joint class×speed AADT table | Noise | trunk×60→15K (was 8K) |
| Highway barrier penalty | Walkability | Southbank 92→76 across CityLink ✅ |
| Barrier direction filter | Walkability | 11→6 false barriers at Southbank |
| 8-direction horizon analysis | View Quality | Detects terrain openness per direction |
| Night LST + heat retention | Heat Island | MODIS LST_Night for nighttime cooling |
| Fire history timeout 15s | Bushfire | 96s→33s latency |
| POI false positive cleanup | Contamination | 28→2 industrial matches at CBD |

## Validation Summary

### Noise (83% pass rate, 12 locations)
- Quiet residential: 4/4 ✅ (48-52 dB)
- Near rail: 2/2 ✅
- Noisy arterials: 4/6 ✅
- Confidence interval: ±4dB (loud) to ±11dB (quiet, no AADT)

### Other Scores
- Validated via sanity checks at Melbourne CBD: 8/8 scores within expected ranges ✅
- No systematic ground-truth validation yet for non-noise scores

## Remaining Items (prioritized)

### Must-do for commercial launch
1. Bushfire latency further optimization (33s → target <15s)
2. Cross-state coverage consistency documentation
3. API authentication + rate limiting
4. Integration with DA Leads /map page

### Should-do
5. Noise: 100+ ground-truth validation
6. Flood: historical flood event validation (Lismore, Maribyrnong)
7. Walkability: slope penalty using existing DEM
8. ERA5 P95 grid completion (running in background)

### Nice-to-have
9. View Quality: building-level occlusion (needs 3D models)
10. Solar: roof analysis (needs LiDAR)
11. Data update pipeline (scheduled re-computation)
