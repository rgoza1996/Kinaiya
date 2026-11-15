"""SDK fixture copies of declared application inputs; never build in source."""

import shutil
import subprocess
from pathlib import Path


def isolated_flutter_project(repository, destination):
    """Copy current tracked bytes, without private/ignored caches or SDK state."""
    repository, destination = Path(repository).resolve(), Path(destination).resolve()
    if destination.is_relative_to(repository):
        raise ValueError("Flutter fixture destination must be outside the checkout")
    prefix = Path("apps/kinaiya_flutter")
    application = (repository / prefix).resolve()
    names = (
        subprocess.check_output(["git", "ls-files", "-z", "--", prefix.as_posix()], cwd=repository)
        .decode()
        .split("\0")
    )
    files = []
    for name in filter(None, names):
        source = repository / name
        if (
            not application.is_relative_to(repository)
            or not source.is_file()
            or source.is_symlink()
            or not source.resolve().is_relative_to(application)
        ):
            raise ValueError("Flutter fixture requires regular declared application inputs")
        files.append((source, Path(name).relative_to(prefix)))
    if not any(relative == Path("pubspec.yaml") for _, relative in files):
        raise ValueError("Flutter fixture has no declared application manifest")
    destination.mkdir()
    for source, relative in files:
        copied = destination / relative
        copied.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, copied)
    return destination
