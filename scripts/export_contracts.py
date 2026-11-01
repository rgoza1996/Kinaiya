"""Emit language-neutral schemas used by Python and Flutter clients."""

import json
from pathlib import Path

from packages.contracts import (
    agent_control,
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

# These strict, immutable control types deliberately require every wire field;
# they do not inherit the legacy event defaults from WireModel.
for name in (
    "AgentModeChange",
    "AgentControlSnapshot",
    "AgentModeReceipt",
    "AgentModeLookupResult",
    "AgentTaskIdentity",
    "AgentSteerChange",
    "AgentSteerSource",
    "AgentSteerAdmissionRequest",
    "AgentSteerReceipt",
    "AgentSteerLookupResult",
):
    cls = getattr(agent_control, name)
    (target / f"{name}.json").write_text(json.dumps(cls.model_json_schema(), indent=2) + "\n")
