"""Native text adapter selection stays lazy and rejects typed Decisions use."""

import subprocess
import sys

import pytest

from packages.providers.lmstudio import LMStudioProvider
from services.launcher import provider


@pytest.mark.parametrize("role", ["agent", "decisions"])
def test_native_lmstudio_profile_respects_component_interfaces(role):
    config = {
        "providers": {
            role: {
                "kind": "lmstudio",
                "base_url": "http://127.0.0.1:1234",
                "model": "already-loaded",
            }
        }
    }
    if role == "decisions":
        with pytest.raises(SystemExit, match="Converse"):
            provider(config, role)
    else:
        adapter = provider(config, role)
        assert isinstance(adapter, LMStudioProvider) and adapter.info.local
        assert callable(adapter.prepare_complete)


def test_configured_converse_uses_native_adapter_without_loading_any_runtime():
    p = provider(
        {
            "providers": {
                "converse": {
                    "kind": "lmstudio",
                    "base_url": "http://127.0.0.1:1234",
                    "model": "already-loaded",
                }
            }
        },
        "converse",
    )
    assert isinstance(p, LMStudioProvider) and p.info.local
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from services.launcher import provider
provider({'providers':{'converse':{'kind':'lmstudio','base_url':'http://127.0.0.1:1234','model':'already-loaded'}}},'converse')
assert not {'torch','transformers','lmstudio','websockets'} & set(sys.modules)
""",
        ],
        check=True,
        timeout=5,
    )
