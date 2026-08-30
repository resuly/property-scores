# Commercial Readiness Assessment (2026-04-24, contamination updated 2026-08-30)

> 2026-08-30 更新：Walkability Screening v1 已使用区域DEM坡度调整并明确披露；距离基础仍是直线米，
> 不包含路网时间或isochrone。Contamination的ACT/VIC fail-closed来源与现有monitoring已进入生产代码，
> 但Self-Serve仍受WA/QLD truth与rights等硬门约束。

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
| Walkability | **Pilot** | Highway/water barrier checks, named-trail dedup, regional DEM slope adjustment | Still straight-line base; no route time or isochrone |
| Flood | **60%** | Overlay+JRC+HAND+P95, cache pipeline, disclaimer | 10s latency; no depth estimation |
| View Quality | **55%** | 6 factors incl. 8-dir horizon angle analysis | No building-level occlusion; no floor level |
| Solar | 50% | Caveat added | Pure API passthrough; no roof analysis |
| Bushfire | **55%** | Overlay+Veg+Slope (FireHistory local, VIC/NSW only) | latency figure below unreliable; remote MODIS fire is dead code |
| Heat Island | **50%** | MODIS day+night LST (local median mosaic 2026-07), night heat retention | 1km coarse; ~1.2s latency (was 18s) |
| Contamination | **NOT READY for Self-Serve** | Production code includes VIC/NSW adapters, ACT register + official block join, VIC Environmental Audit evidence and fail-closed monitoring | Known WA/QLD truth anchors still lack authorised register coverage; WA rights pending; QLD/SA/TAS/NT official register coverage remains incomplete |

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
- Production code adds the CC BY 4.0 ACT register joined to ACTGOV Block, removes
  Clean/Very Clean labels, and withholds optimistic 70-100 scores when required
  coverage is incomplete. Self-Serve remains blocked despite this fail-closed delivery.
- The production implementation also queries the CC BY 4.0 EPA Victoria Environmental Audit
  point/polygon WFS at runtime. Fitzroy Gasworks now returns explicit audit
  evidence and a `Mapped Context - Review` headline, while the audit remains
  evidence-only and contributes no numeric risk score. The mirror's freshness
  and non-transaction-safe paging limitations are disclosed; runtime lookup is
  one bounded page and fails closed on count mismatch, saturation, bad order,
  duplicates or schema drift.
- Monitoring is implemented inside the existing production
  `scripts/score_truth_probes.py` sentinel, not as a parallel monitor. It checks
  both WFS layers' HTTP/error shape, 13-field schema, publisher counts and
  maximum `data_extracted_on`; the 72-hour internal age limit and 24-hour
  point/polygon skew; Fitzroy reference `0008005706`; and a checked-empty
  Carlton 25m control. The existing managed truth sentinel remains the one
  scheduled monitoring surface; do not install a second Contamination cron.
- Self-Serve checkout remains blocked until the remaining truth failures have
  an authorised source or the public product coverage is deliberately narrowed.

### Monitoring verification commands (do not execute without deployment approval)

Read-only live dry-run after the candidate exists on the target host:

```bash
cd /var/www/property-scores
NOISE_TRANSFER=1 NOISE_QUIET_RECAL=1 \
  .venv/bin/python scripts/score_truth_probes.py \
  --domain contamination --source-only --no-alert
```

The existing managed DA Leads cron already runs the full truth sentinel daily
at 20:00 UTC, so no second scheduler entry is required. To reconcile/install
that existing managed block after deployment, from the Limon Ops checkout:

```bash
bash bin/install_daleads_cron.sh
# Inspect the printed diff and confirm. Use --apply only with deployment approval.
```

## Remaining Items (prioritized)

### Must-do for commercial launch
1. Bushfire latency further optimization (33s → target <15s)
2. Cross-state coverage consistency documentation
3. API authentication + rate limiting
4. Integration with DA Leads /map page

### Should-do
5. Noise: 100+ ground-truth validation
6. Flood: historical flood event validation (Lismore, Maribyrnong)
7. Walkability: route-network time / isochrone capability spike while preserving straight-line fallback
8. ERA5 P95 grid completion (running in background)

### Nice-to-have
9. View Quality: building-level occlusion (needs 3D models)
10. Solar: roof analysis (needs LiDAR)
11. Data update pipeline (scheduled re-computation)
