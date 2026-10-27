"""Owner-controlled local worker lifecycle (GitHub #9, Roadmap 14.2-14.4).

Managed subprocesses defined solely by trusted owner configuration. Lazy
imports keep this package free of import-time side effects.
"""

from __future__ import annotations

from typing import Any

__all__ = ["MAX_WORKERS", "TAIL_MAX_BYTES", "WorkerManager", "WorkerSpec"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from packages.resources import processes

        return getattr(processes, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
