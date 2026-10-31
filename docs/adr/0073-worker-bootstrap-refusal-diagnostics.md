# ADR 0073: Constant pre-grant worker refusal observations

Status: accepted for the bounded #176 diagnostic slice.

CI231 failed the existing worker memory lifecycle case with
`ownership-unverified` before grant, configured code or readiness. The existing
verifier did not identify which predicate refused ownership. Equivalent coarse
failures are retained under #9; later successful starts do not establish their
cause. PR175's provider changes do not modify the worker verifier.

Add a best-effort warning at each existing refusal path with one of 17 closed
reason labels and, only for pidfd-open errors, a closed errno category. Preserve
the verifier's tuple interface, predicates and short-circuit order, process
observations, handle closure, exception behavior and pre-grant authority. Close
any opened handle before observing refusal. Never log process identity, nonce,
command, path, raw exception or configuration. Ordinary diagnostic failures
cannot change the existing refusal outcome.

Do not add retry, polling, delay, timeout, adoption, signal or permission. A
reason reports the observed boundary; it does not establish a historical cause
or authorize recovery. Full startup reliability and the M6 gate remain open.
The [contract](../worker-bootstrap-diagnostics.md) and
[verification record](../../evaluation/evidence/worker-bootstrap-diagnostics-2026-10-31.json)
retain the original failing CI and bounded local checks.
