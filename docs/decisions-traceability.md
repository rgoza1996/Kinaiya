# Decisions traceability — planned owner inspection

The October 30 owner-approved [Penpot master](http://localhost:9001/#/workspace?team-id=aed0c863-4a2a-8195-8008-de835100d87c&file-id=aed0c863-4a2a-8195-8008-defdd82e75a3&page-id=1521d820-51cc-4a49-a496-12d9c823c47e&board-id=c9c2ccf2-0799-4652-a4e7-dc95e0968b06)
adds a Decisions window beside Converse and Agent. It replaces the Appearance
sidebar destination; appearance controls remain in Options. Tracked in [#149](https://github.com/rgoza1996/Kinaiya/issues/149).
This document records
the requested behavior, not implemented capture, retrieval or UI capabilities.

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

The current Laya adapter builds the rendered request in `complete()` but does not
retain it as an inspection record. Answer validation drops window confidence and
does not attach historical model/provider identity to every disposition. Existing
journal identity and ignored finalized routes are useful foundations; status and
handoff views do not meet this design. Full request retention also needs bounded
storage, owner-only retrieval and the existing privacy/deletion policy. Never
backfill historical scripts, scores or model identities from current settings.

Required synthetic verification includes ignored inputs, actual Agent observations,
question types/choices, absent confidence, refusals/retries/reuse, source corrections,
historical model changes, combined date/session filters, authorization and literal
untrusted text rendering. No example rows from Penpot belong in a production
timeline. The feature remains a separate implementation gate.
