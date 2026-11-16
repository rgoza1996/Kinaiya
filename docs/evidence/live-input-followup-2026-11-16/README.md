# Live input CI repairs (#669, #670)

This follow-up qualifies changes from PR #668's original input/lifecycle head
`04659d966d75afe4afd51c65abafdd4743adf824`. It corrects the earlier claim that
the original local commands ran the frozen 184-schema aggregate test. They ran
a separate seven-schema Decisions-attempt test instead. The original evidence
and source manifest remain in [the input record](../live-stream-capture-2026-11-16/README.md).

The original [CI job snapshot](original-ci-jobs.json) has four successful platform
jobs, four failed Python jobs and four manual-only skips. All Python versions
found the legacy selector including eleven additive schemas (195 instead of
184). The Python 3.11 job additionally observed a missing native-d1 inspection
response after a successful synthetic classification. All four original Python
logs are retained here; no original job was retried.

[#669](https://github.com/rgoza1996/Kinaiya/issues/669) changes only the selector's
explicit additive-name set. The original 184 count, aggregate hash
`21351c70f152823480441448d140c49cd561aa9be0a9a4e760c1d8035257328a`, parser
limits and literal codec expectations remain unchanged. All 568 prior schema
files are byte-identical. The eleven new readiness/stream files retain their
independent pin
`e7453211778e17609023c0488655584ed8d2bf9581873cd7c125c0303e813f34`.

[#670](https://github.com/rgoza1996/Kinaiya/issues/670) enrolls the original
inspection finish HTTP send, bounded response read and sole close in the existing
journal metadata owner. A half-second facade timeout or caller cancellation
does not cancel that original write. The record stays pending until native
completion; there is no replacement finish or inference replay. Native client
shutdown retains its existing cancellation/drain behavior. The original CI
trace does not prove a timeout caused that particular occurrence. A deterministic
ASGI control establishes the real cancellation defect that can produce the same
missing-finish state.

That control gates before/after native commit, separately testing timeout and
caller cancellation. All four cases originally failed the retained ownership
assertion; all now also require the original response bytes, original request,
completed native record, one write and exactly one response close. Earlier
fixture mistakes (missing target, wrong typed label and wrong expected v1 route)
and their first logs/snapshots are retained and identified in the issue comments.
The original pre-existing native-d1 test assertions remain unchanged.

Current focused qualification passed **361 cases on each Python 3.13.18 and
3.14.10**, in 76.62s and 77.45s. Each reported one existing Starlette warning.
The broader Python 3.13 regression passed **2,035 cases in 543.69s**, also
with one existing Starlette warning. All three runs used the same 896 unchanged
Python inputs identified by
[python-input-sha256.json](python-input-sha256.json). Full Ruff and a 1,173-file
format check, schema export and diff check passed. The receipts contain the
exact commands and log hashes. Counts overlap and are not unique coverage.

Only `packages/contracts/client.py` changes production Python relative to the
original qualified head. The two changed test files add the four ownership
controls and explicit additive schema names. Normal host, Call and UI still
leave Adaptive Live gated off. Model output, model/default selection, protocols,
native permission boundaries and physical audio qualification are not changed.
Full #623 remains open.
