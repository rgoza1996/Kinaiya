# ADR 0004: Protected executable harness (context_selector.v1)

Accepted for issue #6. Implements Roadmap sections 10–12 and M4: a pinned
baseline selector, bubblewrap-isolated candidate execution, independent
evaluation on a heldout suite, and owner-reviewed promotion with rollback.

## Fixed interface

`from core.extensions import Registry, Manifest, SandboxUnavailable`

- `Registry(root: Path, suite_path: Path, model_profile: dict | None = None)`
- `register(source_path: Path, manifest: Manifest | dict | None = None)`
  -> immutable version ID (`v-` + 24 hex chars, content-addressed over
  contract + source bytes + canonical manifest).
- `evaluate(version_id)` -> serializable `EvaluationReport`
  (`report_id`, `passed`, `baseline`/`candidate` metrics, `reasons`).
- `promote(version_id, report_id, *, actor='owner', reviewed=False)` ->
  version ID. Requires `actor == 'owner'` AND explicit `reviewed=True`
  AND the authoritative stored passing report bound to the candidate,
  its source hash, the baseline, the suite hash, the predeclared policy,
  and the registry's model profile. Never a caller-supplied score.
- `rollback(*, actor='owner')` -> version ID (previous known-good).
- `active` property -> pinned active version ID.
- `run(version_id, payload)` -> `list[str]` (any registered version,
  so a task can pin an older version after the pointer moves).

## Executable contract

A source file defines sync `select_context(payload) -> list[str]`.
Payload: `{query: str, files: [{id, path, text}], max_chars: int}`.
The candidate receives ONLY this bounded JSON; no workspace, transcript,
credential, core, registry, or evaluator mounts. Host validates returned
IDs exist in the input, are unique, and fit the budget.

**Budget unit (honest): characters** — `sum(len(text))` of selected files
must be `<= max_chars`. A conservative proxy for tokens, not a token
count. Evaluation uses no per-case model calls for either version;
creation-inference cost is reported separately by the lead. No claim of
overall Agent improvement follows from context recall alone.

## Security

Generated source runs in a separate Linux bubblewrap process:
`--unshare-user/pid/net/uts`, cleared environment, `--cap-drop ALL`,
`--die-with-parent`, read-only `/usr`, a single copied candidate source,
a private runner dir holding ONLY the trusted `_runner.py` copy
(evaluator/registry/criteria sources are never bound, so untrusted reads
fail even past AST checks), and the input payload,
isolated `/proc`, tmpfs `/dev`/`/tmp`/`/work` (no host directory is
writable — storage abuse is bounded by memory plus the 1 MiB per-file cap),
system `/usr/bin/python3.12` (never a venv importing the host repo).
Missing/unusable isolation fails CLOSED with `SandboxUnavailable`; there
is no ordinary-subprocess fallback. The wall timeout and a per-stream output cap are enforced incrementally by
the host pipe reader: either stream exceeding the byte bound kills the
whole process group and fails closed, and the group is always reaped. The
configured CPU hard limit (not wall+5) plus 256 MiB RAM, file size, and
zero core dumps are set by the trusted in-sandbox runner BEFORE untrusted
code loads (no host-user NPROC limit is set before `bwrap` forks); a
required limit that cannot apply exits the runner so the host refuses
execution. Candidate stdout/stderr are discarded during the call; exactly
one JSON document
(`{"ids": [...]}` or `{"error": ...}`) is emitted on a saved fd, and the
host uses ONLY `ids` — forged score/lifecycle fields cannot count as
evidence. Initial packages are stdlib-only and the import allowlist plus
manifest rules are explicitly documented in `sourcecheck.py` /
`manifest.py`.

## Registry / evaluation / promotion

Source + manifest are content-addressed immutable copies; IDs, paths,
symlinks, size (64 KiB), UTF-8, AST shape, imports, entrypoint,
capabilities, and (empty) dependencies are validated. Integrity is
re-checked before every run, evaluation, and promotion. Registry reports
and the activation pointer are atomic writes with an append-only audit
log; candidates have no path to edit them. Evaluation pins the suite hash
and the predeclared policy (`evaluation/harness/policy.py`,
`POLICY_VERSION harness-policy.v1`); baseline and candidate get
identical inputs and budgets; expected answers never enter the sandbox.
Reports record recall, validity/failure rates, wall times, bounds, and
per-gate reasons. Rejected candidates/results are preserved. Own unit
tests never qualify a candidate; unknown/unsupported model or platform
capabilities are explicit (`SandboxUnavailable`, `RegistrationError`).

Suite format is documented in `evaluation/harness/suite.py`. Public
development fixtures (`dev_fixtures.json`) are labeled NOT-heldout; the
lead creates the fresh protected heldout before real candidate work.

## Usage

```python
from pathlib import Path
from core.extensions import Registry

reg = Registry(Path("var/harness"), Path("var/heldout.json"),
               model_profile={"model": "m1", "provider": "p"})
vid = reg.register(Path("cand.py"), {"name": "cand-1"})
report = reg.evaluate(vid)
print(report.passed, report.reasons)
if report.passed:
    reg.promote(vid, report.report_id, actor="owner", reviewed=True)
print(reg.active, reg.run(reg.active, payload))
```

## Limitations

Single-owner Linux experimental deployment (per ADR 0002): requires
bubblewrap user namespaces; otherwise execution is disabled, not
degraded. Recall-on-synthetic-fixtures is a screening signal only, not
evidence of task improvement; repeated adaptive access can overfit even
a fixed holdout — rotate heldout sets. Host-administrator bypass is out
of scope (Roadmap 11.3). No network or FS capabilities exist yet; any
future capability needs a new manifest contract, sandbox binds, and ADR.
NPROC is deliberately not set inside the sandbox: with the uid-0
user-namespace mapping the call can leak onto the host user (observed as
fork EAGAIN) while not reliably containing the candidate, so no
process-count limit is claimed; fork/exec abuse is contained by the PID
namespace plus host group-kill and rejected statically. No seccomp filter
is installed (documented, not claimed). A suite holds at most 1024 cases.
Registry mutation paths share one trusted lock for owner-thread
(`to_thread`) use. A green candidate additionally requires a valid,
comparable baseline: any baseline case failure blocks promotion.

## Lead verification: kernel containment

The lead's first clean run passed all 37 Muse harness tests. Review still found that PID namespaces and timeout cleanup alone do not prevent a candidate from exhausting host process slots or tmpfs storage before the deadline. The initial selector contract now requires libseccomp and a kernel syscall allowlist, installed before any candidate module code runs. The runner first loads bounded source/payload and the fixed permitted stdlib modules. Candidate execution can then compute and use existing bounded pipes; opening files, spawning processes/threads, executing programs and creating sockets fail in the kernel. Missing library, unsupported filter loading or required resource-limit failures refuse execution. The static Python checks are defense in depth, not the isolation boundary. This narrow pure-selector contract intentionally does not support arbitrary third-party runtime dependencies or filesystem capabilities.

The filter uses the documented [libseccomp initialization](https://github.com/seccomp/libseccomp/blob/main/doc/man/man3/seccomp_init.3) and [rule APIs](https://github.com/seccomp/libseccomp/blob/main/doc/man/man3/seccomp_rule_add.3). Adversarial tests bypass registration to verify actual syscall denial and failure when the filter cannot load. This remains an experimental Linux boundary, rather than a claim of general Python or container hardening.
