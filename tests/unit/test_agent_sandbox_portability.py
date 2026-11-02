"""Agent shell sandbox mounts must be portable across claimed Linux architectures.

Found by the hosted aarch64 installer qualification (#261): Ubuntu arm64 has no
/lib64, and a mandatory bind made every sandboxed command fail closed.
"""

from core.policy import ToolPolicy
from services.agent import tools as agent_tools


def test_lib64_is_optional_read_only_while_core_roots_stay_mandatory(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_tools, "bwrap_path", lambda: "/usr/bin/bwrap")
    argv, isolated = agent_tools.build_bwrap_args(ToolPolicy(workspace=tmp_path), "true", False)
    assert isolated
    position = argv.index("/lib64")
    assert argv[position - 1 : position + 2] == ["--ro-bind-try", "/lib64", "/lib64"]
    for root in ("/usr", "/bin", "/lib"):
        index = argv.index(root)
        assert argv[index - 1 : index + 2] == ["--ro-bind", root, root]
    # Optional binds are never writable binds.
    assert "--bind-try" not in argv
