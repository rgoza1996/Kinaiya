# Trusted bootstrap entry qualification

Requirement and protocol: [entry framing v1](../../bootstrap-entry-protocol.md).

The [verification record](verification.json) preserves the frozen baseline failures,
new-path regressions #645/#646, final 591-case expanded Python3.13 result,
39-case Python3.12 result, unchanged native guard/cleanup body audit and actual
installed-wheel ownership/output/reap proof. Full raw logs remain in the local
`output/issue-577-*` artifacts and are identified by SHA256.

This establishes controlled entry ordering before native identity verification.
It does not establish the historical cause of every #577/#620 refusal, physical
cgroup isolation, native target qualification, model behavior or a completed
resource lifecycle matrix. Exact-head hosted CI remains separate evidence.

## Follow-up on the positive JPEG failure in #620

On Linux x64 with Python 3.14.10 and the hashed development dependencies,
the unchanged positive JPEG test reproduced the failure at pre-repair commit
`7299f8b`. Sample 114 failed at bootstrap verification after 113 passes.
The existing observer recorded a zero-byte command-line read and
`pre_command_mismatch`. The 18-original slot-reuse case also produced this
pair of observations on its first run. The original hosted failure predates
the observer.

At integrated commit `ec1ab17`, all 120 positive JPEG samples passed, as did
12 runs of the slot-reuse case. Each pytest sample used a fresh fixture.
The cases are
`test_actual_still_image_point_body_and_real_eof[positive-jpeg]` and
`test_more_than_sixteen_positive_originals_reuse_slots_without_rearming`.
The entry protocol and its controlled failure cases are in PR #647.
