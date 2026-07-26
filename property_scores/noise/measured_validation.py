"""How this model performs against real noise loggers, per state.

Everything else that shapes the confidence interval is derived from the
SoundPLAN corpus, which is itself a model. That is why the interval could look
confident exactly where the model is worst: Victoria has the largest SoundPLAN
sample and so was never flagged, while against instruments it reads 9.4 dB
high. The interval said +-4 dB and the measured error was more than twice that.

Source: scripts/eis_measured_gate.py over data/eis_noise/measured_corpus_v2.csv,
199 noise-logger readings published in road-project environmental impact
statements and reviews of environmental factors. Regenerate after any model or
calibration change and update this table; the gate prints exactly these rows.

Read the caveats before treating a number as the model's true error:

  * Addresses geocode to a building centroid, losing the logger's facade
    position. A logger on the road-facing wall reads LOUDER than the centroid,
    which pushes bias DOWN. The over-reads below survive that pressure, so they
    are, if anything, understated.
  * LA10(18h) rows are converted to LAeq with a fixed -3 dB assumption, and
    they are overwhelmingly Victorian, so metric and state were confounded.
    That confound is now largely resolved: before the Victorian fix the two
    metrics sat 5.9 dB apart (LA10 +8.6, LAeq +2.7) and afterwards 1.5 dB
    (LA10 +3.5, LAeq +2.0). The gap was the Victorian residential over-read,
    not the conversion.
  * South Australia has no rows at all: the model is unvalidated against
    instruments there, which is not the same as validated and accurate.
"""

AS_AT = "2026-07-26"
CORPUS = ("noise-logger readings published in road-project environmental impact "
          "statements (199 points nationally)")

# state -> (instrument_points, bias_db, mae_db). Bias is model minus measured
# Lden, so positive means the model reads high. TAS, ACT and NT are pooled in
# the gate because none has enough points alone.
# Regenerated 2026-07-26 after the quiet-end relief was extended to VIC and WA
# (transfer.QUIET_RECAL_STATES). VIC went +9.4 -> +3.8 and MAE 9.97 -> 6.68,
# WA +6.6 -> +4.9 and 8.08 -> 7.46; NSW, QLD and TAS/ACT/NT are byte-identical.
_GROUPS: dict[str, tuple[int, float, float]] = {
    "NSW": (55, -0.9, 5.58),
    "QLD": (12, 2.8, 4.30),
    "VIC": (79, 3.8, 6.68),
    "WA": (38, 4.9, 7.46),
    "TAS": (15, 4.9, 5.63),
    "ACT": (15, 4.9, 5.63),
    "NT": (15, 4.9, 5.63),
}

# 3 dB doubles acoustic energy and is the point at which a systematic offset
# stops being noise around the estimate and starts being a direction the reader
# should know about. Below it the interval already covers the error.
MATERIAL_BIAS_DB = 3.0


def for_state(state: str | None) -> dict | None:
    """Measured performance for a state, or None if we have no instruments there.

    None is a disclosure too: it means unvalidated, not validated and fine.
    """
    if not state:
        return None
    row = _GROUPS.get(state.upper())
    if row is None:
        return None
    n, bias, mae = row
    out = {
        "instrument_points": n,
        "bias_db": bias,
        "mae_db": round(mae, 1),
        "corpus": CORPUS,
        "as_at": AS_AT,
    }
    if bias >= MATERIAL_BIAS_DB:
        out["note"] = (
            f"Against {n} noise-logger readings in {state.upper()} this model "
            f"reads {bias:+.1f} dB on average, so the estimate is best treated "
            f"as an upper bound in this state.")
    elif bias <= -MATERIAL_BIAS_DB:
        out["note"] = (
            f"Against {n} noise-logger readings in {state.upper()} this model "
            f"reads {bias:+.1f} dB on average, so the estimate is likely "
            f"conservative in this state.")
    else:
        out["note"] = (
            f"Against {n} noise-logger readings in {state.upper()} this model "
            f"reads {bias:+.1f} dB on average, within the stated interval.")
    return out


def unvalidated_note(state: str | None) -> str:
    return ("No published noise-logger readings were available to validate this "
            f"model in {state or 'this area'}. The estimate rests on the same "
            "modelling as elsewhere, but it has not been checked against "
            "instruments here.")
