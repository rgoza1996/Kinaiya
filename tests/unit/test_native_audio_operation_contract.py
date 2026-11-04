"""Ten-key operation metadata contract; baseline fixtures are protocol-only.

Authored from the frozen contract and3bdf baseline, before parity source reads.
Actual native-owner pending is exercised with a held synthetic ASR operation;
malformed observation rows below test consumers, not physical cleanup authority.
"""

import asyncio

import pytest
from test_native_audio_converse import NoLegacyText, body, environment

from services.converse.model_settings import ConverseModelConflict, ConverseModelSettings
from services.converse.native_audio import NativeClipRequest
from services.converse.runtime import ConverseRuntime
from services.converse.stt_model_settings import STTModelConflict, STTModelSettings
from services.converse.tts_model_settings import TTSModelConflict, TTSModelSettings

BASE_KEYS = {
    "preparing",
    "text",
    "text_admitting",
    "windows",
    "speech",
    "stt",
    "stt_cleanup",
    "native_audio",
    "playback_pending",
    "memory_pending",
}
MAX_COUNT = 9007199254740990
ROLES = ("converse", "stt", "tts")


def _settings(role, runtime):
    return {
        "converse": ConverseModelSettings,
        "stt": STTModelSettings,
        "tts": TTSModelSettings,
    }[role](runtime, None)


def _change(role, snapshot):
    request = {
        "schema_version": "1.0",
        "expected_revision": snapshot["revision"],
        "expected_state": snapshot["state_token"],
        "idempotency_key": "native-operation-contract-" + role,
        "selection": snapshot["selection"],
    }
    if role != "converse":
        request.update(
            expected_backend=snapshot["backend_id"],
            expected_instance=snapshot["instance_id"],
        )
    return request


@pytest.mark.asyncio
async def test_standalone_runtime_has_exact_ten_zero_operation_fields(tmp_path):
    async with environment(tmp_path) as env:
        standalone = ConverseRuntime(env.runtime.journal, NoLegacyText())
        try:
            observed = standalone.model_operations()
            assert set(observed) == BASE_KEYS
            assert all(type(count) is int and count == 0 for count in observed.values())
        finally:
            await standalone.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ROLES)
async def test_wired_native_original_pending_is_retained_by_every_role_snapshot(tmp_path, role):
    async with environment(tmp_path) as env:
        settings = _settings(role, env.runtime)
        pending = None
        try:
            idle = env.runtime.model_operations()
            assert set(idle) == BASE_KEYS
            assert all(type(count) is int and count == 0 for count in idle.values())
            before = settings.snapshot()
            extras = {"leases", "cleanup_pending", "cleanup_failed"}
            if role != "converse":
                extras.add("cleanup_unknown")
            assert set(before["operation_state"]) == BASE_KEYS | extras
            assert before["operation_state"]["native_audio"] == 0
            assert before["busy"] is False
            env.native.asr_release.clear()
            pending = asyncio.create_task(
                env.owner.admit(NativeClipRequest.model_validate(body(env)), env.controller)
            )
            await asyncio.wait_for(env.native.asr_entered.wait(), 5)
            assert env.owner.status()["pending"] is True
            observed = env.runtime.model_operations()
            assert set(observed) == BASE_KEYS
            assert type(observed["native_audio"]) is int
            assert observed["native_audio"] == 1
            active = settings.snapshot()
            assert set(active["operation_state"]) == BASE_KEYS | extras
            assert active["operation_state"]["native_audio"] == 1
            assert active["busy"] is True
            conflict = {
                "converse": ConverseModelConflict,
                "stt": STTModelConflict,
                "tts": TTSModelConflict,
            }[role]
            with pytest.raises(conflict):
                settings.change(_change(role, before))
            assert settings.snapshot()["revision"] == before["revision"]
            assert len(env.native.transcriptions) == 1
            env.native.asr_release.set()
            result = await asyncio.wait_for(pending, 5)
            assert result["entry"]["kind"] == "conversation.window"
            settled = settings.snapshot()
            assert settled["operation_state"]["native_audio"] == 0
            assert settled["busy"] is False
        finally:
            env.native.asr_release.set()
            if pending is not None and not pending.done():
                await asyncio.wait_for(pending, 5)
            await settings.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ROLES)
async def test_every_role_rejects_malformed_operation_observations(tmp_path, monkeypatch, role):
    async with environment(tmp_path) as env:
        settings = _settings(role, env.runtime)
        try:
            # Controlled producer-boundary inputs, not a replacement validator.
            valid = dict.fromkeys(BASE_KEYS, 0)
            malformed = [
                {key: value for key, value in valid.items() if key != "native_audio"},
                {**valid, "unrecognized_operation": 0},
                *(
                    {**valid, "native_audio": value}
                    for value in (True, -1, 1.0, "1", None, MAX_COUNT + 1)
                ),
            ]
            with monkeypatch.context() as patch:
                for observed in malformed:
                    patch.setattr(
                        env.runtime, "model_operations", lambda value=observed: dict(value)
                    )
                    with pytest.raises((ValueError, RuntimeError)):
                        settings.snapshot()
                patch.setattr(env.runtime, "model_operations", lambda: {**valid, "native_audio": 1})
                native_only = settings.snapshot()
                assert native_only["busy"] is True
                assert all(
                    native_only["operation_state"][key] == 0 for key in BASE_KEYS - {"native_audio"}
                )
                conflict = {
                    "converse": ConverseModelConflict,
                    "stt": STTModelConflict,
                    "tts": TTSModelConflict,
                }[role]
                with pytest.raises(conflict):
                    settings.change(_change(role, native_only))
                patch.setattr(
                    env.runtime, "model_operations", lambda: {**valid, "native_audio": MAX_COUNT}
                )
                bounded = settings.snapshot()
                assert bounded["operation_state"]["native_audio"] == MAX_COUNT
                assert bounded["busy"] is True
        finally:
            await settings.close()
