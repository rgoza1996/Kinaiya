"""Source/grant and original-call boundaries; synthetic bytes, no decoder/API."""

import asyncio
import hashlib

import pytest

from core.policy import ToolPolicy
from packages.contracts.agent_media import (
    ConsultMediaRequest,
    MediaPrimaryResultTarget,
    MediaProviderResult,
    MediaSourceGrant,
    MediaTarget,
    MediaTaskBinding,
)
from services.agent.media_tools import MediaToolError, WorkspaceMediaBroker

VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"synthetic contract fixture"
SHA = hashlib.sha256(VIDEO).hexdigest()


def result(**changes):
    return MediaProviderResult.model_validate(
        {
            "model": "sam-3.1",
            "provider_request_sha256": "a" * 64,
            "provider_response_sha256": "b" * 64,
            "text": "Synthetic hands observation",
            "data": {"usage": {"images_processed": 1}},
            **changes,
        }
    )


def request(**changes):
    return {
        "operation": "sam_hands",
        "source_path": "clip.mp4",
        "source_sha256": SHA,
        **changes,
    }


def broker(tmp_path, provider, *, current=lambda: None, primary=False):
    (tmp_path / "clip.mp4").write_bytes(VIDEO)
    grant = MediaSourceGrant(
        grant_id="owner-grant-1",
        binding=MediaTaskBinding(
            task_id="task-1",
            objective_revision=1,
            session_id="session-1",
            thread_id="thread-1",
            epoch=1,
        ),
        source_path="clip.mp4",
        source_sha256=SHA,
        source_bytes=len(VIDEO),
        content_scope="private_cloud_authorized",
        targets=(MediaTarget(operation="sam_hands", model="sam-3.1", prompt="hands"),),
        primary_result_target=MediaPrimaryResultTarget() if primary else None,
    )
    return WorkspaceMediaBroker(
        policy=ToolPolicy(tmp_path), grant=grant, provider=provider, current=current
    )


@pytest.mark.asyncio
async def test_exact_native_grant_and_stable_source_reach_adapter_once(tmp_path):
    seen = []

    async def provider(prepared, current):
        current()
        seen.append(prepared)
        return result()

    owner = broker(tmp_path, provider)
    first = await owner.call(request())
    second = await owner.call(request())
    assert first == second and len(seen) == 1
    assert seen[0].source_bytes == VIDEO
    assert first.source_sha256 == SHA
    assert first.content_scope == "private_cloud_authorized"
    assert first.binding.task_id == "task-1"
    assert first.provider_request_sha256 != first.request_sha256
    assert "prompt" not in first.model_dump()
    assert "grant" not in first.model_dump()
    assert await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"source_path": "../clip.mp4"},
        {"source_path": "/clip.mp4"},
        {"source_sha256": "c" * 64},
        {"grant_id": "forged"},
        {"operation": "unknown"},
        {"prompt": "x" * 8001},
    ],
)
async def test_model_request_cannot_create_or_broaden_grant(tmp_path, changes):
    async def provider(*_):
        pytest.fail("Invalid request must not dispatch")

    owner = broker(tmp_path, provider)
    with pytest.raises(MediaToolError):
        await owner.call(request(**changes))
    assert await owner.close()


@pytest.mark.asyncio
async def test_changed_original_and_symlink_are_refused_before_dispatch(tmp_path):
    async def provider(*_):
        pytest.fail("Changed source must not dispatch")

    owner = broker(tmp_path, provider)
    (tmp_path / "clip.mp4").write_bytes(VIDEO[:-1] + b"!")
    with pytest.raises(MediaToolError, match="source_changed"):
        await owner.call(request())
    (tmp_path / "clip.mp4").unlink()
    (tmp_path / "other.mp4").write_bytes(VIDEO)
    (tmp_path / "clip.mp4").symlink_to("other.mp4")
    with pytest.raises(MediaToolError, match="source_unavailable"):
        await owner.call(request())


