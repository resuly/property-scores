# Commercial Readiness Assessment (2026-04-24, contamination updated 2026-08-30)

> ⚠️ 补充 (2026-07-19):walkability 坡度惩罚已完成(commit 23cd101),本文 line16「no slope penalty」及 remaining #7 均已过时。仍准确的剩余项 = auth/rate-limit(#3)+ DA Leads /map 集成(#4)。

> ⚠️ **2026-04-24 snapshot, several items now stale — code is the source of truth**
> (reconciled 2026-07-02). Known inaccuracies: heat-island latency 18s → now a
> local MODIS median mosaic at **~1.2s** (17x); bushfire "MODIS coverage patchy"
> is actually `_fire_history` **dead code** (never called; the live path is
> `_fire_history_local`, VIC/NSW only); the "33s / 96s→33s" figures come from
> that dead path and are unreliable. Do not quote latency numbers from this file.

## Per-Score Assessment

| Score | Readiness | Key Improvements | Remaining Gap |
|-------|-----------|-----------------|---------------|
| Noise | **85%** | ANEF national, rail fix, terrain DEM, confidence ±dB, joint AADT, cache 0.87ms | QLD/NT AADT sparse; vegetation screening |
| Walkability | **70%** | Highway barrier penalty with direction check | Still straight-line base; no slope penalty |
| Flood | **60%** | Overlay+JRC+HAND+P95, cache pipeline, disclaimer | 10s latency; no depth estimation |
| View Quality | **55%** | 6 factors incl. 8-dir horizon angle analysis | No building-level occlusion; no floor level |
| Solar | 50% | Caveat added | Pure API passthrough; no roof analysis |
| Bushfire | **55%** | Overlay+Veg+Slope (FireHistory local, VIC/NSW only) | latency figure below unreliable; remote MODIS fire is dead code |
| Heat Island | **50%** | MODIS day+night LST (local median mosaic 2026-07), night heat retention | 1km coarse; ~1.2s latency (was 18s) |
| Contamination | **NOT READY for Self-Serve** | VIC/NSW register adapters; ACT register + official block join locally complete; VIC Environmental Audit/history/landfill/groundwater, SA GPA/licensed, QLD/TAS context; fail-closed status and monitoring | Known WA/QLD truth anchors still lack authorised register coverage; WA rights pending; QLD/SA/TAS/NT official register coverage absent; live production does not yet contain this candidate |

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

### Contamination commercial gate (2026-08-30)
- Production smoke returns HTTP 200 in all eight jurisdictions, but this proves
  availability, not official-register coverage.
- The machine-readable truth suite now has zero manual contamination rows. On
  the pre-fix production service it exposes known WA remediation sites, QLD
  Newstead Gasworks and VIC Fitzroy Gasworks as failures rather than burying
  them in manual notes.
- Local code adds the CC BY 4.0 ACT register joined to ACTGOV Block, removes
  Clean/Very Clean labels, and withholds optimistic 70-100 scores when required
  coverage is incomplete. These changes are not production evidence until
  independently reviewed and deployed.
- The candidate also queries the CC BY 4.0 EPA Victoria Environmental Audit
  point/polygon WFS at runtime. Fitzroy Gasworks now returns explicit audit
  evidence and a `Mapped Context - Review` headline, while the audit remains
  evidence-only and contributes no numeric risk score. The mirror's freshness
  and non-transaction-safe paging limitations are disclosed and fail closed on
  observed count/order/overlap/schema drift.
- Self-Serve checkout remains blocked until the remaining truth failures have
  an authorised source or the public product coverage is deliberately narrowed.

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
