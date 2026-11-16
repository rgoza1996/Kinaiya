"""#630: five Core exports are additive to the unchanged legacy codec pin."""

import hashlib
import json
from pathlib import Path

from packages.contracts.core_subsystems import (
    CoreSubsystemChange,
    CoreSubsystemLookup,
    CoreSubsystemReceipt,
    CoreSubsystemSelection,
    CoreSubsystemSnapshot,
)

MODELS = (
    CoreSubsystemChange,
    CoreSubsystemLookup,
    CoreSubsystemReceipt,
    CoreSubsystemSelection,
    CoreSubsystemSnapshot,
)


def test_exact_core_selector_export_group_has_its_own_byte_pin():
    root = Path(__file__).resolve().parents[2] / "packages/contracts/schemas"
    paths = sorted(root / f"{model.__name__}.json" for model in MODELS)
    assert len(paths) == 5
    raw = b"".join(path.name.encode() + b"\0" + path.read_bytes() + b"\0" for path in paths)
    assert (
        hashlib.sha256(raw).hexdigest()
        == "4fb6458ec343a7ee867476698cbd954e75ad4174897ae4fe02fa051a950831b5"
    )
    for model in MODELS:
        assert (
            json.loads((root / f"{model.__name__}.json").read_text()) == model.model_json_schema()
        )
