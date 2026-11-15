# Python 3.13 qualification (#624)

This is the additive qualification stage of
[#624](https://github.com/rgoza1996/Kinaiya/issues/624). CI temporarily exercises
3.11, 3.12, 3.13 and 3.14 with the same complete non-live suite. Manual full/media
selection accepts 3.13. The application minimum, installer, Doctor, Ruff target,
dependency locks, original interpreter pins and runner admission/isolation
remain unchanged. This does not complete the 3.11 retirement.

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
The x64 archive was also downloaded and hashed locally. ARM archive execution
is pending; matching published metadata is not an ARM qualification.

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
  The wheel retains the existing `Requires-Python: >=3.11` during this stage.
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

## Remaining retirement acceptance

Before replacing the supported matrix with 3.12/3.13/3.14, record complete hosted
and actual ARM64/x64 results, native wheel/install/package/sandbox behavior,
meaningful Ubuntu 24.04/3.12 and primary 3.14 baselines, and any unresolved
platform failures. Then update minimum-version metadata, installer/Doctor,
compatibility fixtures, documentation and release statements together. Existing
3.11 installations must receive an explicit upgrade requirement. Remove a
3.11 workaround only with evidence that it is obsolete.

Prior native failures (#575/#576/#577/#578/#582/#587) retain their own evidence.
A missing platform result or an unresolved applicable blocker keeps the full
migration unmerged. No runner permission/isolation change, model-quality gate,
paid inference or historical evaluation-environment substitution is part of
this work.
