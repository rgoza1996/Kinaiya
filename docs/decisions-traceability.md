# Decisions traceability — owner inspection

The October 30 owner-approved [Penpot master](http://localhost:9001/#/workspace?team-id=aed0c863-4a2a-8195-8008-de835100d87c&file-id=aed0c863-4a2a-8195-8008-defdd82e75a3&page-id=1521d820-51cc-4a49-a496-12d9c823c47e&board-id=c9c2ccf2-0799-4652-a4e7-dc95e0968b06)
adds a Decisions window beside Converse and Agent. It replaces the Appearance
sidebar destination; appearance controls remain in Options. Tracked in [#149](https://github.com/rgoza1996/Kinaiya/issues/149).
The Python capture and owner retrieval implementation follows the
[versioned inspection API](decisions-trace-api.md). The owner screen is a separate
client of this read-only contract; this document records its intended behavior.

Each inspection record must retain the actual source transcript or Agent event,
including ignored inputs, and its native source/revision and session identity.
Direct Agent Chat continues to bypass conversational routing: inspecting an Agent
event must not invoke a new classifier or create work.

Capture the exact submitted Decisions request at the provider boundary, including
rendered state, every question, its type, instructions and all possible answers
or criteria. A reconstructed generic prompt is insufficient. Preserve actual
structured answers, per-question confidence/distributions when supplied, model,
provider and verified artifact identity when available. Missing historical values
remain unavailable. Confidence is a model signal, not calibrated certainty.

The window shows session ID, decision ID, attempt/call identity and timestamp.
It supports bounded pagination, source/outcome filters, text search, single-date
and date-range selection with explicit timezone and range boundaries. Script and
output remain literal selectable text; opening, copying or searching never runs
inference, grants permission or changes execution state.

Do not generate a rationale or summary and present it as the output of a
discriminative Decisions model. Native reason codes, native overrides, errors,
refusals and no-inference dispositions need explicit labels. Reused classifications
link to their original inference. Keep a routing decision separate from the
Agent's acceptance, execution and completion evidence.

Laya now captures the actual body built in `complete()` and its bounded raw
response before answer mapping; compatible and Meta Decisions calls use the same
attempt-scoped capture boundary. Window confidence/distributions remain in the
actual response rather than being invented as a native route confidence. Native
overrides and executor receipts stay separate. Existing records without capture
retain unavailable fields; there is no historical prompt or identity backfill.

Session-owned private tables keep capture outside generic transcript readers and
event streams. Owner reads page actual source events; Agent observations explicitly
have no Decisions inference. Payloads share a256MiB quota with oldest payload
expiration and explicit markers; session deletion cascades metadata and content.
Reset retains historical epochs. Captures are bounded and idempotent, and capture
failure cannot change routing permissions or claim retained evidence.

Required synthetic verification includes ignored inputs, actual Agent observations,
question types/choices, absent confidence, refusals/retries/reuse, source corrections,
historical model changes, combined date/session filters, authorization and literal
untrusted text rendering. No example rows from Penpot belong in a production
timeline. Synthetic capture/retrieval regressions exercise these cases without
provider inference; physical client inspection remains a separate acceptance gate.
