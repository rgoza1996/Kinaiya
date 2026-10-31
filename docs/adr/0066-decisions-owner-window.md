# ADR 0066: Read-only Decisions evidence window

Status: Implementation candidate, tracked by #149.

The owner requested the Decisions window in the October 30 Penpot master,
including the actual submitted script and historical decision provenance.
The navigation destination replaces Character & motion; presentation settings
remain available through Appearance and Options. Voice and Agent owners remain
mounted while inspecting evidence.

The window reads the Session-owned inspection API documented in
`decisions-trace-api.md`. It displays native source events, route/execution
receipts and captured provider attempts separately. Missing or expired request,
response, confidence and artifact values remain unavailable. A direct Agent
observation explicitly reports no Decisions inference. Reuse links open the
original native source; inspection never classifies, dispatches or grants work.

Source/session/outcome/literal-search filters and inclusive source dates use an
explicit IANA timezone. The server echoes the half-open UTC interval. Timeline
and call pagination retain native watermarks and paired continuation cursors;
returning to the first timeline page uses `before_sequence=through_sequence+1`.
Only the current bounded page is retained. The cursor stack is capped at 1,000.

Requests use separate owner-authenticated transports, disable redirects, enforce
256 KiB list / 4 MiB detail body limits before decoding, and close on a ten-second
deadline. At most two unsettled reads retain admission slots until their actual
futures settle. Closing an evidence read never closes voice or task transports.

Connection and selection generations fence late responses. Changing host clears
search/session filters before reading the replacement host. An evidence dialog's
route is owned before pushing and is removed on identity change or disposal.
All scripts, raw outputs, metadata and receipts render as literal selectable
text. Copy writes only the explicitly selected literal evidence to the clipboard.

Verification maps to `test/decisions_inspection_test.dart`: bounded/failed reads,
paired cursors, exact Unicode questions and outputs, absent confidence, native
Agent observations, source/call scope bindings, stale host responses, dialog-open
race, cleared cross-host filters, expired/reused captures and populated 320 px
layouts at twice the text scale. Navigation regression tests retain voice
ownership, drafts and presentation access. Final native integration, complete
suite, CI and browser release evidence are required before marking #149 complete.
