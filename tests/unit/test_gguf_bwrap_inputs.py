"""Real no-op bwrap startup: tiny fixture ELF/DSO, synthetic bytes, no model."""

import hashlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from packages.resources.processes import _pidfd_pid, _scan_group

_PAYLOAD = r"""
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <unistd.h>
extern int fixture_value(void);
int main(void) {
    const char *paths[] = {"/model.gguf", "/runtime/llama-server", "/vendor/libfixture.so"};
    int fds[3], seals = F_SEAL_WRITE|F_SEAL_GROW|F_SEAL_SHRINK|F_SEAL_SEAL;
    signal(SIGXFSZ, SIG_IGN);
    for (int i=0; i<3; i++) {
        char target[128]; struct stat original, guest;
        ssize_t n=readlink(paths[i], target, sizeof(target)-1);
        if (n<0) return 10+i;
        target[n]=0;
        if (strncmp(target,"/proc/self/fd/",14)) return 20+i;
        fds[i]=atoi(target+14);
        if (fstat(fds[i],&original)||stat(paths[i],&guest)) return 30+i;
        if (original.st_dev!=guest.st_dev||original.st_ino!=guest.st_ino) return 40+i;
        if ((fcntl(fds[i],F_GET_SEALS)&seals)!=seals) return 50+i;
        errno=0;
        if (pwrite(fds[i],"!",1,0)!=-1 || (errno!=EPERM && errno!=EFBIG)) return 60+i;
    }
    struct stat original, executable;
    if (fstat(fds[1],&original)||stat("/proc/self/exe",&executable)) return 70;
    if (original.st_dev!=executable.st_dev||original.st_ino!=executable.st_ino) return 71;
    FILE *input=fopen(paths[0],"rb"); char raw[64]={0};
    if (!input||fread(raw,1,sizeof(raw),input)!=21) return 72;
    fclose(input);
    if (memcmp(raw,"public-synthetic-only",21)||fixture_value()!=42) return 73;
    DIR *directory=opendir("/proc/self/fd"); struct dirent *entry;
    if (!directory) return 74;
    int unexpected=0;
    while ((entry=readdir(directory))) {
        int fd=atoi(entry->d_name);
        if (fd>2 && fd!=dirfd(directory) && fd!=fds[0] && fd!=fds[1] && fd!=fds[2]) unexpected++;
    }
    closedir(directory);
    if (unexpected) return 75;
    struct rlimit size;
    if (getrlimit(RLIMIT_FSIZE,&size)||size.rlim_cur!=0||size.rlim_max!=0) return 76;
    puts("{\"sealed_original_executable\":true,\"sealed_fds\":3,\"guest_inode_matches\":true,\"writes_refused\":true,\"shared_value\":42,\"unexpected_fds\":0,\"fsize_zero\":true}");
    return 0;
}
"""


def _run_owned(argv, *, cwd, timeout=10):
    """Only this newly spawned session is eligible for timeout cleanup."""
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env={
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    pidfd = os.pidfd_open(process.pid)
    try:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            if _pidfd_pid(pidfd) == process.pid:
                os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate(timeout=5)
            pytest.fail(f"Owned no-op child timed out: {stderr[:1024]!r}")
        assert _pidfd_pid(pidfd) == -1
        members, scanned = _scan_group(process.pid)
        assert scanned and not members
        return process.returncode, stdout, stderr
    finally:
        os.close(pidfd)


def test_actual_worker_preserves_sealed_inputs_through_bwrap_and_dynamic_loader(tmp_path):
    if not Path("/usr/bin/bwrap").exists() or not Path("/usr/bin/cc").exists():
        pytest.skip("Local no-op proof requires installed bwrap and C compiler")
    owner = tmp_path / "owner"
    owner.mkdir(mode=0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    socket_dir = tmp_path / "socket"
    socket_dir.mkdir(mode=0o700)
    library = owner / "libfixture.so"
    library_source = owner / "fixture.c"
    library_source.write_text("int fixture_value(void) { return 42; }\n")
    server = owner / "llama-server"
    source = owner / "payload.c"
    source.write_text(_PAYLOAD)
    assert (
        _run_owned(
            [
                "/usr/bin/cc",
                "-shared",
                "-fPIC",
                "-Wl,-soname,libfixture.so",
                str(library_source),
                "-o",
                str(library),
            ],
            cwd=owner,
        )[0]
        == 0
    )
    assert (
        _run_owned(
            ["/usr/bin/cc", str(source), "-L", str(owner), "-lfixture", "-o", str(server)],
            cwd=owner,
        )[0]
        == 0
    )
    model = owner / "public.bin"
    model.write_bytes(b"public-synthetic-only")
    assets = []
    for path, guest in [
        (model, "/model.gguf"),
        (server, "/runtime/llama-server"),
        (library, "/vendor/libfixture.so"),
    ]:
        raw = path.read_bytes()
        assets.append(
            {
                "path": str(path),
                "guest": guest,
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    manifest = {
        "version": 1,
        "profile": "synthetic-cpu-noop",
        "model": "synthetic",
        "alias": "synthetic-noop",
        "assets": assets,
        "sampling": {
            "temperature": 0.0,
            "top_p": 1.0,
            "top_k": 20,
            "min_p": 0.0,
            "presence_penalty": 0.0,
            "repeat_penalty": 1.0,
        },
    }
    path = owner / "manifest.json"
    raw = json.dumps(manifest).encode()
    path.write_bytes(raw)
    code, stdout, stderr = _run_owned(
        [
            sys.executable,
            "-m",
            "packages.providers.gguf_worker",
            "--manifest",
            str(path),
            "--manifest-sha256",
            hashlib.sha256(raw).hexdigest(),
            "--socket-dir",
            str(socket_dir),
            "--workspace",
            str(workspace),
        ],
        cwd=Path(__file__).resolve().parents[2],
    )
    assert code == 0, stderr.decode(errors="replace")
    assert json.loads(stdout) == {
        "sealed_original_executable": True,
        "sealed_fds": 3,
        "guest_inode_matches": True,
        "writes_refused": True,
        "shared_value": 42,
        "unexpected_fds": 0,
        "fsize_zero": True,
    }
    assert json.loads((socket_dir / "phase.json").read_text())["phase"] == "exec_pending"
