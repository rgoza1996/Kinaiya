"""Bounded setup declarations; no discovery, model calls or configuration writes."""

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass

from packages.contracts.converse_setup import (
    ConverseSetup,
    SetupInputs,
    SetupOperationState,
    SetupReasoning,
    SetupRole,
    SetupRoles,
)

_RESTART = "Configured on host. Changing this model requires a service restart."
_HOST_RESTART = (
    "Configured on host; loaded state is not checked. "
    "Changing this model requires a service restart."
)
_UNDECLARED = "No model is declared by this host configuration."


def _name(value):
    if (
        type(value) is not str
        or not 1 <= len(value.encode("utf-8")) <= 128
        or value != value.strip()
        or any(unicodedata.category(char).startswith("C") for char in value)
        or value.startswith(("/", "\\", "."))
        or re.match(r"^[A-Za-z]:[\\/]", value)
        or "://" in value
        or ".." in value.split("/")
    ):
        raise ValueError("Setup requires a bounded display identifier")
    return value


def _role(provider=None, model=None, *, label=None, source="host_configuration"):
    if provider is None or model is None:
        return SetupRole(
            selected_id=None,
            label=None,
            configured=False,
            source=source,
            options=[],
            reason=_UNDECLARED,
        )
    selected_id = f"{_name(provider)}:{_name(model)}"
    label = _name(model if label is None else label)
    return SetupRole(
        selected_id=selected_id,
        label=label,
        configured=True,
        source=source,
        options=[{"id": selected_id, "label": label}],
        reason=_RESTART if source == "runtime" else _HOST_RESTART,
    )


@dataclass(frozen=True)
class HostSetup:
    stt: SetupRole
    tts: SetupRole
    decisions: SetupRole
    reasoning: str


def host_setup(config):
    """Project explicit launcher fields, never retain the complete host config.

    Other service declarations are not health receipts. Missing unmanaged speech
    metadata stays unknown rather than inventing the launcher's voice defaults.
    """
    stt, tts, decisions = _role(), _role(), _role()
    voice, resources = config.get("voice"), config.get("resources")
    if isinstance(voice, dict) and voice:
        # SpeechClient applies this same default when voice is configured.
        tts = _role("tts", voice.get("profile", "Kokoro 82M"))
        choice = voice.get("stt")
        if isinstance(choice, dict) and choice.get("provider") == "whistle":
            if (
                isinstance(resources, dict)
                and resources.get("enabled") is True
                and isinstance(resources.get("whistle"), dict)
            ):
                stt = _role("whistle", "Whistle", label="Whistle")
        elif choice is None and isinstance(resources, dict) and resources.get("enabled") is True:
            stt_model = resources.get("stt_model", "tiny.en")
            stt = _role("whisper", stt_model, label=f"Whisper {stt_model}")
    providers = config.get("providers", {})
    if not isinstance(providers, dict):
        raise ValueError("Setup requires explicit provider configuration")
    selected = providers.get("decisions")
    if isinstance(selected, dict):
        decisions = _role(selected.get("kind"), selected.get("model"))
    conversation = providers.get("converse", {})
    if not isinstance(conversation, dict):
        raise ValueError("Setup requires explicit Converse configuration")
    kind = conversation.get("kind")
    reasoning = conversation.get("reasoning_effort")
    if "reasoning_effort" not in conversation:
        reasoning = "xhigh" if kind == "meta" else "auto" if kind == "compatible" else "none"
    elif reasoning is None:
        reasoning = "auto" if kind == "compatible" else "unavailable"
    return HostSetup(stt, tts, decisions, _name(reasoning))


def setup_state(runtime, host=None):
    """Read live operation counters and the configured provider declaration only."""
    info = runtime.provider.info
    roles = SetupRoles(
        conversation=_role(info.provider, info.model, source="runtime"),
        stt=host.stt if host else _role(),
        tts=host.tts if host else _role(),
        decisions=host.decisions if host else _role(),
    )
    inputs = SetupInputs(supported=["text"], selected=["text"])
    reasoning = SetupReasoning(
        selected=host.reasoning if host else "unavailable",
        options=[host.reasoning if host else "unavailable"],
        reason=(
            "Configured on host. Changing reasoning requires a service restart."
            if host
            else "Reasoning configuration is not declared by this embedded runtime."
        ),
    )
    # Bind the exact public declaration, never credentials, paths, endpoint URLs,
    # journal/session data, owner intent, or transient operation counts.
    canonical = json.dumps(
        {
            "roles": roles.model_dump(),
            "inputs": inputs.model_dump(),
            "reasoning": reasoning.model_dump(),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return ConverseSetup(
        revision=hashlib.sha256(canonical).hexdigest(),
        roles=roles,
        inputs=inputs,
        reasoning=reasoning,
        operation_state=SetupOperationState(
            text=runtime.text_status(), speech=runtime.speech_status()
        ),
    )
