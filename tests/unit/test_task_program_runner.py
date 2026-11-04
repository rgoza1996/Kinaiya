"""Native immutable task-program execution; no external/model requests."""

import asyncio
import fcntl
import hashlib
import os
import shlex
import stat

import pytest

from core.policy import ApprovalError, PolicyDenied, ToolPolicy
from core.policy.tool_admission import native_tool_admission
from services.agent import tools


def sha(value):
    return hashlib.sha256(value).hexdigest()


@pytest.fixture
def opened(tmp_path):
    program, source = tmp_path / "program.py", tmp_path / "input.bin"
    program.write_bytes(b"print('public synthetic program')\n")
    source.write_bytes(b"public synthetic input\x00\xff")
    descriptors = [os.open(path, os.O_RDONLY | os.O_NOFOLLOW) for path in (program, source)]
    yield program, source, descriptors
    for descriptor in descriptors:
        os.close(descriptor)


class Process:
    pid = 424242

    def __init__(self):
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(b"public synthetic output\n")
        self.stdout.feed_eof()
        self.returncode = None

    async def wait(self):
        self.returncode = 0
        return 0


async def invoke(opened, policy, **changes):
    program, source, (program_fd, input_fd) = opened
    values = {
        "input_fd": input_fd,
        "source_sha256": sha(program.read_bytes()),
        "input_sha256": sha(source.read_bytes()),
    }
    values.update(changes)
    return await tools.run_program(
        policy,
        program_fd,
        "python3",
        ["/kinaiya-input", "/workspace/result.txt"],
        5,
        1,
        **values,
    )


@pytest.mark.parametrize(
    "interpreter,prefix",
    [
        ("python3", ["exec", "/usr/bin/python3", "-B", "/kinaiya-program"]),
        ("bash", ["exec", "/bin/bash", "--noprofile", "--norc", "/kinaiya-program"]),
        ("direct", ["exec", "/kinaiya-program"]),
    ],
)
def test_command_preserves_every_argument_as_data(interpreter, prefix):
    arguments = [
        "",
        "two words",
        "$(touch /workspace/forbidden)",
        "'quoted'",
        "; false",
        "a\nb",
        "é",
    ]
    assert shlex.split(tools.task_program_command(interpreter, arguments)) == prefix + arguments


@pytest.mark.parametrize(
    "interpreter,arguments",
    [
        (None, []),
        (True, []),
        ("python", []),
        ("/bin/bash", []),
        ("direct", None),
        ("python3", ("tuple",)),
        ("python3", [True]),
        ("python3", ["nul\0"]),
        ("python3", ["\ud800"]),
        ("python3", ["a" * 4097]),
        ("python3", ["é" * 2049]),
        ("python3", [""] * 65),
        ("python3", ["a" * 4096] * 5),
    ],
)
def test_command_rejects_unknown_interpreter_and_unbounded_arguments(interpreter, arguments):
    with pytest.raises(tools.ToolFailure):
        tools.task_program_command(interpreter, arguments)


def test_command_accepts_exact_argument_bounds():
    assert len(shlex.split(tools.task_program_command("direct", ["a" * 4096] * 4))) == 6


def test_snapshot_is_sealed_exact_and_does_not_move_or_close_caller_fd(opened):
    program, _, (descriptor, _) = opened
    original = program.read_bytes()
    os.lseek(descriptor, 3, os.SEEK_SET)
    with tools._sealed_program_snapshot(descriptor, sha(original), len(original)) as snapshot:
        assert os.pread(snapshot, len(original) + 1, 0) == original
        assert os.lseek(snapshot, 0, os.SEEK_CUR) == 0
        assert os.lseek(descriptor, 0, os.SEEK_CUR) == 3
        assert (
            fcntl.fcntl(snapshot, fcntl.F_GET_SEALS) & tools._PROGRAM_SEALS == tools._PROGRAM_SEALS
        )
        for operation in (
            lambda: os.pwrite(snapshot, b"x", 0),
            lambda: os.ftruncate(snapshot, 0),
            lambda: os.ftruncate(snapshot, len(original) + 1),
        ):
            with pytest.raises(OSError):
                operation()
        program.write_bytes(b"changed original pathname")
        assert os.pread(snapshot, len(original) + 1, 0) == original
    with pytest.raises(OSError):
        os.fstat(snapshot)
    assert os.fstat(descriptor).st_size == len(b"changed original pathname")


