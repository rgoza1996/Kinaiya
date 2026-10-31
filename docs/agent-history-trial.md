# Bounded M1 repository workflow: Mica

On October 31, 2026, one frozen public-synthetic Mica repository attempt
completed the native Agent inspect/modify/test workflow in **50.4315 seconds**.
It read the complete README, source and public tests, changed only
`mica/ledger.py`, ran the exact public command, and reached a confirmed durable
`succeeded` outcome. Independent review found no candidate or evidence blocker.
This qualifies the observed bounded workflow for [M1/#2](https://github.com/rgoza1996/Kinaiya/issues/2),
not general Agent reliability, production readiness or every M1 exit gate.

The [JSON capsule](../evaluation/evidence/agent-history-mica-2026-10-31.json)
is an explicit sanitized projection. It contains synthetic fixture/candidate
texts as JSON strings, frozen hashes, selected measurements and host-grader
values. It contains no private host paths, credentials, headers, process or
private task/session/provider-response identities. The embedded Python texts
are evidence data, not installed or executable test packages. The fixture is
now consumed and must not be presented as a fresh future evaluation set.

## Frozen source and scope

The executed clean source was `1f19315396ff3e129ac90612f97efb547d1a65e5`, tree
`8fcc8a041607e5104222adb78cbadfff680a9332`, integrated by
[PR185](https://github.com/rgoza1996/Kinaiya/pull/185) as
`06117063433943cfc4fbdb7cc8322350a9bdb4d7`. Root verified all five jobs on
[exact-head CI run 41220660884](https://github.com/rgoza1996/Kinaiya/actions/runs/41220660884)
before admitting the attempt. This CI provenance is projected from the frozen
trial declaration; preparation of this public capsule made no network request.

The trial used the real `AgentRuntime`, `Store`, role-Agent `LocalJournal` and
`MetaResponsesProvider`, with Contributor `muse-spark-1.3-contributor`, `xhigh`,
8,192 reserved output tokens, explicitly configured context capacity 1,007,997,
flat-v1 actions, at most 18 steps/count requests and a 300-second deadline.
The fresh trial constructed and used no Converse or Decisions runtime; unrelated
host services were preserved. There was one Mica attempt, with no automatic
retry or hand-written replacement of model output. Complete-context admission
and read-only public-reference protection are described by
[ADR0075](adr/0075-complete-agent-context.md) and
[ADR0076](adr/0076-readonly-workspace-references.md).

The frozen preparation suite passed 94 offline tests with no skip or
deselection, including the source-pin gate; root repeated those 94 checks before
the attempt. Those preparation checks are separate from live task evidence.

## Observed workflow

Mica's seeded defect aggregated by original SKU spelling and normalized only
the output. The README requires aggregation by exactly
`sku.strip().casefold()`, integer precision, canonical ordering, final-zero
handling, one-pass consumption and explicit invalid-input errors. Before
inference, the unchanged baseline failed seven of 14 public tests and six of
eight independently authored reference tests. A separate correct reference
passed both sets under mandatory BWrap isolation; that reference never entered
the Agent workspace. These are synthetic author judgments, not human ground
truth or a general benchmark.

The native event order was three successful reads, a write approval/resolution,
the write, a shell approval/resolution, the shell result and one successful
outcome. The first edit's generation request contained all three exact native
`[ok]` observations: README 2,299 UTF-8 bytes, source 909 bytes, and public tests
4,084 bytes. The test file's substantive tail checks cancellation, ordering and
input preservation. The native read receipts have the original complete-file
hashes and precede the write. This verifies whole delivery in this attempt,
including the tail; it does not prove that the model attended to every byte.

The runner resolved two native owner-role approvals under the frozen
authorization: only the exact `mica/ledger.py` write at revision 1 and
`python3 -B -m unittest discover -s tests -v`. These were action-specific native
resolutions, not a claim that two new human clicks occurred. Read-only README,
tests, license and package exports and protected Git metadata remained unchanged.
The candidate made the general two-line canonical-key correction, with no
imports, hardcoded examples or fabricated command output. Its full AST matched
the separately frozen correct reference. The final write receipt matches the
902-byte candidate hash. The exact public command ran after that write, exited
zero and reported all 14 unchanged public tests passing.

The retained SQLite journal was independently opened read-only. Its 11 durable
entries exactly matched the exported native events. The single successful
outcome contains the same Task snapshot as the trial receipt and post-cleanup
status, with the actual read/write/command evidence. Runtime/provider closure,
worker completion and the observed shell child's wait/reap were confirmed in
the native cleanup receipt. The executed source stayed clean at the pinned
HEAD/tree.

## Count, generation and independent grading

There were **six count requests and six generation requests**. Every captured
count body was byte-identical to its paired generation body. The independent
review recomputed all 24 request/response hashes and lengths, verified the
requested/resolved model and reasoning profile, and found HTTP 200 and complete
EOF records for all 12 responses with zero capture errors. The public capsule
publishes these digests and selected scalars, not raw private captures. Captured
hooks and retained responses are not an independent network tap.

Every count-endpoint `input_tokens` result exceeded the paired generation
usage's `input_tokens` by **143**. Exact body identity is established; this
record does not independently qualify tokenizer accuracy or billing accounting.

After native acceptance and confirmed cleanup, candidate code ran only inside
a separate mandatory BWrap grading workspace. That read-only workspace contained
the candidate package, a frozen stdlib driver and input-only JSON. It received
no expected values, author grader source, credentials, core or network grant.
The trusted host compared the bounded complete child JSON response with frozen
expected values/error categories: all **eight cases, ten calls** matched,
including a generator and cancellation of `10**80` integers. A child exit code
or unittest-looking footer was insufficient; an offline synthetic summary
forgery had been demonstrated and is refused by this final host protocol.
Grading files remained unchanged and grading-child cleanup was confirmed.

Input nonmutation, return container shape and generator consumption remain
**untrusted child-reported properties**. The host independently compares received
fixed values/error categories, not candidate memory or arbitrary internals.
Static review supplies separate support for this simple general implementation.
The independent artifact review passed **208 predicates, not 208 tests**; it
executed no candidate, grader, model, runtime or HTTP request. Historical sandbox
and reaping confirmation relies on pinned code, native receipts and unchanged
snapshots; artifact review does not reconstruct past kernel events.

The development guard's incremental Contributor estimate was **$0.001648096**;
its cache-aware admission estimate delta was $0.003296192. These are estimates,
not an invoice. Actual reported provider billing remains **null/unknown**;
outstanding reservations and the existing $22 admission/$25 budget limits were
preserved.

## Preserved failure and qualification boundary

The earlier consumed Waterlog attempt remains
[failed/unqualified](../evaluation/evidence/agent-history-baseline-2026-10-31.json):
18 completed provider responses, 14 reads and four searches, no approval, edit,
test command or candidate. Its clipping/history loss was independently
reproduced, but was not proved the sole cause of repeated reads. It was not
retried here. Mica is a different fresh case after the repair, so these results
are **not a paired causal comparison**. The existing failure record is unchanged.

The capsule binds the original fixture/heldout/runner/declaration hashes, actual
trial receipt and independent review receipt. Its
[data-consistency tests](../tests/unit/test_agent_history_trial_evidence.py)
check embedded text hashes, the exact candidate diff, wire-pair/scalar
consistency, approval/evidence ordering and frozen host-value comparison. They
do not replay model inference or replace the independent private-artifact audit.

No default model, voice behavior or production readiness is established by this
one workflow. Combined with the prior CLI/browser, policy, steering and failure
evidence in [#2](https://github.com/rgoza1996/Kinaiya/issues/2), it supplies the
previously missing baseline repository exit gate. Glimmer/local-provider
qualification remains separately open in [#186](https://github.com/rgoza1996/Kinaiya/issues/186).
The [implementation ledger](implementation-status.md) distinguishes that baseline
from broader reliability and release work.
