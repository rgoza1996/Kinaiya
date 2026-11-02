# Bounded bootstrap command and pre-grant exit observations (#278)

This refines the accepted [bootstrap diagnostics](worker-bootstrap-diagnostics.md)
for the unresolved startup investigation in #9. It does not repair startup or
establish a cause for the CI411 refusal in draft PR259. The original log records
`pre_command_mismatch`; its SHA256 is
`db2c22a3e827b9d9c1dbcd83341ce8382985e31c0fb8acd2b29c7265db9216ae`.

Preserve `_cmdline_has(pid, nonce, bootstrap) -> bool`, the verifier tuple,
every native observation and predicate in its existing short-circuit order,
read bounds, replacement decoding, descriptor closure, grant and cleanup.
There is exactly one bounded cmdline read for each existing helper call.
No additional read, polling, retry, delay, signal or execution grant is allowed.

The existing read may contribute only one closed detail per helper call:
`read_failed`, `empty_read`, `decode_failed`, `missing_nonce`,
`missing_bootstrap`, `missing_both`, or `matched`. Replacement decoding stays
unchanged: malformed UTF-8 alone is not a new rejection predicate. Empty read
is specifically zero bytes, not a list of empty NUL-separated tokens.

Use bounded per-verifier-invocation capture, isolated across reentrancy,
threads and async contexts. A diagnostic from a prior/direct helper call cannot
label a later verifier refusal. Do not use a global last-result slot. At most
the existing two helper outcomes may be retained; never retain bytes, tokens,
paths, exceptions, process identifiers or caller configuration. A helper called
outside a verifier remains a boolean helper with no new logging side effect.

Only a coarse `pre_command_mismatch` or `post_command_mismatch` may emit the
corresponding captured failure detail. Preserve the existing coarse warning
unchanged. A post-open detail warning must follow the existing descriptor
close, as the coarse warning does. Missing detail emits no invented observation.
Success stays silent. Other prior refusal predicates retain precedence even
when the later command read also fails.

The detail warning format is fixed:
`worker bootstrap command observation: result=%s`.

On actual verifier refusal, sample the original asyncio Process's cached
`returncode` immediately before the existing grant-writer close. Sample it
again after the existing unverified-child reap/pipe cleanup. Record only
`not_reported`, `exited_zero`, `exited_nonzero`, `terminated`, or `unavailable`.
These are cached observations, not new process probes. A post-close/reap exit
does not establish an exit before grant closure. No PID, descriptor, raw exit
number, argv, path, nonce, payload, exception text or traceback may be logged.
The exit warning format is fixed:
`worker bootstrap refusal exit: before_grant_close=%s after_reap_attempt=%s`.
`not_reported` means the watcher has not reported a code; it is not liveness
evidence. An unfinished reap attempt must not be described as completed reap.

Observation/capture/logger exceptions must not alter the preexisting boolean,
tuple, returned lifecycle error, grant decision, close sequence or exception
propagation from native authority operations. Do not catch a genuine native
authority exception merely to produce diagnostics. Keep original failure
uncertainty and cleanup owners. No extra signal, adoption or fresh closer.

Verification will freeze new tests before implementation and retain the old
diagnostic/lifecycle/boundary tests unchanged. Check single-read bounds,
constant/private output, original observation/close ordering, success silence,
reentrant/context isolation and hostile diagnostic sinks. Actual disposable
worker refusal checks must distinguish no configured execution, pre-close
cached status, later reap and original pipe/Task retirement. Synthetic fault
results are not a reproduction of the historical CI trigger. Independent
source review and applicable CI are required before merge. No inference,
model/GPU/device/live-service/configuration/preview operation or install is
part of this diagnostic slice.
