"""Controller/pinning service tests: real synthetic skills, no network."""

import hashlib
import json
import os
import stat as _stat
import zipfile
from pathlib import Path

import pytest

from packages.contracts.models import ProviderInfo
from packages.ecosystem.skills import SourceRef
from services.agent import skill_service as svc
from services.agent.skill_service import SkillService, SkillServiceError

LOCAL = ProviderInfo(provider="test-local", model="tiny-local", local=True)
META_ALIAS = ProviderInfo(provider="meta", model="maverick", local=False)
CONTRIBUTOR = ProviderInfo(provider="other-cloud", model="contributor-large", local=False)
OTHER_CLOUD = ProviderInfo(provider="openai-compatible", model="gpt-x", local=False)


def make_skill(
    path,
    name="demo",
    description="demo skill",
    version="1.0.0",
    body="Do the thing.\n",
    extra="",
    files=None,
):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    front = f"---\nname: {name}\ndescription: {description}\nversion: {version}\n{extra}---\n"
    (path / "SKILL.md").write_text(front + body, encoding="utf-8")
    for rel, data in (files or {}).items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data if isinstance(data, bytes) else data.encode())
    return path


def make_service(tmp_path, name="root"):
    root = tmp_path / name
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return SkillService(root, workspace)


def zip_of(tree, archive):
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as archive_file:
        for item in sorted(tree.rglob("*")):
            if item.is_file() and not item.is_symlink():
                archive_file.write(item, item.relative_to(tree).as_posix())
    return archive


async def install_enabled(service, source, scope=None, ref=None):
    out = await service.mutate(
        "install",
        authority="owner",
        source=source,
        source_ref=ref,
        content_scope=scope,
    )
    await service.mutate("enable", authority="owner", skill_id=out["id"])
    return out["id"]


# -- roundtrip ---------------------------------------------------------------


async def test_install_enable_load_roundtrip(tmp_path):
    service = make_service(tmp_path)
    try:
        src = make_skill(tmp_path / "src")
        skill_id = await install_enabled(service, src)
        assert skill_id == "local/demo"
        result = await service.load("task-1", skill_id, LOCAL)
        assert result["body"] == "Do the thing.\n"
        assert result["untrusted"] is True
        assert result["digest"].startswith("sha256:")
        assert result["source"]["kind"] == "local"
        assert result["invocation"] == "model"
        # Returned structures are copies: mutating them touches no pin.
        result["source"]["kind"] = "tampered"
        again = await service.load("task-1", skill_id, LOCAL)
        assert again["source"]["kind"] == "local"
    finally:
        service.close()


async def test_install_archive_update_archive_rollback(tmp_path):
    service = make_service(tmp_path)
    try:
        src = make_skill(tmp_path / "src", body="version one.\n")
        archive = zip_of(src, tmp_path / "v1.zip")
        out = await service.mutate("install_archive", authority="owner", source=archive)
        skill_id = out["id"]
        await service.mutate("enable", authority="owner", skill_id=skill_id)
        first = await service.load("task-a", skill_id, LOCAL)
        assert first["body"] == "version one.\n"
        src2 = make_skill(tmp_path / "src2", body="version two.\n")
        archive2 = zip_of(src2, tmp_path / "v2.zip")
        await service.mutate(
            "update_archive", authority="owner", skill_id=skill_id, source=archive2
        )
        await service.mutate("rollback", authority="owner", skill_id=skill_id)
        fresh = await service.load("task-b", skill_id, LOCAL)
        assert fresh["body"] == "version one.\n"
        assert fresh["digest"] == first["digest"]
    finally:
        service.close()


async def test_resource_load_preserves_script_and_license_without_execution(tmp_path):
    service = make_service(tmp_path)
    try:
        sentinel = tmp_path / "executed.txt"
        src = make_skill(
            tmp_path / "src",
            files={
                "helper.py": "print('must never run')\n",
                "LICENSE": "synthetic license text\n",
            },
        )
        skill_id = await install_enabled(service, src)
        assert not sentinel.exists()
        result = await service.load("task-1", skill_id, LOCAL, resource="LICENSE")
        assert result["resource_path"] == "LICENSE"
        assert result["resource_text"] == "synthetic license text\n"
        assert (
            result["resource_sha256"]
            == "sha256:" + hashlib.sha256(b"synthetic license text\n").hexdigest()
        )
        assert "body" not in result
        assert result["digest"].startswith("sha256:")
        script = await service.load("task-1", skill_id, LOCAL, resource="helper.py")
        assert "print" in script["resource_text"]
        assert not sentinel.exists()
        # Installed bytes really preserved the script and license.
        installed = list((tmp_path / "root" / "installed").rglob("helper.py"))
        assert len(installed) == 1
    finally:
        service.close()


