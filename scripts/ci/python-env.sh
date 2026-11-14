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
# Upstream binaries target the hosted toolcache's absolute library directory.
# Relocate only this account's copy so isolated subprocesses load its matching
# libpython, even when they intentionally start with an empty environment.
if test ! -x "$HOME/.local/bin/patchelf"; then
  case "$arch" in
    arm64) elf_arch=aarch64; elf_digest=2281dd071995b449b9ce7c53c71d6d56ec3a280ae1cc66b779442abf624f7c10 ;;
    x64) elf_arch=x86_64; elf_digest=38861b171079e440b25ac86483284f690d0ee7921dba98b0c8fe04a9b0a11667 ;;
  esac
  elf_stage=$(mktemp -d "$HOME/tmp/patchelf.XXXXXX")
  curl -fsSL --retry 3 "https://github.com/NixOS/patchelf/releases/download/0.19.3/patchelf-0.19.3-$elf_arch.tar.gz" -o "$elf_stage/patchelf.tar.gz"
  printf '%s  %s\n' "$elf_digest" "$elf_stage/patchelf.tar.gz" | sha256sum -c -
  tar -xzf "$elf_stage/patchelf.tar.gz" -C "$elf_stage"
  install -m 0755 "$elf_stage/bin/patchelf" "$HOME/.local/bin/patchelf"
  test "$(dirname "$elf_stage")" = "$HOME/tmp"
  rm -rf -- "$elf_stage"
fi
"$HOME/.local/bin/patchelf" --set-rpath '$ORIGIN/../lib' "$prefix/bin/python$minor"
env -i PATH=/usr/bin:/bin "$prefix/bin/python$minor" -c 'import asyncio, fcntl; assert fcntl.F_SEAL_SEAL'
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
