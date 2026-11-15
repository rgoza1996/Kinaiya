# Private media bootstrap refusal observations (#620)

The first Python 3.14 failure in [CI run 43709052468](https://github.com/rgoza1996/Kinaiya/actions/runs/43709052468/job/132796437290)
returns `MediaProcessError` while starting the positive JPEG child. The original
subprocess has returned, but `_verify_bootstrap` has not established a leader.
The earlier generic error does not identify which existing identity predicate
refused. Later successful tests cannot explain that historical cause.

The media verifier now emits one best-effort, closed warning on ordinary
refusal: `media bootstrap refused: reason=%s`. The label is the first failing
existing predicate, in the original short-circuit order:

- `pre_stat_unavailable`, `pre_not_live`, `pre_parent_mismatch`,
  `pre_group_mismatch`, `pre_session_mismatch`, `pre_command_mismatch`;
- `pidfd_unavailable`;
- `post_stat_unavailable`, `pidfd_binding_mismatch`, `post_command_mismatch`,
  `post_not_live`, `post_parent_mismatch`, `post_group_mismatch`,
  `post_session_mismatch`, `post_starttime_mismatch`.

The two-value verifier result, native observations, read bounds, acceptance
predicates, original pidfd close attempt and exception propagation are retained.
Post-open refusal logging follows the original close attempt; it does not prove
that an uncertain close settled. Success stays silent. Diagnostic sink failures
cannot turn a refusal into a grant or replace an authority-operation exception.
The warning carries no process or descriptor identity, path, command, nonce,
payload, raw exception or traceback. The closed label describes an observation,
not a proven cause, a settled child or permission to retry.

No polling, fresh observation, timeout change, signal, process adoption,
decoder retry, source read or execution grant is added. In particular, this
diagnostic increment does **not** repair or qualify the historical CI failure.
Source/grant, decoder body/EOF and physical cleanup checks retain their separate
authority.

The guard/ordering/privacy oracles were frozen before implementation: the
unchanged code fails 18 diagnostic assertions and passes seven
authority/exception controls. A later import-only fixture re-export correction
keeps every test function/class AST unchanged. The resulting 25 tests verify
first-failure precedence, exact native read order, original close before warning,
success silence, authority exceptions, hostile diagnostic sinks, and actual
ungranted CPU-child refusal without target execution. Both original child
processes are reaped through their existing owners.

The project environment's initial decoder run skipped four cases because its
PyAV prerequisite was absent. That result remains a skip. A purpose-built
private runtime uses the exact PyAV 17.1.0 wheel/hash from
`requirements-whistle.lock`; the existing environments are unchanged. The four
actual PNG/JPEG point/positive cases then pass inside a no-network,
no-owner-home namespace with read-only source/runtimes and private writable
scratch. This Python 3.12 result is not a reproduction of the Python 3.14 CI
trigger or native runtime publication qualification. Original logs, runtime
hashes and exact namespace/pytest arguments are retained under local
`output/issue-620-media-decoder-ci/`.

The wider local checks pass 178 bootstrap/retirement tests and 30 actual
decoder-child/multitrack cases in the isolated pinned runtime. A bounded
differential inspection of 1,176 native-field combinations matches both the
original verifier outcomes and its ordered observations/closes. These checks
support preservation of the boundary; they are not a formal proof or a
reconstruction of the historical CI trigger.
