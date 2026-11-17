"""Passive original native command/chunk evidence; never a second effect grant."""

from typing import Literal

from packages.contracts.live_effects import LiveEffectEvidence, LiveEffectReceipt, _Closed


class LiveEffectEvidenceState(_Closed):
    schema_version: Literal["2.0"]
    protocol: Literal["native_live_effect_v2"]
    original: LiveEffectReceipt
    evidence: LiveEffectEvidence | None
