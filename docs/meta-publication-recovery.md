# Settled publication syntax rejection (#473)

The November 4 rehearsal completed actual media consultations and published a
private Intelligent UI before a later model proposal failed. Its native outcome
retained `ui_arguments/json_syntax`. The displayed error combines several
different rejection reasons; the older October 30 outcome lacks a reason and
does not establish the same cause. No original private payload is needed for
this diagnosis. Existing artifacts remain available, and the failed original
task is not retried, resumed or relabeled by this source change.

## Native admission and bounds

The JSON decoder and its 48 KiB UTF-8, duplicate-key, finite-number, closed-field
and component checks are unchanged. No substring extraction, trailing-text
removal, coercion or repaired JSON is executed. Only the prepared Meta flat-v1
path can emit `SettledPublicationRejection`, and only when all of these hold:

- The original request is measured and one-shot consumed, and advertised
  `publish_ui` in its immutable tool snapshot.
- HTTP response bytes reached EOF and the original owned response confirmed
  close. Cleanup errors and cancellation occur before this proof can escape.
- Meta explicitly reported `completed`. Missing/other statuses, HTTP errors and
  failed/incomplete responses cannot acquire this proof.
- The strict outer publication wrapper passed; only the inner logical JSON
  object failed with the exact closed reason `ui_arguments/json_syntax`.
- The proof's exact request SHA matches the consumed prepared measurement.
  After the generation await, the native supervisor rechecks source revisions,
  session epoch, current task/revision/provider, catalog, history, permissions
  and disclosure. It binds the notice to its own generation identity.

A generic, subclassed or mutated decoding exception remains terminal, as does
a proof supplied by any provider other than the exact native Meta adapter. The native supervisor
accepts at most one proven syntax rejection per objective revision. It consumes
that allowance and establishes a pending-publication fence before awaiting the
journal. The journal contains only a fixed diagnostic, task/revision/generation
identity, exact request digest, and `effect_executed: false`; it contains no
rejected keys, text, parser positions, response body or exception cause. No
approval, tool effect, artifact or work evidence is generated for the rejection.

The Agent receives a constant error notice in a standalone native history unit.
Its next step must pass ordinary fresh context counting, source checks, budget
and export admission. It cannot reuse the old request, prepared completion,
admission or model fallback. The existing maximum step count still applies.

Finish remains blocked until a later genuine `publish_ui` succeeds and its
native receipt is journaled in the same revision. Prior artifacts, an unrelated
read, a finish proposal, or another failed publication cannot clear that fence.
A second syntax rejection in the revision is terminal. An accepted owner steer
invalidates the old revision's pending fence along with its evidence; it grants
no completion in the new revision. Cancellation, revocation, stale sources and
uncertain cleanup retain their existing authority. The enrolled media workflow
completion guard and semantic acceptance remain separately required.

## Verification status

Root and independent full source reviews cleared this change. On November 4,
the existing pinned runtime passed:

```text
ruff check packages/providers/text.py services/agent/runtime.py tests/integration/test_meta_ui_actions.py tests/integration/test_meta_publication_recovery.py
ruff format --check packages/providers/text.py services/agent/runtime.py tests/integration/test_meta_ui_actions.py tests/integration/test_meta_publication_recovery.py
git diff --check
PYTHONPATH=. python -m pytest -q tests/integration/test_meta_publication_recovery.py tests/integration/test_meta_ui_actions.py
```

Ruff 0.17.3 and all static checks passed. The two modules passed 67 tests in
10.50 seconds on the first run; all 764 Python source/test inputs had identical
before/after hashes. This was the combined source with separately reviewed,
unactivated Wonder provider changes. The exact parser-only staged tree, based
on `4ec86e7` and excluding those changes, then passed the same 67 tests in
10.97 seconds plus Ruff check/format; all 762 standalone Python inputs remained
identical. These are two runs of the same 67 cases, not 134 distinct cases.
No live provider, task replay or activation occurred. Tests use MockTransport, a real native
SQLite journal/artifact store, held original response close, authenticated
source revisions, and ordinary native owner control APIs. They cover successful
correction with retained prior publication, fresh exact counting, one notice per
revision, repeated malformed proposals, generic/unmeasured/wrong proof, status
failures, unresolved finish after reads, cleanup, cancel, steer, source changes,
permission changes and reset. Existing unproven decoder-failure tests retain
their terminal queued-finish trap.

This change does not establish semantic correctness of an Agent-authored ASL
research/translation workflow, actual model reliability, browser usability or a successful demo. Those acceptance results
must come from a separately authorized fresh run and review.
