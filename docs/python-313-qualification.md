# Python 3.13 qualification (#624)

This record begins with the additive stage of
[#624](https://github.com/rgoza1996/Kinaiya/issues/624). At that stage CI
exercised 3.11, 3.12, 3.13 and 3.14 with the same complete non-live suite and
accepted 3.13 in manual full/media runs. The release retained its 3.11
minimum while the native builds were under qualification.

## Native build provenance

The official [actions/python-versions release](https://github.com/actions/python-versions/releases/tag/3.13.18-40336726450)
is `3.13.18-40336726450`, published October 26, 2026. The observed
[versions manifest](https://github.com/actions/python-versions/blob/main/versions-manifest.json)
Git blob is `8468b3406cb309a299b2ead4109d0a2b2467153b`; release ID is
`424354990`. This selects ordinary CPython, not the separate free-threaded ABI.

| Ubuntu 24.04 archive | SHA-256 |
| --- | --- |
| `python-3.13.18-linux-24.04-arm64.tar.gz` | `8336b5b058474b8967155a24a5dd164cd6fa14866c009cce599f751e878dfc71` |
| `python-3.13.18-linux-24.04-x64.tar.gz` | `683005a1465d94d40045f6a1a6eb316885b5f08d3360f7922b44ef58a14a9eed` |

Both published asset digests agree with the downloaded `hashes.sha256`
(SHA-256 `9011ff8e558cd9c5373a3b814778e67b920ab8c1317a170fe1ce4161361aab1a`).
The x64 archive was also downloaded and hashed locally. Oracle subsequently
executed the pinned ARM64 build and passed 16,157 tests on Python 3.13.
[Full native job on `71d797c5`](https://github.com/rgoza1996/Kinaiya/actions/runs/44211402907/job/134490417868)

The existing CI helper verifies each archive, relocates only its private
interpreter using the pinned patchelf, imports with an empty environment and
checks real kernel-enforced memfd seals. Its account guard and all host policy
remain byte-identical. No reviewed-ref exception is added: native qualification
uses the existing main-only policy after the additive lane has landed normally.

## Local evidence so far

Baseline `548f85d84874b2b0d3366ffdb36f8c74aa880ae8`, Ubuntu 24.04.5 x64,
glibc 2.39. All qualification files are private task artifacts under local
`output/issue-624-python313/`; no installed owner runtime is promoted.

- The private 3.13.18 interpreter passes empty-environment imports and an actual
  write-refused sealed-memfd probe. `SOABI` is
  `cpython-313-x86_64-linux-gnu`, `Py_GIL_DISABLED=0`.
- The unchanged development lock installs with `pip --require-hashes`, editable
  package installation succeeds, and `pip check` reports no broken requirements.
  PyAV 17.1.0 and NumPy 2.3.3 import. A real bubblewrap/seccomp prerequisite
  invocation exits zero.
- A 1,510,340-byte `py3-none-any` wheel builds on 3.13; all 364 hashed RECORD
  members verify. It installs with the unchanged hashed binary-only runtime
  closure into a second clean environment. Imports resolve to that environment's
  site-packages outside the checkout, `pip check` passes, and CLI help runs.
  That wheel declared `Requires-Python: >=3.11`.
- Runtime and Supertonic locks resolve under 3.13 with hash enforcement and
  binary-only wheels, without changing the running test environment. This is
  wheel-resolution evidence, not inference or full optional-worker execution.
- Ruff check/format (1,040 files), unchanged schema export and dependency audit
  pass on the new interpreter. No known vulnerabilities are reported for the
  unchanged development lock.

[Local qualification receipts](evidence/python-313-qualification-2026-11-15/local-preflight.json)
retain artifact/source hashes and distinguish completed probes from unresolved
full-suite and platform acceptance.

The first full run was stopped deliberately after discovering inherited Conda
subprocess lookup. It is incomplete and not counted as a pass. The verified
original pytest process alone received SIGINT; its KeyboardInterrupt/pytest
teardown KeyError and all 834 unchanged input hashes remain recorded. The
replacement run asserts that both `python` and `python3` resolve to the isolated
3.13.18 executable before running the complete non-live suite. It completed with
14,993 passed, one failed, 49 live deselected and two subtests passed in 3,269.73
seconds, with no skips. All 834 Python input hashes match before and after. The
start receipt identifies baseline `548f85d8`; the final receipt identifies the
metadata-only additive commit `47f43ff6`, with identical Python inputs.

The failure is the initial `/v1/standing/dispatch` GET returning 503 while setting
up `test_snapshot_keeps_original_context_with_release_only_control`, before its
target timeout scenario. This matches the observable setup boundary tracked in
[#639](https://github.com/rgoza1996/Kinaiya/issues/639), previously seen on hosted
3.14. Its underlying cause remains unproven; it is not a passing qualification.

Hosted run [43758372160](https://github.com/rgoza1996/Kinaiya/actions/runs/43758372160)
on exact additive head `47f43ff6969c9e6bb834e085401e9629b759c454` has seven other
standard jobs passing and the added 3.13 lane failing: 14,984 passed, one failed,
nine skipped, 49 live deselected and two subtests passed in 2,005.37 seconds.
The positive original-process slot-reuse case refuses `pre_command_mismatch`;
the same boundary is tracked in [#620](https://github.com/rgoza1996/Kinaiya/issues/620).
This does not establish the same cause as that issue's earlier JPEG failure.
Four manual-only jobs were skipped. Both first failure logs are retained.

The subsequent documentation-head run
[43765825209](https://github.com/rgoza1996/Kinaiya/actions/runs/43765825209)
on `7b59e84ce7b1a8116cec285c923c9321506e6803` passes seven standard jobs,
including 14,989 cases on each of 3.11, 3.13 and 3.14. Python 3.12 reports
14,988 passed and one failure in the selected-window oracle-exhaustion negative
control, with nine skips, 49 live deselections and two subtests. The native
capture remained `started` after its bounded best-effort finish; this is unknown,
not proof of terminal error. [#643](https://github.com/rgoza1996/Kinaiya/issues/643)
retains that failure and the conservative test-only correction merged in
[PR #644](https://github.com/rgoza1996/Kinaiya/pull/644). The positive oracle still
fails on that uncertainty and cannot advance to another observation.

The resumed branch integrates main32d5fb7a and the private trusted-bootstrap
entry protocol in [PR #647](https://github.com/rgoza1996/Kinaiya/pull/647), alongside
the passive standing/media observations and isolated Dart fixture. This is a
changed runtime/test composition; the prior failures remain retained rather
than counted as passing runs. A fresh full local and hosted qualification is
required for this composition. PR #647 must land through its own checks before
this additive lane merges. No historical failure cause or physical target
qualification follows from the controlled entry-ordering tests alone.

The SDK-generated analyzer-file change is tracked in
[#637](https://github.com/rgoza1996/Kinaiya/issues/637). That file and the existing
GUI recovery test's generated receipt were preserved as post-run artifacts and
then restored to the original branch bytes. No runtime, test/resource deadline,
expectation, interpreter pin or qualification source was changed to hide either
startup failure. The full migration and actual native platform acceptance remain
pending.

The separately declared VibeVoice derivative remains an isolated CPython 3.12
x64 worker (`requirements-vibevoice.in`), with an explicitly cp312 Torch wheel.
It is not silently installed into the host's 3.13 environment or relabeled as
3.13-qualified. Its protocol/worker-runtime boundary retains separate checks.

## Python 3.11 retirement

The retirement wheel declares `>=3.12,<3.15`. The installer validates the
selected interpreter before resolving a release. The staged launcher enforces
the same range before loading application dependencies. A generation built
by an older 3.11 updater fails its import probe before the launcher switch.
The hosted installer workflow builds one wheel, installs it on 3.12, 3.13 and
3.14, and checks wheel and source-launcher refusal on real 3.11 and 3.15.
[Upgrading a 3.11 installation](linux-installation.md#moving-an-existing-python-311-installation)

The sandboxed extension worker uses only the system standard library in a
separate process. Its interpreter discovery is independent of the host
application minimum. The Agent symlink-loop handling also applies on 3.12,
and Converse still needs its operation-token check when cancellation races
with an in-flight observation.

Before merging retirement, record complete hosted and native ARM64/x64
results, the Ubuntu 24.04/3.12 baseline, primary 3.14 behavior, and native
wheel, packaging and sandbox outcomes. The following runs exercised the
listed revisions of `main`.

| Target | Source | Result |
| --- | --- | --- |
| Oracle ARM64, 3.12 | `ad619cba` | A bounded read before the second provider admission stopped the retry-budget test after 2,096 passes. [Job](https://github.com/rgoza1996/Kinaiya/actions/runs/44232510743/job/134560821150) · [Regression #739](https://github.com/rgoza1996/Kinaiya/issues/739) |
| Oracle ARM64, 3.13 | `71d797c5` | Full suite passed, 16,157 tests. [Job](https://github.com/rgoza1996/Kinaiya/actions/runs/44211402907/job/134490417868) |
| Oracle ARM64, 3.14 | `ad619cba` | Full suite passed, 16,213 tests. [Job](https://github.com/rgoza1996/Kinaiya/actions/runs/44232510743/job/134560822427) |
| Dell x64, 3.13 | `71d797c5` | The sealed GGUF worker exited 126 after 8,104 passes. [Job](https://github.com/rgoza1996/Kinaiya/actions/runs/44211402907/job/134490417600) · [AppArmor issue #481](https://github.com/rgoza1996/Kinaiya/issues/481) |

Prior native failures (#575/#576/#577/#578/#582/#587) retain their own evidence.
A missing platform result or an unresolved applicable blocker keeps the full
migration unmerged. No runner permission/isolation change, model-quality gate,
paid inference or historical evaluation-environment substitution is part of
this work.
