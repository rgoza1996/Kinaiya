# Native live Agent consumer qualification (#623, #671)

This record covers the gated [native Agent consumer](../../live-agent-consumption.md)
on `codex/623-live-consumers`, after integrating parent PR #668 at local merge
`5d4c533b649b64b2d63469bd07fac8126aaeee69`. Target main is
`cf477a36fad51bc92b40419bee2d310e834b6719`. The earlier
[ledger-only record](../live-effect-consumption-2026-11-16/README.md) remains historical.
Normal host, browser Call and default Capture Mode do not enable this consumer.

Final focused qualification passed **234 cases on Python 3.13.18 in 54.40s**
and **234 on Python 3.14.10 in 54.38s**. Both reported one existing Starlette
deprecation warning and match the identical **911 unchanged Python inputs** in
[python-input-sha256.json](python-input-sha256.json). Counts overlap.

The prior broader Python 3.13 run passed **900 cases in 228.51s**, with the same
warning. Its only later changed inputs are `services/decisions/live_agent.py`
and `tests/integration/test_live_agent_consumer.py`: the explicit queue-target
existence/scope/revision guard and its missing-target regression control. Both
final focused runs cover that guard. The broader run was not repeated for this
narrow change. The earlier 233-case Python 3.14 result is retained separately.

| Requirement | Actual verification |
| --- | --- |
| One native Agent handoff | Real Session and AgentRuntime submit, steer and interrupt; exact source IDs/text, objective revision, supervisor origin and command/intent evidence |
| No replay after lost acknowledgment | Real Agent ASGI before/after native commit, retained original Task through facade cancellation, replacement dispatcher observing an existing start, positive late evidence without a second POST |
| Native queue ownership | Busy Agent waits; missing target stays reserved/unstarted; atomic unconfirmed-handoff blocker; microphone closure does not revoke published work; later user interrupt fences old queues |
| Faithful source authority | Actual capture pairs and historical raw revisions; harmless speaking/end hints accepted; changed text/disclosure and changed-then-restored text refused; legacy owner windows gain no live equivalence |
| Execution boundary | Actual post-acceptance steer application accepts an equivalent captured raw revision without changing source text or granting permissions |
| Bounded service lifecycle | Actual Decisions lifespan and journal wakes, no idle busy loop, healthy owner-history retirement, original handoffs retained across facade cancellation |
| Passive discovery and roles | Actual SQLite rows/wakes unchanged by reads; retained uncertain ownership across resets, bounded ordered continuation pages, HTTP/LocalJournal parity, Resources denied |
| Contract compatibility | All 588 prior JSON schemas unchanged, original nine-family pin preserved, six additive roots separately pinned, original 184-schema aggregate unchanged |

Full Ruff and the **1,192-file format check** passed. Schema export passed with
the explicit workspace `PYTHONPATH`; all prior JSON bytes still match. The
first export command lacked that path and imported the older installed package,
failing before export. Its exact command, log and issue-before-correction comment
are retained. No source was changed to accommodate that command error.
The working source/prose whitespace check passed before evidence assembly; the
final staged check is recorded separately at publication.

[verification.json](verification.json) retains exact commands, results, log
hashes, original receipt hashes, issue-before-fix links and source snapshots.
All 18 first failing groups are retained, including explicitly identified
fixture mistakes. JSON log envelopes preserve exact UTF-8 bytes; decoding
`content` reproduces the stated SHA-256 without trimming pytest whitespace.
Each run's original input manifest reconstructs by removing its
`absent_from_manifest` paths from the shared manifest and applying its
`input_hash_overrides`. Complete small source/control snapshots are similarly
byte-exact and deduplicated by hash. Large native runtime snapshots are clearly
labeled derived method excerpts with original full-file hashes and line spans;
the complete originals remain in root `output/`.

The interrupt change for [#671](https://github.com/rgoza1996/Kinaiya/issues/671)
retains its original request key, expected target revision and incoming source
provenance in the native command event. The actual native interrupt control
passes. Dedicated interrupt retry/lost-ACK and journal-failure ownership
acceptance is still outstanding, so this record does not close #671. A recorded
command is evidence of acceptance, not task success or physical stop.

This local record predates hosted PR CI. No paid/live model, physical audio or
installed-owner promotion was used. Actual Converse consumption, normal host/
Call integration, complete owner/process replacement, echo/mute/accessibility/
no-audio qualification, representative timing comparison and qualified default
rollout remain outstanding. Full #623 remains open. Model answer, classification,
persona and speech quality are not architectural acceptance gates.
