"""Native local adapter selection remains lazy and Converse-scoped."""

import subprocess
import sys

import pytest

from packages.providers.lmstudio import LMStudioProvider
from services.launcher import provider


@pytest.mark.parametrize("role", ["agent", "decisions"])
def test_native_lmstudio_profile_is_not_repurposed_for_other_components(role):
    config = {
        "providers": {
            role: {
                "kind": "lmstudio",
                "base_url": "http://127.0.0.1:1234",
                "model": "already-loaded",
            }
        }
    }
    with pytest.raises(SystemExit, match="Converse"):
        provider(config, role)


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