# -- author invocation controls ----------------------------------------------


async def test_model_user_restrictions_apply_to_load_but_not_catalog_shape(tmp_path):
    service = make_service(tmp_path)
    try:
        model_blocked = make_skill(
            tmp_path / "src-model",
            name="quiet",
            extra="disable-model-invocation: true\n",
        )
        manual = make_skill(tmp_path / "src-manual", name="hands", extra="manual-only: true\n")
        quiet = await install_enabled(service, model_blocked)
        hands = await install_enabled(service, manual)
        with pytest.raises(SkillServiceError, match="model invocation is disabled"):
            await service.load("task-1", quiet, LOCAL)
        user_ok = await service.load("task-1", quiet, LOCAL, invocation="user")
        assert user_ok["body"] == "Do the thing.\n"
        with pytest.raises(SkillServiceError, match="model invocation is disabled"):
            await service.load("task-1", hands, LOCAL, invocation="model")
        # Neither skill is visible to the model catalog.
        page = await service.catalog(LOCAL)
        assert page["skills"] == []
        assert page["next_after"] is None
    finally:
        service.close()


# -- scope / provider policy ---------------------------------------------------


async def test_default_local_only_denies_contributor_alias_before_export(tmp_path):
    service = make_service(tmp_path)
    try:
        skill_id = await install_enabled(service, make_skill(tmp_path / "src"))
        owner = await service.inspect_owner(skill_id)
        assert owner["scope"] == "local_only"
        # Local models see it; Meta (even under an alias without
        # "contributor" in the name) sees and loads nothing.
        assert [row["id"] for row in (await service.catalog(LOCAL))["skills"]] == [skill_id]
        meta_page = await service.catalog(META_ALIAS)
        assert meta_page == {"skills": [], "next_after": None}
        with pytest.raises(SkillServiceError, match="not disclosable"):
            await service.load("task-1", skill_id, META_ALIAS)
        assert await service.load("task-1", skill_id, LOCAL)
    finally:
        service.close()


async def test_other_cloud_gets_explicit_scopes_only(tmp_path):
    service = make_service(tmp_path)
    try:
        names = {}
        for name, scope in (
            ("loc", "local_only"),
            ("pub", "public_synthetic"),
            ("priv", "private_cloud_authorized"),
        ):
            src = make_skill(tmp_path / f"src-{name}", name=name)
            names[name] = await install_enabled(service, src, scope=scope)
        cloud_ids = sorted(row["id"] for row in (await service.catalog(OTHER_CLOUD))["skills"])
        assert cloud_ids == sorted([names["pub"], names["priv"]])
        contrib_ids = sorted(row["id"] for row in (await service.catalog(CONTRIBUTOR))["skills"])
        assert contrib_ids == [names["pub"]]
        with pytest.raises(SkillServiceError, match="not disclosable"):
            await service.load("task-1", names["priv"], CONTRIBUTOR)
        assert await service.load("task-1", names["priv"], OTHER_CLOUD)
        with pytest.raises(SkillServiceError, match="not disclosable"):
            await service.load("task-1", names["loc"], OTHER_CLOUD)
    finally:
        service.close()


async def test_scope_persists_across_restart(tmp_path):
    service = make_service(tmp_path)
    src = make_skill(tmp_path / "src")
    skill_id = await install_enabled(service, src)
    await service.mutate(
        "scope",
        authority="owner",
        skill_id=skill_id,
        content_scope="public_synthetic",
    )
    service.close()
    reopened = SkillService(tmp_path / "root", tmp_path / "workspace")
    try:
        assert (await reopened.inspect_owner(skill_id))["scope"] == "public_synthetic"
        assert [row["id"] for row in (await reopened.catalog(OTHER_CLOUD))["skills"]] == [skill_id]
    finally:
        reopened.close()


