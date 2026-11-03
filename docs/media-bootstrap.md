# Private media bootstrap

Related: [media intake](media-intake-contract.md),
[issue #204](https://github.com/rgoza1996/Kinaiya/issues/204).

The trusted `packages/resources/_media_bootstrap.py` program is a dependency of
the proposed media worker lifecycle. It holds execution until the original
native parent grants it, then maps a private duplex control socket to FD3 and a
read-only source to FD4. It does not decode media or read the source body.

This program has no public service endpoint. Native Session admission, a
registered original source grant, descriptor-aware WorkerManager integration,
the sealed decoder sandbox, inner-process ownership and per-segment budgets
remain required before a decoder can run. The generic worker bootstrap and
existing public media protocols are unchanged.

## Inputs and refusal

The native-selected Python executable runs the absolute script with exactly
five arguments:

```text
<script> <grant_fd> <config_fd> <nonce> <control_fd> <source_fd>
```

The four descriptors must be distinct canonical decimal integers greater than
two. The nonce is exactly 32 lowercase hexadecimal characters. Malformed,
unsupported or out-of-range input exits with status 2 without target execution
or exception text.

The grant must be a readable pipe. An initial EOF exits with status 0 without
execution. Otherwise, exactly one `0x01` byte followed by EOF is required. A
trailing byte refuses execution; keeping the writer open cannot release the
target. Native startup deadlines and original process retirement remain the
parent's responsibility.

Configuration comes from a native-created sealed memfd, with seal, shrink,
grow and write seals present and a positive size at most 65,536 bytes. Before
seeking or reading configuration, the bootstrap rejects a source/config inode
alias. It seeks to zero, reads exactly the sealed size and requires EOF.
Configuration is strict UTF-8 JSON with no duplicate keys or nonfinite numbers,
and exactly these fields:

- `nonce`: the same nonce as the command line.
- `parent_pid`: the original positive integer parent PID.
- `argv`, `cwd`, `env`: fixed native execution configuration, using the existing
  generic bootstrap's bounds and validation.

Argument, directory and environment strings must also contain valid Unicode
scalar values; escaped lone surrogates are refused before filesystem use. The
tighter media configuration cap does not change the generic bootstrap's cap.

## Descriptor mapping and execution

The source must be a regular readable `O_RDONLY` file, excluding `O_PATH`.
Control must be a connected Unix stream socket. The bootstrap verifies its
original parent before and after arming the parent-death `SIGKILL` signal.

Both media descriptors are duplicated to fresh close-on-exec descriptors at
least 5 before any original is closed or overwritten. Only after all four
originals close successfully are those temporaries mapped to inheritable FD3
and FD4 and retired. This handles swapped source/control descriptors and
grant/config descriptors occupying the destination numbers. Any partial remap
or close failure exits before target execution. The bootstrap then changes to
the validated directory and executes the exact native-selected argv/environment.

The eventual media pool must restrict that target to its fixed sealed sandbox.
A read-only descriptor by itself does not prevent an unisolated process from
finding a writable alias. No shell, caller-selected descriptor transfer,
complete-source memory copy or failed-check fallback belongs to this interface.

## Evidence boundary

Disposable subprocess checks can establish this program's hold, refusal and
descriptor behavior. They do not establish original native registration,
complete process/publication settlement, a working decoder, supported model
transports, AT64–AT66 or the sign-language demonstration.

Parent-side descriptor closure, channel EOF, target acknowledgment, timeout and
generic outer-worker stop are not complete source-grant retirement. The full
lifecycle must retain the original source reader until its parent/child copies,
inner processes, I/O and publication owners have actually settled.
