# Archived noise experiments

Superseded work, kept because each one records a *negative result* worth not
repeating. Nothing here is imported by production. The live model and its
provenance are in the registry (`scripts/noise_model.py list`).

## Why each was abandoned

| script | what it tried | why it is not the shipped model |
|---|---|---|
| `poc_eu_transfer.py` … `poc_eu_transfer4.py` | successive EU→AU transfer feature sets | superseded by `poc_eu_transfer5.py`, which added buildings + DEM + land cover + POI on top of roads and is the feature set the shipped model uses |
| `poc_aadt_impute.py`, `poc_aadt_residual.py` | add measured traffic volume (AADT) as a feature | **judged negative**: road class already carries the volume signal, so AADT added nothing. Do not re-add without new evidence |
| `poc_mapbox.py`, `poc_mapbox_v2.py` | add Mapbox speed / congestion | **judged negative**, same reason, and it added a paid dependency |
| `poc_alphaearth.py` | AlphaEarth embeddings as features | not adopted; cache kept at `data/alphaearth_emb_cache.npz` |
| `poc_us_train.py` | train on US noise data instead of EU | not adopted; EU (NL+UK) has denser official Lden coverage |
| `poc_soundplan_distill.py` | distil SoundPLAN directly into an AU-native model | became `noise/distill.py` + `noise_rf_soundplan.pkl`, which **nothing imports** — a dead path |
| `train_noise_model.py` | the original pre-transfer model | superseded by the EU transfer approach |

## Still live in `scripts/` (do not archive)

- `poc_eu_transfer5.py` — defines `fkeys()`, the 75-feature contract the shipped
  model depends on. Archiving it would break `build_noise_model.py` and the
  calibration scripts.
- `poc_eu_transfer6_geodist.py` — the 2026-07-26 corrected-geometry rebuild.
  Its result was negative (MAE 3.798 → 3.852) but it is the reproduction script
  for that finding and regenerates all features in 96 s.
- `optimize_noise_transfer_n.py`, `retrain_noise_transfer_n300.py`,
  `recalc_au_full_calibration.py`, `unified_calib_analysis.py` — the chain that
  actually built the live model.
- `calib_eval.py` — the A/B gate.
- `verify_noise_frozen.py` — proves a change left noise bit-identical.
- `build_physics_feature_cache.py` — physics road-only Lden per calibration
  point, for the physics-in-the-blend work.

## Orphan data artefacts

These sit in `data/` (gitignored) and are **not** loaded by production:

- `noise_rf_soundplan.pkl` (30 MB) — used only by `noise/distill.py`, which has
  no importers.
- `noise_ml_model_la50.pkl` — the XGBoost residual correction, gated off by
  `NOISE_ML_CORRECTION=0` (unset in the systemd unit). Its own docstring says it
  was trained against the pre-fix physics and inverts against the corrected one.
- `noise_state_calibration.json.{v0bak,pre_opt_bak,pre_unified_bak}` — the
  pre-registry way of doing rollback. Superseded by version directories.
- `data/eu/transfer{,2,3,4}_cache.npz` — feature caches for the archived POCs.

Leave them until the registry has been live on the server for a while, then
delete with `scripts/noise_model.py` in front of you so nothing in use is
removed. See `feedback_list_before_delete`.