# -- pins ----------------------------------------------------------------------


async def test_revocation_blocks_pin_and_reenable_permits_it(tmp_path):
    service = make_service(tmp_path)
    try:
        skill_id = await install_enabled(service, make_skill(tmp_path / "src"))
        before = await service.load("task-1", skill_id, LOCAL)
        await service.mutate("disable", authority="owner", skill_id=skill_id)
        with pytest.raises(SkillServiceError, match="not enabled"):
            await service.load("task-1", skill_id, LOCAL)
        page = await service.catalog(LOCAL)
        assert page["skills"] == []
        await service.mutate("enable", authority="owner", skill_id=skill_id)
        after = await service.load("task-1", skill_id, LOCAL)
        assert after["digest"] == before["digest"]
        assert after["body"] == before["body"]
    finally:
        service.close()


async def test_pin_immutable_across_update_and_release_recaptures(tmp_path):
    service = make_service(tmp_path)
    try:
        skill_id = await install_enabled(
            service, make_skill(tmp_path / "v1", body="version one.\n")
        )
        pinned = await service.load("task-a", skill_id, LOCAL)
        # Updates stay allowed while pins exist: pins hold captured bytes.
        await service.mutate(
            "update",
            authority="owner",
            skill_id=skill_id,
            source=make_skill(tmp_path / "v2", body="version two.\n"),
        )
        assert (await service.load("task-a", skill_id, LOCAL))["body"] == "version one.\n"
        assert (await service.load("task-a", skill_id, LOCAL))["digest"] == pinned["digest"]
        fresh = await service.load("task-b", skill_id, LOCAL)
        assert fresh["body"] == "version two.\n"
        assert fresh["digest"] != pinned["digest"]
        # A restarted incarnation releases first, then sees the new version.
        await service.release_task("task-a")
        recaptured = await service.load("task-a", skill_id, LOCAL)
        assert recaptured["body"] == "version two.\n"
    finally:
        service.close()


async def test_remove_refuses_dependents_and_release_permits(tmp_path):
    service = make_service(tmp_path)
    try:
        skill_id = await install_enabled(service, make_skill(tmp_path / "src"))
        await service.load("task-1", skill_id, LOCAL)
        await service.load("task-2", skill_id, LOCAL)
        with pytest.raises(SkillServiceError, match=r"task-1.*task-2|task-2.*task-1"):
            await service.mutate("remove", authority="owner", skill_id=skill_id)
        owner = await service.inspect_owner(skill_id)
        assert owner["dependent_task_ids"] == ["task-1", "task-2"]
        await service.release_task("task-1")
        with pytest.raises(SkillServiceError, match="task-2"):
            await service.mutate("remove", authority="owner", skill_id=skill_id)
        await service.release_task("task-2")
        # Releasing is idempotent, including unknown tasks.
        await service.release_task("task-2")
        await service.release_task("never-seen")
        assert await service.mutate("remove", authority="owner", skill_id=skill_id) == {
            "id": skill_id,
            "removed": True,
        }
        with pytest.raises(SkillServiceError, match="unknown skill"):
            await service.inspect_owner(skill_id)
    finally:
        service.close()


