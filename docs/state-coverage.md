# State-by-State Data Coverage

> ⚠️ 部分过时 (2026-07-19):坡度已改为全州统一本地 DEM(5m LiDAR→30m 兜底),per-state contour 端点已全删;矩阵「Terrain slope (contour) VIC/NSW/QLD/TAS only」低估其余州能力(现只有 DEM 瓦片未覆盖处才 not_assessed)。现状以 bushfire/score.py + 记忆 project_property_scores_roadmap 为准。

> ⚠️ **Reconciled with code 2026-07-02.** Three rows below were overstated and
> are corrected inline: "JRC satellite (38yr)" is actually local Overture water
> proximity (the remote JRC path is dead code); "MODIS fire history" runs VIC/NSW
> only (remote MODIS path is dead code); "COP DEM slope" has contour endpoints
> for VIC/NSW/QLD/TAS only (other states fall back and are flagged
> `slope_assessment=not_assessed`). Score APIs now emit `official_layer` /
> `slope_assessment` / aircraft `assessment` flags so a no-data state is not
> read as a confident zero.

Scores use different data sources depending on the state. This document explains what data is available where, so users understand why accuracy may vary across states.

## Coverage Matrix

| Data Source | VIC | NSW | QLD | SA | WA | TAS | NT | ACT |
|-------------|-----|-----|-----|----|----|-----|----|----|
| **Noise** | | | | | | | | |
| NFDH traffic counts | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ |
| VicRoads AADT | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| GTFS rail timetables | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |
| ANEF civilian airport | ✅ | ✅* | ✅* | ❌ | ✅ | ❌ | ❌ | ❌ |
| ANEF Defence airfields | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ❌ |
| **Flood** | | | | | | | | |
| Planning overlays | ✅ | ✅ | ❌ | ✅ | ❌ | ✅ | ❌ | ✅ |
| Water proximity (Overture, local) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| HAND elevation | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Bushfire** | | | | | | | | |
| Planning overlays | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ | ❌ | ❌ |
| WorldCover vegetation | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Terrain slope (contour) | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ❌ | ❌ |
| Fire history (local) | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Contamination** | | | | | | | | |
| Official contaminated-sites register | ✅ | ✅ | ❌ | ❌ | 🔒 | ❌ | ❌ | ✅ |
| EPA Environmental Audit location context | ✅ evidence-only | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Historical-use signal | ✅ | ❌ | ⚠️ licensed activity only | ⚠️ licensed activity only | ❌ | ⚠️ regulated/UPSS context | ❌ | ❌ |
| Statutory contaminated-groundwater restriction | ✅ | ⚠️ vulnerability only | ❌ | ✅ | 🔒 depth layer | ❌ | ❌ | ❌ |
| Known/operating landfill context | ✅ | ⚠️ national operating only | ⚠️ national operating only | ⚠️ national operating only | ⚠️ national operating only | ⚠️ national operating only | ⚠️ national operating only | ⚠️ national operating only |
| Industrial POI proxy | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Universal (all states)** | | | | | | | | |
| Overture roads/buildings/POIs | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Open-Meteo ERA5 climate | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| MODIS LST 1km | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Global Solar Atlas | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

*NSW ANEF: Western Sydney Airport only. QLD ANEF: Brisbane/Archerfield only.

## Accuracy Implications

### Best accuracy: VIC
Victoria has the richest data coverage: VicRoads AADT for noise, full planning overlays for flood/bushfire, an EPA priority-sites register plus evidence-only Environmental Audit locations for contamination, and GTFS rail. An audit location is not a contamination finding and the DataVic mirror may lag EPA's public register. Noise model validated at 83% against known locations.

### Good accuracy: NSW
Planning overlays for flood/bushfire, an official contamination register, GTFS rail and NFDH traffic counts. Missing VicRoads-equivalent granular AADT.

### Moderate accuracy: SA, TAS
Planning overlays for some scores, NFDH traffic counts, but no EPA register and limited ANEF data.

### Uneven coverage: QLD, WA, NT, ACT
ACT now has an official contaminated-sites register joined to the official
block cadastre. QLD, WA and NT do not have an authorised register integrated;
WA is specifically rights-blocked. Other score families have their own
coverage. Never transfer one family's state rating to another.

## How to Interpret

When a score includes data from official planning overlays or registers, it carries higher confidence. When contamination coverage is incomplete, optimistic numeric scores are withheld; `score_status` and each component status must be read before ranking.

The API response includes a `disclaimer` field for every score. Risk-related scores (flood, bushfire, contamination) explicitly state they are not professional assessments.
