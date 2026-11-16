"""#623: capture preference schemas form a distinct additive export family."""

import hashlib
import json
from pathlib import Path

from packages.contracts import capture_modes


def test_capture_mode_exports_are_exact_and_preserve_their_own_group_pin():
    root = Path(__file__).resolve().parents[2] / "packages/contracts/schemas"
    names = [
        "CaptureModeChange",
        "CaptureModeLookup",
        "CaptureModeReceipt",
        "CaptureModeSelection",
        "CaptureModeSnapshot",
    ]
    paths = [root / (name + ".json") for name in names]
    raw = b"".join(p.name.encode() + b"\0" + p.read_bytes() + b"\0" for p in paths)
    assert (
        hashlib.sha256(raw).hexdigest()
        == "f1f035d09c8a98096d11dad62037f6f4f49af50880b5186ebf71b011bf8d0c62"
    )
    for name, path in zip(names, paths, strict=True):
        assert json.loads(path.read_bytes()) == getattr(capture_modes, name).model_json_schema()
