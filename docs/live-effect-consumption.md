# Native live outcome consumption (#623)

Status: gated development boundary. An explicit [native Agent consumer](live-agent-consumption.md)
now uses it in trusted embeddings. The normal host and browser Call do not
enable a v2 consumer. Legacy speech sources gain no live revision equivalence.
This ledger alone does not complete Adaptive Live.

Session owns `native_live_effect_v2`, schema 2.0, under
`/v2/transcript/live-effects/{lookup,admit,start,finish}`. Its nine exported root
schemas are additive; all 579 prior schema files remain byte-identical. The
family's independent byte pin is
`1044614df72ec0f52a38947ce3063745030401ec9baa6d2144cfdbdc13e2daf9`.

One original reservation binds an authenticated native v2 publication to one of
`converse_reply`, `converse_backchannel` or `agent_route`. Converse owns the first
two; Decisions owns the last. The original decision must actually select that
effect. Reads grant nothing and create no table, reservation, inference or wake.
Owner, Converse, Decisions and Agent can look up metadata; Resources cannot.

Converse admission requires current native capture/source authority. A published
Agent outcome retains independent user authority after microphone closure. A separate atomic
start binds one actual executor instance and operation. An unstarted reservation
can be adopted after owner replacement. Once started, a different executor is
refused. An identical request reads its original marker: this is not a second
effect grant. The eventual consumer must retain its one actual execution Task,
reconcile native evidence after uncertain acknowledgments and prevent duplicate
execution on idempotent readback.

Published Agent routing can be reserved and started after the microphone ends.
Session/thread/epoch, original text, disclosure scope and
STT binding must still match current native user authority. A later authenticated
v2 interrupt fences earlier queued sources, including other utterances in that
scope. Ordering uses actual source sequence, so delayed publication of an older
interrupt does not cancel newer user instructions. Agent's own execution,
approval, task revision and tool permissions remain separate native controls.

A completion records `declined`, `refused`, `uncertain` or `recorded`. The last
requires authentic native journal evidence with the original scope, action,
source, receipt-derived idempotency key and a sequence after the actual start.
An uncertain effect retains active capacity and cannot be admitted or started
again. Positive late native evidence can resolve it to `recorded` without a new
effect. Other completion replacements are refused. A recorded Agent command is
not task success; a recorded Converse response is not proof of audible playback
or physical cleanup. Consumers must report uncertainty truthfully.

There are at most 64 active or uncertain effects and 10,000 retained records.
No history eviction makes uncertain work reusable. Existing physical resource
ownership remains independent. Construction is passive. Mutations refuse a
foreign SQLite transaction without committing or rolling it back.

Bodies authenticate before acquisition and again before mutation, accept at
most 16 KiB of identity JSON within five seconds, and reject duplicate fields,
fractional counters, nonfinite values, invalid UTF-8, excessive structure and
unknown fields. Responses are uncached and at most 32 KiB. This family uses its
own depth/node limits; the frozen STT parser stays unchanged. JournalClient keeps
the original bounded HTTP read and sole close after facade cancellation and
performs no automatic write replay. LocalJournal preserves role parity.

The explicit Agent consumer has bounded discovery and native evidence/source
qualification. Actual Converse output/playback, complete process replacement,
Call integration, representative timings and qualified
rollout remain outstanding in #623. Model quality is diagnostic and is not an
acceptance gate.
