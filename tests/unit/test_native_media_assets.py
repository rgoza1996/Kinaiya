"""Opaque native media binding; synthetic signature bytes, no decoder/provider."""

import hashlib
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

import services.agent.media_assets as media_module
from services.agent.artifact_api import router
from services.agent.artifacts import (
    ArtifactInvalid,
    ArtifactNotFound,
    ArtifactStore,
    validate_components,
)
from services.agent.media_assets import MediaAssetError, MediaAssetNotFound, NativeMediaAssets
from services.common.auth import Auth

SOURCE = b"\x00\x00\x00\x18ftypmp42synthetic source fixture"
HIGHLIGHTED = b"\x00\x00\x00\x18ftypmp42synthetic highlighted fixture"
BINDING = {
    "task_id": "task-1",
    "objective_revision": 1,
    "session_id": "session-1",
    "thread_id": "thread-1",
    "epoch": 1,
}
SCOPE = "private_cloud_authorized"


def register(assets, **changes):
    return assets.register(
        **{
            "binding": BINDING,
            "content_scope": SCOPE,
            "source_bytes": SOURCE,
            "source_sha256": hashlib.sha256(SOURCE).hexdigest(),
            "rendered_mp4": HIGHLIGHTED,
            "overlay_sha256": "a" * 64,
            "width": 1280,
            "height": 720,
            "duration_seconds": 2.0,
            "fps_num": 30,
            "fps_den": 1,
            "frame_count": 60,
            **changes,
        }
    )


def task(**changes):
    return SimpleNamespace(**{**BINDING, "content_scope": SCOPE, **changes})


def component(asset_id, cid="clip"):
    return {"id": cid, "type": "media", "data": {"asset_id": asset_id}}


def publish(store, asset_id, native_task=None):
    return store.publish(
        native_task or task(),
        {
            "title": "Synthetic video",
            "summary": "Publication only",
            "components": [component(asset_id)],
        },
    )


@pytest.fixture
def env(tmp_path):
    assets = NativeMediaAssets(tmp_path / "assets.sqlite")
    store = ArtifactStore(tmp_path / "artifacts.sqlite", media_assets=assets)
    receipt = register(assets)
    try:
        yield assets, store, receipt
    finally:
        store.close()
        assets.close()


def test_exact_native_pair_is_immutable_idempotent_and_durable(env, tmp_path):
    assets, _, receipt = env
    assert register(assets) == receipt
    assert receipt["binding"] == BINDING and receipt["content_scope"] == SCOPE
    assert receipt["publication_only"] is True
    assert set(receipt["source"]) == {"sha256", "bytes", "mime_type"}
    assert receipt["highlighted"]["sha256"] == hashlib.sha256(HIGHLIGHTED).hexdigest()
    receipt["binding"]["task_id"] = "forged"
    actual = register(assets)
    assert actual["binding"] == BINDING
    assets.close()
    restored = NativeMediaAssets(tmp_path / "assets.sqlite")
    try:
        assert restored.get(actual["asset_id"], binding=BINDING, content_scope=SCOPE) == actual
        assert (
            restored.content(
                actual["asset_id"], binding=BINDING, content_scope=SCOPE, variant="source"
            )
            == SOURCE
        )
    finally:
        restored.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"source_sha256": "b" * 64},
        {"source_bytes": b"not a video"},
        {"rendered_mp4": b"not a video"},
        {"overlay_sha256": "invalid"},
        {"width": True},
        {"height": 0},
        {"duration_seconds": float("nan")},
        {"duration_seconds": True},
        {"fps_den": 0},
        {"frame_count": 0},
        {"content_scope": "public"},
        {"binding": {**BINDING, "epoch": True}},
        {"binding": {**BINDING, "objective_revision": 0}},
        {"binding": {**BINDING, "grant": "forged"}},
    ],
)
def test_malformed_native_registration_refused(env, changes):
    with pytest.raises(MediaAssetError):
        register(env[0], **changes)


@pytest.mark.parametrize(
    "field,value",
    [
        ("task_id", "another-task"),
        ("session_id", "another-session"),
        ("thread_id", "another-thread"),
        ("epoch", 2),
        ("objective_revision", 2),
    ],
)
def test_asset_cannot_cross_native_binding(env, field, value):
    assets, store, receipt = env
    with pytest.raises(MediaAssetNotFound):
        assets.get(receipt["asset_id"], binding={**BINDING, field: value}, content_scope=SCOPE)
    with pytest.raises(ArtifactInvalid):
        publish(store, receipt["asset_id"], task(**{field: value}))


