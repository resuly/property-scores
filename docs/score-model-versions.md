# Score model versions (v1 / v2)

DA Leads pins each paid API key to a score model (`api_keys.delivery_policy.
score_model_version`) and routes it through `PROPERTY_SCORES_VERSION_BASES`
to a separate scores process (da_leads `docs/property-intelligence-v1-versioning.md`).
Foundit's key is pinned to `v1`. This repo serves both models from one
codebase. `PROPERTY_SCORES_MODEL_VERSION` selects the model for each process
(`property_scores/common/model_version.py`):

- unset or `v1`: the model Foundit is pinned to. Every v2 change is gated off.
- `v2`: the changes listed below.
- anything else: the service refuses to start. It never falls back to another model.

The selected version is a score-stamp component (`/version` ->
`components.score_model`), so v1 and v2 cache rows can never be mixed up downstream.

## v2 changes (flood only, 2026-11-05)

1. **CC BY council layers count toward the score** (`local_overlays._V2_COUNCIL_LAYERS`).
   Each layer must be on the da_leads hazard geometry allowlist, and its
   meaning must be checked against the publisher's record. Matching uses the
   exact baked category, so a renamed category drops out of the score.
   - Ku-ring-gai mainstream FPA (1% AEP + 0.5 m): `flood`. Overland FPA: `moderate`.
   - Hawkesbury FRMSP 2025 Flood Planning Area: `flood`.
   - Campbelltown 1% AEP design extent: `flood`.
   - Sunshine Coast flooding and inundation area (no hazard level published): `moderate`.
   - Display-only, with reasons in the code comment: SES "Northern Lakes"
     (it actually holds Hornsby data licensed `3rd`), Gold Coast and Logan
     (assessment triggers), Redland (CC BY-SA), Georges River (invalid
     geometry gives about 1 in 3 false hits), QLD SPP floodplain, and QFMP.
2. **VIC SBO counts as `moderate` again.** The library has no SBO source, so
   v2 asks VicPlan layer 16 directly: timeout 3 s to connect and 3.5 s to
   read, and a per-process breaker that skips the call for 5 minutes after 3
   failures in a row. When the call fails or is skipped, the result carries
   `warnings` (for example "Could not reach VicPlan SBO (layer 16)") and a miss
   is no longer reported as a clean statewide check (`trust` becomes
   `hit_only`). da_leads `_score_component_bad` refuses to cache a flood
   component with `warnings`, so a degraded answer is never pinned for 31
   days. If the library gains `vic_hazard_flood_sbo`, the library answers and
   VicPlan is not asked.
   **Stacking.** SBO never lowers the worst severity: LSIO stays `flood` and
   FO stays `floodway`. Each extra mapped zone takes 3 points off the band's
   top (at most 3 extra zones), the same as the remote path did before
   2026-07-03. So LSIO alone gives 40 Moderate, and LSIO plus SBO gives 37
   High. VicPlan is still asked when LSIO or FO already hit: skipping it would
   change that 37 into 40 and cross a label boundary.
3. **Unverified terrain is never Low** (`score._v2_unverified_terrain_cap`).
   This applies when no official overlay vouches for the point and there is
   no study depth. If HAND is uncertain, missing, proxy-based or has no
   source, or is below 2 m, the score is capped at 65 and the payload gets
   `terrain_check: "unverified"`.
   **Meaning: the model cannot measure the terrain here. It is not a finding
   of moderate risk.** The 65 says "not shown to be low". A Moderate score
   without `terrain_check` comes from actual flood evidence. The customer
   API docs (da_leads "Flood score vs flood layers") say the same.
4. **An official overlay hit caps the score** at the top of its severity
   band, so the HAND > 20 m boost cannot lift a mapped hit into Low.

Tests: `tests/test_flood_model_v2.py`. It asserts that v1 equals the recorded
production score at every fixture point, and that v2 moves those points.

## Deploying

1. Deploy the code to the existing `:8099` unit as usual, with no new env.
   It stays v1. Offline replay reproduced the production score at 716/716
   validation points and 51/51 council sample points. The stamp moves once,
   because the code hash and the new `score_model` component both change, so
   downstream v1 caches recompute once with identical values.
2. Create the v2 unit, which needs Bo's approval (new service and resources):
   `property-scores-v2.service`, a copy of the v1 unit with `API_PORT=8100`,
   `Environment=PROPERTY_SCORES_MODEL_VERSION=v2`, 1 worker,
   `MemoryHigh=1.5G` / `MemoryMax=2G`, and the same read-only data. Check
   `curl 127.0.0.1:8100/version` shows `components.score_model == "v2"` and
   `:8099` shows `"v1"`.
3. da_leads env: set `PROPERTY_SCORES_VERSION_BASES={"v1":"http://127.0.0.1:8099","v2":"http://127.0.0.1:8100"}`.
   Keep `PROPERTY_SCORES_DEFAULT_VERSION=v1` until Bo decides which keys and
   surfaces move. The website proxy reads `PROPERTY_SCORES_API_BASE`, which
   is :8099 today. Changing it is a separate decision. Run
   `pi_v1_contract.py check` and the Foundit 12-address acceptance afterwards.
4. Smoke test (both ports, `nocache=true`): 42 Romeo Crescent Rosemeadow
   (-34.1018, 150.7934) gives 95 on v1 and 40 on v2. 15 Flinders Street
   Mentone gives 80 on v1 and 60 on v2.
5. Rollback: stop the v2 unit and remove `v2` from the mapping. v1 is untouched.

Note: two units running from one checkout means a later deploy updates both.
v1 stays stable because every change is gated on the version, plus the v1
equality tests. Any new change that moves numbers must be gated the same way.