@pytest.mark.parametrize(
    "damage", ["digest", "empty", "too_large", "directory", "closed", "bool_fd"]
)
def test_snapshot_refuses_invalid_raw_fd_or_bytes(opened, tmp_path, damage):
    program, _, (descriptor, _) = opened
    expected, maximum = sha(program.read_bytes()), tools.PROGRAM_MAX_BYTES
    extra = None
    if damage == "digest":
        expected = "0" * 64
    elif damage == "empty":
        program.write_bytes(b"")
    elif damage == "too_large":
        maximum = program.stat().st_size - 1
    elif damage == "directory":
        extra = descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    elif damage == "closed":
        descriptor = os.dup(descriptor)
        os.close(descriptor)
    else:
        descriptor = True
    try:
        with pytest.raises(tools.ToolFailure):
            with tools._sealed_program_snapshot(descriptor, expected, maximum):
                pytest.fail("Invalid snapshot was admitted")
    finally:
        if extra is not None:
            os.close(extra)


def test_snapshot_refuses_same_length_mutation_during_copy(opened, monkeypatch):
    program, _, (descriptor, _) = opened
    original = program.read_bytes()
    real_pread = os.pread

    def mutate(fd, count, offset):
        value = real_pread(fd, count, offset)
        program.write_bytes(b"x" * len(original))
        return value

    monkeypatch.setattr(tools.os, "pread", mutate)
    with pytest.raises(tools.ToolFailure, match="changed"):
        with tools._sealed_program_snapshot(descriptor, sha(original), len(original)):
            pytest.fail("Changing source was admitted")


def test_direct_requires_executable_original_and_preserves_execute_permission(opened):
    program, _, (descriptor, _) = opened
    original = program.read_bytes()
    program.chmod(0o600)
    with pytest.raises(tools.ToolFailure):
        with tools._sealed_program_snapshot(
            descriptor, sha(original), len(original), executable=True
        ):
            pytest.fail("Non-executable binary was admitted")
    program.chmod(0o700)
    with tools._sealed_program_snapshot(
        descriptor, sha(original), len(original), executable=True
    ) as snapshot:
        assert stat.S_IMODE(os.fstat(snapshot).st_mode) == 0o500


async def test_runner_reuses_shell_exact_command_admission_mounts_evidence_and_custody(
    opened, tmp_path, monkeypatch
):
    program, source, caller_fds = opened
    expected_program, expected_input = program.read_bytes(), source.read_bytes()
    launched = []
    monkeypatch.setattr(tools, "bwrap_path", lambda: "/usr/bin/bwrap")

    async def spawn(*argv, **kwargs):
        descriptors = kwargs["pass_fds"]
        assert len(descriptors) == 2 and not set(descriptors) & set(caller_fds)
        assert [os.pread(fd, 65536, 0) for fd in descriptors] == [expected_program, expected_input]
        for descriptor, destination in zip(
            descriptors, ("/kinaiya-program", "/kinaiya-input"), strict=True
        ):
            position = argv.index(destination)
            assert argv[position - 4 : position + 1] == (
                "--perms",
                "0400",
                "--ro-bind-data",
                str(descriptor),
                destination,
            )
        assert kwargs["start_new_session"] is True
        assert argv[-3:] == ("/bin/bash", "-c", command)
        assert "--unshare-net" in argv
        launched.extend(descriptors)
        return Process()

    monkeypatch.setattr(tools.asyncio, "create_subprocess_exec", spawn)
    command = tools.task_program_command("python3", ["/kinaiya-input", "/workspace/result.txt"])
    arguments = {"command": command, "timeout_s": 5, "objective_revision": 1}
    policy, slot = ToolPolicy(tmp_path), {}
    digest = policy.digest_for("shell", arguments, 1)
    with native_tool_admission(
        policy=policy,
        tool="shell",
        arguments=arguments,
        broker_arguments=arguments,
        digest=digest,
        revision=1,
        decision={
            "effective": "allow",
            "source": "host",
            "source_scope_id": None,
            "rule_id": None,
            "rule_revision": 0,
        },
        current=lambda: None,
        approval_id=None,
        reviewed=False,
        speech_review=False,
    ) as admission:
        output, evidence = await invoke(opened, policy, digest=digest, proc_slot=slot)
    assert admission.consumed is True
    assert output == "public synthetic output\n"
    assert evidence.kind == "command" and evidence.exit_code == 0
    assert evidence.objective_revision == 1 and slot["proc"] is None
    for descriptor in launched:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    assert all(os.fstat(fd).st_size > 0 for fd in caller_fds)


