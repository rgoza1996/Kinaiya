"""Explicit compatibility for trusted runtimes predating model selection.

An absent old interface is distinct from a present but unavailable native model
owner. Never catch attribute errors from a declared hook and substitute a host.
"""

from contextlib import nullcontext
from inspect import getattr_static

from packages.providers.text import ProviderError

_ABSENT = object()
_MODEL_INTERFACE = (
    "model_settings",
    "_model_options_unavailable",
    "selected_provider_info",
    "model_preparation",
)


def legacy_model_embedding(runtime):
    return all(getattr_static(runtime, name, _ABSENT) is _ABSENT for name in _MODEL_INTERFACE)


def runtime_model_settings(runtime):
    if getattr_static(runtime, "model_settings", _ABSENT) is not _ABSENT:
        return runtime.model_settings
    if legacy_model_embedding(runtime):
        return None
    raise ProviderError("Converse model settings interface is unavailable")


def runtime_provider_info(runtime):
    if getattr_static(runtime, "selected_provider_info", _ABSENT) is not _ABSENT:
        return runtime.selected_provider_info()
    if legacy_model_embedding(runtime):
        return runtime.provider.info
    raise ProviderError("Converse model selection interface is unavailable")


def runtime_model_preparation(runtime):
    if getattr_static(runtime, "model_preparation", _ABSENT) is not _ABSENT:
        return runtime.model_preparation()
    if legacy_model_embedding(runtime):
        return nullcontext()
    raise ProviderError("Converse model preparation interface is unavailable")


def host_model_selected(runtime):
    if legacy_model_embedding(runtime):
        return True
    if getattr_static(runtime, "_model_options_unavailable", _ABSENT) is _ABSENT:
        raise ProviderError("Converse model availability interface is unavailable")
    if runtime._model_options_unavailable:
        return False
    settings = runtime_model_settings(runtime)
    return settings is None or settings.selection().source == "host"