async def test_pin_bounds_fail_explicitly(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    try:
        first = await install_enabled(service, make_skill(tmp_path / "s1", name="one"))
        second = await install_enabled(service, make_skill(tmp_path / "s2", name="two"))
        monkeypatch.setattr(svc, "MAX_PINS", 1)
        assert await service.load("task-1", first, LOCAL)
        with pytest.raises(SkillServiceError, match="too many pinned"):
            await service.load("task-2", second, LOCAL)
        monkeypatch.setattr(svc, "MAX_PINS", 32)
        monkeypatch.setattr(svc, "MAX_PIN_BYTES", 1)
        with pytest.raises(SkillServiceError, match="exceed"):
            await service.load("task-3", second, LOCAL)
    finally:
        service.close()


# -- single controller -----------------------------------------------------------


def test_two_controllers_rejected_until_close(tmp_path):
    first = make_service(tmp_path)
    with pytest.raises(SkillServiceError, match="another skill controller"):
        make_service(tmp_path)
    first.close()
    second = make_service(tmp_path)
    try:
        assert not second.closed
    finally:
        second.close()
    # Close is idempotent and the lock really is free afterwards.
    first.close()
    third = make_service(tmp_path)
    third.close()


def test_lockfile_is_regular_0600_and_no_follow(tmp_path):
    service = make_service(tmp_path)
    try:
        lock = tmp_path / "root" / "controller.lock"
        st = os.lstat(lock)
        assert _stat.S_ISREG(st.st_mode)
        assert _stat.S_IMODE(st.st_mode) == 0o600
    finally:
        service.close()
    # A pre-existing symlink at the lock path is refused, never followed.
    root = tmp_path / "root2"
    root.mkdir()
    outside = tmp_path / "outside-target"
    outside.write_text("SYNTHETIC_SENTINEL")
    (root / "controller.lock").symlink_to(outside)
    with pytest.raises(SkillServiceError, match="lock"):
        SkillService(root, tmp_path / "workspace")
    assert outside.read_text() == "SYNTHETIC_SENTINEL"


def test_symlinked_scopes_refused(tmp_path):
    root = tmp_path / "root"
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    root.mkdir()
    outside = tmp_path / "scopes-outside.json"
    outside.write_text(json.dumps({"local/x": "public_synthetic"}))
    (root / "scopes.json").symlink_to(outside)
    with pytest.raises(SkillServiceError, match="symlink"):
        SkillService(root, workspace)
    assert json.loads(outside.read_text()) == {"local/x": "public_synthetic"}


async def test_ops_after_close_refused(tmp_path):
    service = make_service(tmp_path)
    skill_id = await install_enabled(service, make_skill(tmp_path / "src"))
    service.close()
    assert service.closed
    with pytest.raises(SkillServiceError, match="closed"):
        await service.load("task-1", skill_id, LOCAL)
    with pytest.raises(SkillServiceError, match="closed"):
        await service.list_owner()
    with pytest.raises(SkillServiceError, match="closed"):
        await service.release_task("task-1")


# -- malformed inputs --------------------------------------------------------------


async def test_malformed_inputs_rejected(tmp_path):
    service = make_service(tmp_path)
    try:
        skill_id = await install_enabled(service, make_skill(tmp_path / "src"))
        with pytest.raises(SkillServiceError, match="unknown operation"):
            await service.mutate("fetch", authority="owner")
        with pytest.raises(SkillServiceError, match="owner authority"):
            await service.mutate("enable", authority="model", skill_id=skill_id)
        with pytest.raises(SkillServiceError, match="takes no skill_id"):
            await service.mutate(
                "install",
                authority="owner",
                skill_id=skill_id,
                source=make_skill(tmp_path / "other"),
            )
        with pytest.raises(SkillServiceError, match="takes no source"):
            await service.mutate(
                "enable",
                authority="owner",
                skill_id=skill_id,
                source=make_skill(tmp_path / "other2"),
            )
        with pytest.raises(SkillServiceError, match="takes no source_ref"):
            await service.mutate(
                "enable",
                authority="owner",
                skill_id=skill_id,
                source_ref=SourceRef(kind="local", namespace="local", name="demo", raw="demo"),
            )
        with pytest.raises(SkillServiceError, match="takes no content_scope"):
            await service.mutate(
                "disable",
                authority="owner",
                skill_id=skill_id,
                content_scope="public_synthetic",
            )
        with pytest.raises(SkillServiceError, match="needs content_scope"):
            await service.mutate("scope", authority="owner", skill_id=skill_id)
        with pytest.raises(SkillServiceError, match="never a dict"):
            await service.mutate(
                "install",
                authority="owner",
                source=make_skill(tmp_path / "d"),
                source_ref={"kind": "local"},  # type: ignore[arg-type]
            )
        with pytest.raises(SkillServiceError, match="not performed here"):
            await service.mutate(
                "install",
                authority="owner",
                source="https://example.com/skill.zip",
            )
        with pytest.raises(SkillServiceError, match="content_scope"):
            await service.mutate(
                "scope",
                authority="owner",
                skill_id=skill_id,
                content_scope="world_readable",
            )
        with pytest.raises(SkillServiceError, match="unknown skill"):
            await service.mutate("enable", authority="owner", skill_id="local/missing")
        for bad_task in ("", "../escape", "x" * 200, None, 42):
            with pytest.raises(SkillServiceError, match="task_id"):
                await service.load(bad_task, skill_id, LOCAL)  # type: ignore[arg-type]
        for bad_after in (-1, True, "0", 1 << 30):
            with pytest.raises(SkillServiceError, match="after"):
                await service.list_owner(after=bad_after)  # type: ignore[arg-type]
                await service.catalog(LOCAL, after=bad_after)  # type: ignore[arg-type]
        with pytest.raises(SkillServiceError, match="limit"):
            await service.list_owner(limit=64)
        with pytest.raises(SkillServiceError, match="unknown invocation"):
            await service.load("task-1", skill_id, LOCAL, invocation="tool")
        for bad_resource in ("", "/absolute", "../escape", "a" * 2000):
            with pytest.raises(SkillServiceError, match="[Rr]esource"):
                await service.load("task-1", skill_id, LOCAL, resource=bad_resource)
        with pytest.raises(SkillServiceError, match="regular file"):
            await service.load("task-1", skill_id, LOCAL, resource="missing.txt")
        with pytest.raises(SkillServiceError, match="provider_info"):
            await service.load("task-1", skill_id, object())
        with pytest.raises(SkillServiceError, match="skill_id"):
            await service.inspect_owner("not-an-id")
    finally:
        service.close()


# -- bounds ------------------------------------------------------------------------


async def test_bounded_pages_and_payloads(tmp_path):
    service = make_service(tmp_path)
    try:
        for index in range(6):
            src = make_skill(
                tmp_path / f"src-{index}",
                name=f"skill{index}",
                description="d" * 200,
            )
            await install_enabled(service, src)
        first = await service.catalog(LOCAL)
        assert len(first["skills"]) == 4
        assert first["next_after"] == 4
        for row in first["skills"]:
            assert len(row["description"]) <= 80
        assert len(json.dumps(first, sort_keys=True)) <= 1600
        second = await service.catalog(LOCAL, after=4)
        assert len(second["skills"]) == 2
        assert second["next_after"] is None
        owners = await service.list_owner()
        assert len(owners["skills"]) == 6
        assert owners["next_after"] is None
        head = await service.list_owner(limit=5)
        assert len(head["skills"]) == 5
        assert head["next_after"] == 5
        tail = await service.list_owner(after=5, limit=5)
        assert len(tail["skills"]) == 1
        assert tail["next_after"] is None
        for entry in owners["skills"]:
            assert entry["scope"] == "local_only"
    finally:
        service.close()


async def test_oversized_result_fails_with_smaller_resource_hint(tmp_path):
    service = make_service(tmp_path)
    try:
        src = make_skill(
            tmp_path / "src", body=("x" * 100 + "\n") * 200, files={"tiny.txt": "ok\n"}
        )
        skill_id = await install_enabled(service, src)
        with pytest.raises(SkillServiceError, match="needs-smaller-resource"):
            await service.load("task-1", skill_id, LOCAL)
        small = await service.load("task-1", skill_id, LOCAL, resource="tiny.txt")
        assert small["resource_text"] == "ok\n"
        assert len(json.dumps(small, sort_keys=True)) <= 12000
    finally:
        service.close()


async def test_scope_write_failure_fails_closed_to_local_only(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    try:
        skill_id = await install_enabled(service, make_skill(tmp_path / "src"))

        def boom():
            raise SkillServiceError("synthetic disk failure")

        monkeypatch.setattr(service, "_write_scopes", boom)
        with pytest.raises(SkillServiceError, match="synthetic disk failure"):
            await service.mutate(
                "scope",
                authority="owner",
                skill_id=skill_id,
                content_scope="public_synthetic",
            )
        # The failure never leaves a broader scope in force.
        assert (await service.inspect_owner(skill_id))["scope"] == "local_only"
        assert (await service.catalog(OTHER_CLOUD))["skills"] == []
    finally:
        service.close()