@pytest.mark.parametrize("denied", [False, True])
async def test_runner_never_bypasses_shell_approval_or_hard_denial(
    opened, tmp_path, monkeypatch, denied
):
    async def forbidden(*_, **__):
        pytest.fail("Unapproved execution reached spawn")

    monkeypatch.setattr(tools.asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(PolicyDenied if denied else ApprovalError):
        await invoke(opened, ToolPolicy(tmp_path, deny_shell=denied))


async def test_input_digest_mismatch_never_reaches_shell(opened, tmp_path, monkeypatch):
    async def forbidden(*_, **__):
        pytest.fail("Wrong input reached shell")

    monkeypatch.setattr(tools, "run_shell", forbidden)
    with pytest.raises(tools.ToolFailure, match="changed"):
        await invoke(opened, ToolPolicy(tmp_path), input_sha256="0" * 64)


async def test_spawn_failure_closes_native_snapshots_but_preserves_caller_fds(
    opened, tmp_path, monkeypatch
):
    snapshots = []
    monkeypatch.setattr(tools, "bwrap_path", lambda: "/usr/bin/bwrap")

    async def fail(*_, **kwargs):
        snapshots.extend(kwargs["pass_fds"])
        raise OSError("synthetic spawn failure")

    monkeypatch.setattr(tools.asyncio, "create_subprocess_exec", fail)
    with pytest.raises(tools.ToolFailure, match="snapshot is unavailable"):
        await invoke(opened, ToolPolicy(tmp_path, allow_shell=True))
    assert len(snapshots) == 2
    for descriptor in snapshots:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    assert all(os.fstat(fd).st_size > 0 for fd in opened[2])


@pytest.mark.parametrize("settled", [False, True])
async def test_cancellation_keeps_actual_shell_owner_until_reaped(
    opened, tmp_path, monkeypatch, settled
):
    started, release = asyncio.Event(), asyncio.Event()
    slot, snapshots, killed = {}, [], []

    class HeldProcess:
        pid = 424242
        returncode = None

        def __init__(self):
            self.stdout = asyncio.StreamReader()

        async def wait(self):
            await release.wait()
            self.returncode = -9
            return -9

    process = HeldProcess()
    monkeypatch.setattr(tools, "bwrap_path", lambda: "/usr/bin/bwrap")

    async def spawn(*_, **kwargs):
        snapshots.extend(kwargs["pass_fds"])
        started.set()
        return process

    async def kill(handle):
        assert slot["proc"] is handle and handle._proc is process
        killed.append(handle)
        if settled:
            release.set()
            await process.wait()

    monkeypatch.setattr(tools.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(tools.RunningProc, "kill", kill)
    task = asyncio.create_task(
        invoke(opened, ToolPolicy(tmp_path, allow_shell=True), proc_slot=slot)
    )
    try:
        await asyncio.wait_for(started.wait(), 2)
        async with asyncio.timeout(2):
            while "proc" not in slot:
                await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert len(killed) == 1
        if settled:
            assert slot["proc"] is None
        else:
            assert slot["proc"] is killed[0] and process.returncode is None
            cleanup = slot["cleanup"]
            release.set()
            await asyncio.wait_for(cleanup, 2)
            await asyncio.sleep(0)
            assert slot["proc"] is None and "cleanup" not in slot
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
    for descriptor in snapshots:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_native_mount_hook_refuses_unsealed_fds(opened, tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "bwrap_path", lambda: "/usr/bin/bwrap")
    with pytest.raises((tools.ToolFailure, OSError)):
        tools.build_bwrap_args(ToolPolicy(tmp_path), "true", False, _program_fds=tuple(opened[2]))


@pytest.mark.parametrize("moved", [False, True])
def test_data_mount_preserves_direct_execute_mode_and_requires_initial_offset(
    opened, tmp_path, monkeypatch, moved
):
    program, source, (program_fd, input_fd) = opened
    program.chmod(0o700)
    monkeypatch.setattr(tools, "bwrap_path", lambda: "/usr/bin/bwrap")
    with tools._sealed_program_snapshot(
        program_fd, sha(program.read_bytes()), tools.PROGRAM_MAX_BYTES, executable=True
    ) as entrypoint:
        with tools._sealed_program_snapshot(
            input_fd, sha(source.read_bytes()), tools.PROGRAM_INPUT_MAX_BYTES
        ) as source_snapshot:
            if moved:
                os.lseek(entrypoint, 1, os.SEEK_SET)
                with pytest.raises(tools.ToolFailure, match="offset changed"):
                    tools.build_bwrap_args(
                        ToolPolicy(tmp_path),
                        "true",
                        False,
                        _program_fds=(entrypoint, source_snapshot),
                    )
                return
            argv, _ = tools.build_bwrap_args(
                ToolPolicy(tmp_path), "true", False, _program_fds=(entrypoint, source_snapshot)
            )
            for fd, destination, mode in (
                (entrypoint, "/kinaiya-program", "0500"),
                (source_snapshot, "/kinaiya-input", "0400"),
            ):
                position = argv.index(destination)
                assert argv[position - 4 : position + 1] == [
                    "--perms",
                    mode,
                    "--ro-bind-data",
                    str(fd),
                    destination,
                ]


def network_files(tmp_path, monkeypatch):
    resolv, certs = tmp_path / "resolv.conf", tmp_path / "certs"
    certs.mkdir()
    bundle = certs / "ca-certificates.crt"
    resolv.write_text("nameserver 192.0.2.1\n")
    bundle.write_text("PUBLIC SYNTHETIC CA FIXTURE\n")
    monkeypatch.setattr(tools, "bwrap_path", lambda: "/usr/bin/bwrap")
    monkeypatch.setattr(tools, "_NETWORK_RESOLV", resolv)
    monkeypatch.setattr(tools, "_NETWORK_CA_DIRECTORY", certs)
    monkeypatch.setattr(tools, "_NETWORK_CA_BUNDLE", bundle)
    return resolv, certs, bundle


@pytest.mark.parametrize("network", [False, True])
def test_dns_ca_mounts_exist_only_with_explicit_whole_host_network_grant(
    tmp_path, monkeypatch, network
):
    resolv, certs, bundle = network_files(tmp_path, monkeypatch)
    argv, isolated = tools.build_bwrap_args(ToolPolicy(tmp_path), "true", network)
    assert isolated is not network
    assert ("--unshare-net" in argv) is not network
    for source, destination in (
        (resolv, "/etc/resolv.conf"),
        (certs, "/etc/ssl/certs"),
        (bundle, "/etc/ssl/certs/ca-certificates.crt"),
    ):
        if network:
            index = argv.index(destination)
            assert argv[index - 2 : index + 1] == ["--ro-bind", str(source), destination]
        else:
            assert destination not in argv
    assert "/etc" not in argv and "/etc/ssl/private" not in argv


@pytest.mark.parametrize("missing", [0, 1, 2])
def test_networked_shell_fails_closed_without_exact_dns_ca_runtime(tmp_path, monkeypatch, missing):
    paths = network_files(tmp_path, monkeypatch)
    if missing == 1:
        paths[2].unlink()
        paths[1].rmdir()
    else:
        paths[missing].unlink()
    with pytest.raises(tools.ShellUnavailable):
        tools.build_bwrap_args(ToolPolicy(tmp_path), "true", True)
    assert tools.build_bwrap_args(ToolPolicy(tmp_path), "true", False)[1] is True


@pytest.mark.sandbox
async def test_actual_sandbox_executes_exact_native_snapshots_with_binary_input(tmp_path):
    policy = ToolPolicy(tmp_path, allow_shell=True)
    try:
        await tools.run_shell(policy, "true", 5, 1)
    except tools.ToolFailure as error:
        pytest.skip(f"Basic OS sandbox unavailable: {error}")
    program = tmp_path / "program.py"
    source = tmp_path / "input.bin"
    body = (
        b"import pathlib, sys\n"
        b"assert sys.argv[1] == '/kinaiya-input'\n"
        b"assert pathlib.Path(sys.argv[1]).read_bytes() == b'public\\x00\\xff'\n"
        b"assert pathlib.Path('/kinaiya-program').read_bytes().startswith(b'import pathlib')\n"
        b"assert all(int(fd) <= 3 for fd in pathlib.Path('/proc/self/fd').iterdir() for fd in [fd.name])\n"
        b"pathlib.Path(sys.argv[2]).write_bytes(b'public result')\n"
        b"print('TASK_PROGRAM_RUNNER_PROOF_OK')\n"
    )
    program.write_bytes(body)
    source.write_bytes(b"public\x00\xff")
    program_fd = os.open(program, os.O_RDONLY | os.O_NOFOLLOW)
    input_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    slot = {}
    try:
        output, evidence = await tools.run_program(
            policy,
            program_fd,
            "python3",
            ["/kinaiya-input", "/workspace/result.bin"],
            5,
            1,
            input_fd=input_fd,
            source_sha256=sha(body),
            input_sha256=sha(b"public\x00\xff"),
            proc_slot=slot,
        )
        assert evidence.exit_code == 0 and evidence.kind == "command"
        assert "TASK_PROGRAM_RUNNER_PROOF_OK" in output
        assert (tmp_path / "result.bin").read_bytes() == b"public result"
        assert program.read_bytes() == body and source.read_bytes() == b"public\x00\xff"
        assert slot["proc"] is None
    finally:
        os.close(program_fd)
        os.close(input_fd)


@pytest.mark.sandbox
async def test_actual_sandbox_executes_pinned_standard_binary(tmp_path):
    policy = ToolPolicy(tmp_path, allow_shell=True)
    try:
        await tools.run_shell(policy, "true", 5, 1)
    except tools.ToolFailure as error:
        pytest.skip(f"Basic OS sandbox unavailable: {error}")
    program, source = tmp_path / "public-true", tmp_path / "input.bin"
    # This standard system ELF supplies no task-specific implementation.
    with open("/usr/bin/true", "rb") as executable:
        body = executable.read(tools.PROGRAM_MAX_BYTES + 1)
    assert 1 <= len(body) <= tools.PROGRAM_MAX_BYTES
    program.write_bytes(body)
    program.chmod(0o700)
    source.write_bytes(b"public input")
    program_fd = os.open(program, os.O_RDONLY | os.O_NOFOLLOW)
    input_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    slot = {}
    try:
        output, evidence = await tools.run_program(
            policy,
            program_fd,
            "direct",
            [],
            5,
            1,
            input_fd=input_fd,
            source_sha256=sha(body),
            input_sha256=sha(b"public input"),
            proc_slot=slot,
        )
        assert output == "" and evidence.exit_code == 0 and evidence.kind == "command"
        assert slot["proc"] is None
        assert program.read_bytes() == body and source.read_bytes() == b"public input"
    finally:
        os.close(program_fd)
        os.close(input_fd)
