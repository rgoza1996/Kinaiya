"""A selected Pi workspace must stay outside native protected storage."""

import os

import pytest

from services.decisions.pi_process import PiLaunchProfile, PiLaunchUnavailable, launch


def configuration(tmp_path, workspace=None):
    return PiLaunchProfile(
        profile_id="external-test",
        node="/usr/bin/true",
        sdk_root=str(tmp_path / "sdk"),
        agent_dir=str(tmp_path / "agent"),
        workspace=str(workspace or tmp_path / "workspace"),
    )


async def test_pi_launch_refuses_protected_workspace_before_allocating_history(tmp_path):
    data = tmp_path / "protected-data"
    data.mkdir()
    with pytest.raises(PiLaunchUnavailable):
        await launch(
            configuration(tmp_path, workspace=data),
            attachment_id="a" * 32,
            data_dir=data,
            protected=(data,),
            expected_session_id=None,
        )
    assert list(data.iterdir()) == []


async def test_pi_launch_refuses_hardlinks_in_workspace_before_allocating_history(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    for name in ("sdk", "agent", "workspace"):
        (tmp_path / name).mkdir(mode=0o700)
    secret = data / "protected.txt"
    secret.write_text("synthetic protected data")
    os.link(secret, tmp_path / "workspace" / "alias.txt")
    node = tmp_path / "node"
    node.write_text("synthetic executable")
    node.chmod(0o500)
    profile = configuration(tmp_path).model_copy(update={"node": str(node)})
    with pytest.raises(PiLaunchUnavailable):
        await launch(
            profile,
            attachment_id="a" * 32,
            data_dir=data,
            protected=(data,),
            expected_session_id=None,
        )
    assert not (data / "external-agent").exists()
