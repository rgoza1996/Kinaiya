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
