"""Which score model family this process serves.

DA Leads pins each paid API key to a score model version
(`api_keys.delivery_policy.score_model_version`) and routes it to an
independently deployed scores service through `PROPERTY_SCORES_VERSION_BASES`
(da_leads docs/property-intelligence-v1-versioning.md). Foundit is pinned to
v1, and its licence gives 40 Business Days' notice before a material change in
methodology or source coverage. So a change that moves numbers must not reach
the v1 service, even though both services run the same repository.

This switch is how one codebase serves both: every behaviour that changes a
score relative to v1 is gated on `at_least("v2")`, and the process selects its
family from PROPERTY_SCORES_MODEL_VERSION. Unset means v1, so deploying this
code to the existing :8099 unit changes no number; the v2 unit sets the
variable explicitly.

Fail closed: an unknown value raises instead of falling back to v1 or to the
newest model. A typo in a systemd unit must stop the service at startup, not
silently serve a different model under the label the consumer asked for.

The selected version is part of the score stamp (api/stamp.components), so a
v2 payload can never be served from, or bless, a v1 cache row.
"""
from __future__ import annotations

import os

ENV_VAR = "PROPERTY_SCORES_MODEL_VERSION"
DEFAULT = "v1"
# Ordered oldest -> newest. Add a version only together with the gated code.
KNOWN = ("v1", "v2")


class UnknownModelVersion(ValueError):
    """PROPERTY_SCORES_MODEL_VERSION names a model this code does not have."""


def current() -> str:
    """The model family selected for this process (read per call, cheap)."""
    raw = os.environ.get(ENV_VAR, "").strip().lower() or DEFAULT
    if raw not in KNOWN:
        raise UnknownModelVersion(
            f"{ENV_VAR}={raw!r} is not one of {', '.join(KNOWN)}")
    return raw


def at_least(version: str) -> bool:
    """True when the selected model includes the changes introduced in
    `version`. `version` must be a known label; a misspelt gate in code is a
    bug and raises rather than reading as False."""
    if version not in KNOWN:
        raise UnknownModelVersion(f"unknown gate version {version!r}")
    return KNOWN.index(current()) >= KNOWN.index(version)
