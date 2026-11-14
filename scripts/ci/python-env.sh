#!/bin/bash
# Pinned upstream Linux builds retain the real fcntl/memfd sealing facilities
# needed by Agent. Portable uv-managed builds failed that prerequisite (#571).
set -euo pipefail
test "$(id -un)" = kinaiya-ci
minor=${1:?Python minor required}
venv=${2:?Job virtualenv path required}
case "$(uname -m)" in aarch64) arch=arm64 ;; x86_64) arch=x64 ;; *) exit 1 ;; esac
source_dir=$(cd -- "$(dirname -- "$0")" && pwd)
read -r version release digest < <(python3 - "$source_dir/python-builds.json" "$minor" "$arch" <<'PY'
import json, sys
build = json.load(open(sys.argv[1]))[sys.argv[2]]
print(build["version"], build["release"], build["sha256"][sys.argv[3]])
PY
)
test -n "$version" && test -n "$digest"
prefix="$HOME/toolchains/python-$version"
if test ! -d "$prefix"; then
  mkdir -p "$HOME/toolchains" "$HOME/tmp"
  stage=$(mktemp -d "$HOME/tmp/cpython.XXXXXX")
  trap 'rm -rf -- "$stage"' EXIT
  curl -fsSL --retry 3 "https://github.com/actions/python-versions/releases/download/$release/python-$version-linux-24.04-$arch.tar.gz" -o "$stage/python.tar.gz"
  printf '%s  %s\n' "$digest" "$stage/python.tar.gz" | sha256sum -c -
  mkdir "$prefix"
  tar -xzf "$stage/python.tar.gz" -C "$prefix" --no-same-owner
  printf '%s\n' "$digest" > "$prefix/.ci-archive-sha256"
fi
test "$(cat "$prefix/.ci-archive-sha256")" = "$digest"
"$prefix/bin/python$minor" - <<'PY'
import fcntl
import os

fd = os.memfd_create("ci-sealing-probe", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
try:
    os.write(fd, b"probe")
    seals = fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE
    fcntl.fcntl(fd, fcntl.F_ADD_SEALS, seals)
    assert fcntl.fcntl(fd, fcntl.F_GET_SEALS) & seals == seals
    try:
        os.write(fd, b"rejected")
    except PermissionError:
        pass
    else:
        raise RuntimeError("Kernel did not enforce sealed input")
finally:
    os.close(fd)
PY
uv venv --seed --no-managed-python --python "$prefix/bin/python$minor" "$venv"
