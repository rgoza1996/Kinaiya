"""Emit language-neutral schemas used by Python and Flutter clients."""

import json
from pathlib import Path

from packages.contracts import (
    agent_export,
    agent_submit,
    companion,
    context,
    converse_setup,
    memory,
    memory_catalog,
    memory_context,
    memory_operations,
    models,
    tool_permissions,
)

target = Path(__file__).resolve().parents[1] / "packages/contracts/schemas"
target.mkdir(parents=True, exist_ok=True)
for module in (
    models,
    companion,
    memory,
    memory_catalog,
    memory_context,
    memory_operations,
    context,
    agent_export,
    agent_submit,
):
    for name in dir(module):
        cls = getattr(module, name)
        if isinstance(cls, type) and issubclass(cls, models.WireModel):
            (target / f"{name}.json").write_text(
                json.dumps(cls.model_json_schema(), indent=2) + "\n"
            )

# This owner document uses its explicit contract_version instead of the legacy
# event schema_version. Nested setup types are included in the root's $defs.
(target / "ConverseSetup.json").write_text(
    json.dumps(converse_setup.ConverseSetup.model_json_schema(), indent=2) + "\n"
)

# Owner policy requests are strict frozen models, separate from journal events.
for cls in (tool_permissions.PermissionScope, tool_permissions.ToolPermissionChange):
    (target / f"{cls.__name__}.json").write_text(
        json.dumps(cls.model_json_schema(), indent=2) + "\n"
    )
