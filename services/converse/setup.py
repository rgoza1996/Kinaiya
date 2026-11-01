"""Bounded setup declarations; no discovery, model calls or configuration writes."""

import hashlib
import json
import re
import sqlite3
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
from services.converse.embedding import runtime_model_settings, runtime_provider_info

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


@dataclass(frozen=True)
class HostSetupUnavailable:
    """Failed optional projection, with no retained config or exception detail."""


HOST_SETUP_UNAVAILABLE = HostSetupUnavailable()


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
        if voice.get("tts") is not None:
            from packages.providers.openrouter_speech import MODEL, openrouter_tts_settings

            openrouter_tts_settings(voice["tts"])
            tts = _role("openrouter", MODEL, label="Kokoro 82M (OpenRouter cloud)")
        choice = voice.get("stt")
        if isinstance(choice, dict) and choice.get("provider") == "whistle":
            if (
                isinstance(resources, dict)
                and resources.get("enabled") is True
                and isinstance(resources.get("whistle"), dict)
            ):
                stt = _role("whistle", "Whistle", label="Whistle")
        elif isinstance(choice, dict) and choice.get("provider") == "meta":
            from packages.providers.meta_speech import MODEL, meta_stt_settings

            meta_stt_settings(choice)
            stt = _role("meta", MODEL, label="Muse Voice Transcribe 1.0 (cloud)")
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


def safe_host_setup(config):
    """Display validation cannot take ownership of service startup.

    Some providers accept opaque aliases that the narrower display contract must
    refuse. Keep that optional failure distinct from absent embedded metadata;
    provider construction, runtime failures and unrelated exceptions still fail
    through their existing owners.
    """
    try:
        return host_setup(config)
    except ValueError:
        return HOST_SETUP_UNAVAILABLE


def setup_state(runtime, host=None):
    """Read live operation counters and the configured provider declaration only."""
    if isinstance(host, HostSetupUnavailable):
        raise ValueError("Host setup projection is unavailable")
    from packages.providers.text import ProviderError

    settings = runtime_model_settings(runtime)
    try:
        info = runtime_provider_info(runtime)
    except (ProviderError, ValueError, RuntimeError):
        conversation = _role(source="runtime")
    else:
        # An unavailable owner is different from an invalid display identity.
        # Refuse unsafe metadata through the API's fixed no-store error boundary.
        conversation = _role(info.provider, info.model, source="runtime")
    if settings is not None:
        conversation.reason = (
            "Selected through Converse model options; readiness is not checked."
            if conversation.configured
            else "Selected Converse model is unavailable. Inspect model options."
        )
    stt = host.stt if host else _role()
    if getattr(runtime, "_stt_options_configured", False):
        owner = getattr(runtime, "stt_model_settings", None)
        try:
            if owner is None or runtime._stt_options_unavailable:
                raise ValueError("STT selection unavailable")
            stt_info = owner.selected_info()
            stt = (
                _role(source="runtime")
                if stt_info is None
                else _role(stt_info.provider, stt_info.model, source="runtime")
            )
            stt.reason = "Selected through Audio options; identity or readiness may be unknown."
        except (ValueError, RuntimeError, OSError):
            stt = _role(source="runtime")
            stt.reason = "Selected STT is unavailable. Inspect Audio options."
    tts = host.tts if host else _role()
    if getattr(runtime, "_tts_options_configured", False):
        try:
            owner = runtime.tts_model_settings
            if owner is None or runtime._tts_options_unavailable:
                raise ValueError("TTS selection unavailable")
            tts_info = owner.selected_info()
            tts = (
                _role(source="runtime")
                if tts_info is None
                else _role(tts_info.provider, tts_info.model, source="runtime")
            )
            tts.reason = "Selected through Audio options; identity or readiness may be unknown."
        except (ValueError, RuntimeError, OSError, sqlite3.Error):
            tts = _role(source="runtime")
            tts.reason = "Selected TTS is unavailable. Inspect Audio options."
    roles = SetupRoles(
        conversation=conversation,
        stt=stt,
        tts=tts,
        decisions=host.decisions if host else _role(),
    )
    inputs = SetupInputs(supported=["text"], selected=["text"])
    try:
        effort = (
            settings.selected_reasoning()
            if settings is not None
            else host.reasoning
            if host
            else "unavailable"
        ) or "unavailable"
    except (ProviderError, ValueError, RuntimeError):
        effort = "unavailable"
    reasoning = SetupReasoning(
        selected=effort,
        options=[effort],
        reason=(
            "Selected through Converse model options."
            if settings is not None
            else "Configured on host. Changing reasoning requires a service restart."
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