@pytest.mark.asyncio
async def test_cancelled_observer_and_changed_request_never_replace_original(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def provider(_prepared, current):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        current()
        return result()

    owner = broker(tmp_path, provider)
    observer = asyncio.create_task(owner.call(request()))
    await entered.wait()
    observer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await observer
    with pytest.raises(MediaToolError, match="invalid_request"):
        await owner.call(request(prompt="Changed prompt"))
    release.set()
    assert (await owner.call(request())).status == "completed"
    assert calls == 1
    assert await owner.close()


@pytest.mark.asyncio
async def test_failed_original_is_retained_without_dispatch_retry(tmp_path):
    calls = 0

    async def provider(*_):
        nonlocal calls
        calls += 1
        raise RuntimeError("Untrusted provider body must not escape")

    owner = broker(tmp_path, provider)
    for _ in range(2):
        with pytest.raises(MediaToolError, match="provider_failed"):
            await owner.call(request())
    assert calls == 1
    assert await owner.close()


@pytest.mark.asyncio
async def test_native_currentness_is_rechecked_after_provider_wait(tmp_path):
    live = True

    def current():
        if not live:
            raise RuntimeError("Native task revision changed")

    async def provider(*_):
        nonlocal live
        live = False
        return result()

    owner = broker(tmp_path, provider, current=current)
    with pytest.raises(MediaToolError, match="stale"):
        await owner.call(request())
    assert await owner.close()


@pytest.mark.asyncio
async def test_cleanup_observation_timeout_retains_cancellation_resistant_original(tmp_path):
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def provider(*_):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        return result()

    owner = broker(tmp_path, provider)
    observer = asyncio.create_task(owner.call(request()))
    await entered.wait()
    assert not await owner.close(timeout=0)
    await cancelled.wait()
    assert not owner._calls["sam_hands"][1].done()
    release.set()
    with pytest.raises(MediaToolError, match="stale"):
        await observer
    assert await owner.close()


def test_result_data_and_source_paths_refuse_non_json_and_excessive_values():
    for data in ({"bytes": b"raw"}, {"nan": float("nan")}, {"rows": [0] * 1025}):
        with pytest.raises(ValueError):
            result(data=data)
    with pytest.raises(ValueError):
        ConsultMediaRequest.model_validate(request(source_path="a//clip.mp4"))


@pytest.mark.asyncio
async def test_primary_export_requires_exact_original_and_optional_owner_target(tmp_path):
    async def provider(*_):
        return result()

    info = {"provider": "meta", "model": "muse-spark-1.3-contributor", "local": False}
    owner = broker(tmp_path, provider)
    observed = await owner.call(request())
    with pytest.raises(MediaToolError, match="disclosure_refused"):
        owner.check_primary_export(info, observed)
    assert await owner.close()

    owner = broker(tmp_path, provider, primary=True)
    observed = await owner.call(request())
    assert await owner.close()
    # Custody is closed; the exact grant remains current for later primary use.
    owner.check_primary_export(info, observed.model_dump(mode="json"))
    for changed in (
        {**info, "model": "different"},
        *({**info, "local": value} for value in (True, 0, 0.0, None, "False")),
        {key: value for key, value in info.items() if key != "local"},
    ):
        with pytest.raises(MediaToolError, match="disclosure_refused"):
            owner.check_primary_export(changed, observed)
    tampered = observed.model_dump(mode="json")
    tampered["text"] = "Unrelated private text"
    # Even a caller recomputing the self-digest cannot create an original.
    from packages.contracts.agent_media import digest

    tampered["result_sha256"] = digest({k: v for k, v in tampered.items() if k != "result_sha256"})
    with pytest.raises(MediaToolError, match="disclosure_refused"):
        owner.check_primary_export(info, tampered)


@pytest.mark.parametrize(
    "required",
    [
        ("sam_hands", "sam_hands"),
        ("transcribe_video",),
        ("unknown",),
    ],
)
def test_required_operations_cannot_duplicate_or_broaden_native_targets(tmp_path, required):
    owner = broker(tmp_path, None)
    value = owner.grant.model_dump(mode="python")
    value["required_operations"] = required
    with pytest.raises(ValueError):
        MediaSourceGrant.model_validate(value)


def test_native_required_operations_are_frozen_and_excluded_from_model_request(tmp_path):
    owner = broker(tmp_path, None)
    assert owner.required_operations == frozenset()
    value = owner.grant.model_dump(mode="python")
    value["required_operations"] = ("sam_hands",)
    grant = MediaSourceGrant.model_validate(value)
    owner = WorkspaceMediaBroker(
        policy=owner.policy, grant=grant, provider=None, current=lambda: None
    )
    assert owner.required_operations == frozenset({"sam_hands"})
    with pytest.raises(ValueError):
        ConsultMediaRequest.model_validate(request(required_operations=[]))
