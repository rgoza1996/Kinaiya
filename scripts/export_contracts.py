"""Emit language-neutral schemas used by Python and Flutter clients."""

import json
from pathlib import Path

from packages.contracts import companion, models

target = Path(__file__).resolve().parents[1] / "packages/contracts/schemas"
target.mkdir(parents=True, exist_ok=True)
for module in (models, companion):
    for name in dir(module):
        cls = getattr(module, name)
        if isinstance(cls, type) and issubclass(cls, models.WireModel):
            (target / f"{name}.json").write_text(
                json.dumps(cls.model_json_schema(), indent=2) + "\n"
            )
