"""Bounded reads of protected registry and owner-suite files."""

import os
import stat
from pathlib import Path

from core.plugins.errors import PluginIntegrityError


def read_bytes(path: Path, maximum: int) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise PluginIntegrityError(
                    "Plugin storage requires regular files without hard links"
                )
            data = source.read(maximum + 1)
        if len(data) > maximum:
            raise PluginIntegrityError("Plugin storage exceeds its byte bound")
        return data
    except OSError as exc:
        raise PluginIntegrityError(f"Plugin storage unavailable: {exc}") from exc