def test_private_pair_cannot_publish_as_public_or_without_native_registry(env, tmp_path):
    _, store, receipt = env
    with pytest.raises(ArtifactInvalid):
        publish(store, receipt["asset_id"], task(content_scope="public_synthetic"))
    unconfigured = ArtifactStore(tmp_path / "plain.sqlite")
    try:
        with pytest.raises(ArtifactInvalid):
            publish(unconfigured, receipt["asset_id"])
    finally:
        unconfigured.close()


def test_only_opaque_id_and_one_media_component_are_accepted(env):
    asset_id = env[2]["asset_id"]
    for data in (
        {"asset_id": asset_id, "url": "https://example.invalid/clip"},
        {"asset_id": "/private/source.mp4"},
        {"asset_id": "forged"},
    ):
        with pytest.raises(ArtifactInvalid):
            validate_components([{"id": "clip", "type": "media", "data": data}])
    with pytest.raises(ArtifactInvalid):
        validate_components([component(asset_id, "a"), component(asset_id, "b")])


def test_patch_rechecks_asset_binding_and_original_remains_unchanged(env):
    assets, store, receipt = env
    envelope = publish(store, receipt["asset_id"])
    other = register(assets, binding={**BINDING, "task_id": "another-task"})
    with pytest.raises(ArtifactInvalid):
        store.publish(
            task(),
            {
                "artifact_id": envelope["artifact_id"],
                "expected_revision": 1,
                "updates": [{"id": "clip", "data": {"asset_id": other["asset_id"]}}],
            },
        )
    assert (
        store.get(
            envelope["artifact_id"],
            session_id=BINDING["session_id"],
            thread_id=BINDING["thread_id"],
            epoch=1,
        )["revision"]
        == 1
    )


def test_asset_content_requires_actual_artifact_reference(env):
    _, store, receipt = env
    ordinary = store.publish(
        task(),
        {
            "title": "Ordinary",
            "summary": "No media reference",
            "components": [{"id": "text", "type": "text", "data": {"text": "Fixture"}}],
        },
    )
    with pytest.raises(ArtifactNotFound):
        store.media(
            ordinary["artifact_id"],
            receipt["asset_id"],
            session_id="session-1",
            thread_id="thread-1",
            epoch=1,
        )


def test_quotas_refuse_new_original_but_preserve_idempotent_receipt(env, monkeypatch):
    assets, _, receipt = env
    monkeypatch.setattr(media_module, "MAX_ASSETS", 1)
    assert register(assets) == receipt
    with pytest.raises(MediaAssetError):
        register(assets, overlay_sha256="b" * 64)


def test_closing_artifact_store_does_not_close_shared_registry(env):
    assets, store, receipt = env
    store.close()
    assert assets.get(receipt["asset_id"], binding=BINDING, content_scope=SCOPE) == receipt


async def test_owner_scoped_media_api_metadata_and_both_variants(env):
    _, store, receipt = env
    envelope = publish(store, receipt["asset_id"])
    auth = Auth({"owner": "o" * 32, "agent": "a" * 32, "decisions": "d" * 32})
    app = FastAPI()
    app.include_router(router(SimpleNamespace(artifact_store=store, _tasks={}), auth))
    path = f"/v1/artifacts/{envelope['artifact_id']}/media/{receipt['asset_id']}"
    scope = {"session_id": "session-1", "thread_id": "thread-1", "epoch": 1}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        for role, status in ((None, 401), ("agent", 403), ("decisions", 403), ("owner", 200)):
            headers = {"Authorization": "Bearer " + auth.tokens[role]} if role else {}
            response = await client.get(path, params=scope, headers=headers)
            assert response.status_code == status
        headers = {"Authorization": "Bearer " + auth.tokens["owner"]}
        assert response.json() == receipt
        assert response.headers["cache-control"] == "no-store"
        for variant, expected in (("source", SOURCE), ("highlighted", HIGHLIGHTED)):
            response = await client.get(
                path + "/content", params={**scope, "variant": variant}, headers=headers
            )
            assert response.status_code == 200 and response.content == expected
            assert response.headers["content-type"] == "video/mp4"
            assert response.headers["cache-control"] == "no-store"
        assert (
            await client.get(path, params={**scope, "epoch": 2}, headers=headers)
        ).status_code == 404
        assert (
            await client.get(
                path + "/content", params={**scope, "variant": "remote"}, headers=headers
            )
        ).status_code == 422
        assert (await client.post(path, json={}, headers=headers)).status_code == 405
