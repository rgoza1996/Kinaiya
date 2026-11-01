"""Pure preparation of a native extension owner's next workspace reference."""

from pathlib import Path


def prepare_disjoint_workspace(root, workspace):
    workspace = Path(workspace)
    control = Path(root).resolve()
    if (
        not workspace.is_absolute()
        or workspace.resolve() != workspace
        or not workspace.is_dir()
        or workspace == control
        or workspace.is_relative_to(control)
        or control.is_relative_to(workspace)
    ):
        raise ValueError("Workspace must be a canonical directory disjoint from control state")
    return workspace
