# Worker bootstrap refusal diagnostics (#176)

The pre-grant worker verifier must retain enough bounded diagnostic information
to distinguish its existing rejection paths. CI231 at PR175 recorded an
`ownership-unverified` refusal before configured code or readiness ran. Earlier
equivalent coarse observations are retained under #9; their causes remain
unestablished. Later passing runs do not explain those failures.

Keep `_verify_bootstrap(pid, nonce, bootstrap) -> (pidfd, starttime)` and every
acceptance/rejection predicate, observation order, handle closure and grant
boundary unchanged. No new polling, retry, timeout, process adoption or signal
is authorized. The existing lifecycle error/result remains unchanged.

On rejection, best-effort warning logging uses the fixed format
`worker bootstrap refused: reason=%s errno=%s`. Closed reason labels are:

- `pre_stat_unavailable`, `pre_not_live`, `pre_parent_mismatch`,
  `pre_group_mismatch`, `pre_session_mismatch`, `pre_command_mismatch`;
- `pidfd_open_failed`;
- `post_observation_error`, `post_stat_unavailable`,
  `pidfd_binding_unavailable`, `pidfd_binding_mismatch`,
  `post_command_mismatch`;
- `post_not_live`, `post_parent_mismatch`, `post_group_mismatch`,
  `post_session_mismatch`, `post_starttime_mismatch`.

The first failing existing predicate determines the label. Post-open observation
order stays stat → fdinfo binding → command line. Any opened handle is closed
exactly as before, before refusal logging. No signal or grant follows refusal.

Only `pidfd_open_failed` may identify a kernel errno using a closed category:
EACCES `access_denied`, EPERM `operation_denied`, ESRCH `process_missing`,
EMFILE `process_fd_limit`, ENFILE `system_fd_limit`, ENOSYS `not_supported`,
EINVAL `invalid_argument`, ENOMEM `memory_unavailable`. Other integer errors
use `other`; missing/non-integer errno uses `not_reported`. Invalid diagnostic
reason input uses `unknown`. Never format the exception or unvalidated values.
Ordinary logging/diagnostic exceptions cannot change the tuple outcome.

No PID, descriptor, path, nonce, command, environment, config, raw exception or
traceback is added to this warning. These labels report an observation, not a
proven cause, foreign-process identity or authorization to retry.

Verification must cover every fixed reason, predicate/read ordering, accepted
tuples, one close after post-open refusal, unchanged pre-grant refusal, absent
payloads, logging failure and existing actual synthetic worker lifecycle and
boundary checks. This diagnostic slice does not fix or qualify general startup
reliability, the failing CI run, GPU handoff or the M6 roadmap gate.

## Verification mapping

- [Diagnostic fault tests](../tests/unit/test_worker_bootstrap_diagnostics.py)
  cover every reason and errno category, observation/close order, simultaneous
  faults, accepted tuples, hostile diagnostic values and logging failures.
- The unchanged [process lifecycle](../tests/integration/test_resource_processes.py),
  [ownership boundary](../tests/integration/test_resource_boundaries.py) and
  [parent/restart](../tests/integration/test_resource_parent.py) suites exercise
  actual synthetic workers. Related owner, handoff, identity and managed-worker
  fixtures retain their existing acceptance rules.
- Independent differential inspection compares the old and new verifier's
  outcomes and ordered native operations. This bounded comparison is not a
  formal proof or a reproduction of the CI failure.
- The [evidence record](../evaluation/evidence/worker-bootstrap-diagnostics-2026-10-31.json)
  pins the failed baseline, local source hashes, commands and limitations. CI
  must pass on the PR's tested source before merge; later passing checks cannot
  retroactively explain CI231.
