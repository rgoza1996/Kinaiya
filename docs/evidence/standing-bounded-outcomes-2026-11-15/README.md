# Standing bounded outcome evidence (#633 / #487)

Baseline: `548f85d84874b2b0d3366ffdb36f8c74aa880ae8`. The original ordinary
outcome repair, `212c4bc3242e67182afda32128c413f159c1f89d`, is already in that
main revision. Its unchanged 56 focused checks pass before this work.

The candidate preserves ordinary v1 and inline records and adds a distinct
standing v2 commitment to the complete native initial row. See the
[contract](../../agent-bounded-outcomes.md) and [verification receipt](verification.json).
The native origin and immutable ordered tool evidence are required; a reference
or retained initial proof never creates permission or launch authority.

The final combined run passes **783 cases, zero failures/skips, in 377.86
seconds across 34 modules**. It contains 37 new standing cases and 746 existing
regressions. All 836 repository Python inputs have identical hashes before and
after execution. The receipt pins the five changed Python files, complete
command and manifest hashes. Contract export is unchanged; Ruff check, format
check (1,042 files) and diff checks pass. Targeted runs overlap this total and
are not added to it. Remote exact-head CI remains required before merge.

## Actual boundaries exercised

The integration fixture uses real Session/SQLite, authenticated JournalClient
and native one-use standing admission, claim/resource binding, export admission,
captured model lease, Agent loop, file policy/read tools and actual cleanup.
Only measured inference is synthetic. Forty fixed reads of one public tiny file
through a long valid relative path produce original native receipts whose
duplicated terminal request exceeds 65,536 bytes. File receipts contain no file
content output. The test declares its 42-step budget before admission; production
defaults and the standing file-only subset are unchanged.

All nine terminal/transport combinations are checked: succeeded, failed and
cancelled, each with confirmed, lost and absent terminal acknowledgement. There
is exactly one terminal attempt; committed sparse requests fit the unchanged
limit and restore every original Evidence object in order. Absent writes remain
paused/unconfirmed with the frozen intended snapshot and no replacement work.

A fresh runtime and its own native client recover confirmed terminal history
with no ordinary submit identity/reservation, standing work owner, provider
lease or worker. Incomplete admission is not installed as ordinary paused work.
The same fresh runtime can look up retained admission without acquiring a model
or consuming a launch signal. Retained-control bookkeeping remains fenced and
owned until its actual cleanup; the test does not mistake its presence for work.

Authenticated observer reads refuse missing/changed initial metadata, wrong
admission family, missing/changed receipts and changed event provenance.
Pure contract vectors additionally cover full initial time/identity/model/scope
bindings, closed reference fields, bad digests and cross-family downgrade.
Ordinary count/byte/page/deadline and malformed-proof checks remain in the
combined run. The new CPU-validation deadline fault advances only the standing
module's local clock after a real proof/index check; the original admission
commits, but no late worker launches. Existing final model/workspace deadline
controls remain unchanged.

Converse performs full successful hydration with its actual service credential
but returns no notice for the native Agent channel. Tampered evidence fails
before a notice or provider call. The existing channel rule is retained.

## First failures and fixture corrections

[Check history](check-history.json) retains the original log names, hashes and
summaries. Logs remain in the local `output/issue-633-*.log` evidence set.

Before source changes, the new standing contract suite fails 25 cases; the
actual native suite fails all ten cases in 93.99 seconds: nine large terminal
cases make no terminal POST, while a small inline terminal is not recovered.
After implementation, all 66 ordinary/standing unit checks pass. The first
native rerun passes 25 and fails three new exception expectations: the retired
standing owner correctly refuses a second control through its permanent
PolicyDenied fence before the ordinary TaskTerminal guard. The test now asserts
that exact standing refusal and the permanent fence, retaining no-retry checks.

A later 94-pass/one-failure run exposed a test expecting Converse speech in an
Agent channel. The valid result is no notice. A proposed Converse-channel
fixture was itself rejected by real native standing authority (409); that
fixture was removed. No service/channel guard changed. Further new assertions
were corrected to distinguish fenced retained bookkeeping from a model/worker
and to give each restart its own native client: closing the restart owner had
correctly closed the mistakenly shared original client. Both final targeted
observer/deadline cases pass in 11.73 seconds. Ruff's initial formatting and
loop-capture findings were corrected without changing production behavior.

## Limits

This is architecture qualification with public fixtures, not model-quality,
live scheduler, physical resource settlement or installed-owner qualification.
No paid inference, historical task replay/backfill, default-model change,
permission widening or journal-cap increase occurs. The historical #487 task's
missing durable outcome remains uncertain. Broader standing work #228 and
deployment/cutover work retain their separate acceptance requirements.
