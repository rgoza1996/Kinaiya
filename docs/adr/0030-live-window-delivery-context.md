# ADR 0030: native delivery context in live-window prompts

Tracks [#77](https://github.com/rgoza1996/Kinaiya/issues/77), Roadmap D01,
§7.3 and AT08/AT09. The live-window consumer previously forwarded generated
assistant text as ordinary history without its retained playback receipts.
This could invite an assumption that generated speech was heard. This change
qualifies that history using native metadata; it does not establish acoustic
parity, physical audibility, full M2/M3 acceptance or task completion.

The lead's read-only comparison recorded in #77 cites Korina-Agent master
`9db02e6c39e4667f1b547d0aa5a9551d9c023708`, `history.js`: generated replies
retain approximate spoken/unspoken annotations, while barge-in stops the speech
pipeline and listening continues. Kinaiya preserves that generated-versus-delivered
distinction through native receipt qualifications. No private code/content is
copied. That prototype comparison does not establish semantic WAIT/IGNORE;
its endpoint used fixed silence, and no exact spoken prefix is inferred here.

## Prompt representation and bounds

`WindowConverseController._prepare` retains at most eight prior messages, each
at the existing 2,000-character cap, and the unchanged current user hypothesis.
Delivery state never substitutes an estimated spoken prefix for generated text.
Full source text remains in the journal. If assistant history is included, one
system message headed `Native playback context v1` maps its zero-based
`history_index` to the prior conversation messages (excluding system/current
user messages). Model/user text is never parsed as native delivery metadata.

The block contains at most eight rows and 8 KiB of UTF-8, including its fixed
instructions. A row records event/chunk/runtime/generation identity, latest
valid receipt status/sequence, up to three valid receipts in native sequence
order, an independently retained prior `played_report_sequence`, and an optional
separate `generation_stop` with `status=stop_requested`. No valid receipt means
`delivery_unknown`, not unplayed or heard. A status such as `played` is a client
report, not a physical hearing measurement. A later stop never erases that
observed prior played report; inconsistent reports remain visible as reports.

Receipt details carry only whitelisted status/alignment, played-seconds estimate
and an explicitly supplied fractional estimate. Fractions are never calculated
from time or text, and no exact heard words are inferred. Identifiers are limited
to 128 ASCII identifier characters. Generation/sequence values must be strict
integers in 1..9,007,199,254,740,991, matching the JSON/web-safe boundary; stop
source generation may be zero. Boolean counters/estimates are rejected. Seconds
must be finite and within 0..86,400; fractions must be finite in 0..1. These are
conservative context-consumer limits, not changes to canonical receipts.

If the metadata exceeds its byte budget, optional receipt details are dropped
before identity/status. A final minimal representation keeps an unknown
qualification for every included assistant and preserves any observed prior
played-report sequence. Details omitted for size are explicitly marked. Nothing
edits the journal or represents missing observations as evidence of non-delivery.

## Native provenance and precedence

Only current session/thread/epoch entries participate. Assistant text originates
from Converse. Per-chunk receipts require `delivery.updated`, `origin=converse`,
exact causal/event/chunk/generation binding, known status, and
`alignment=conservative_estimate`. The existing authenticated
`acknowledge_delivery` writer validates speech ownership before journaling. Its
payload has no runtime ID: runtime binding comes from the immutable referenced
assistant event. An optional conflicting runtime ID is refused. Generic owner
receipts, malformed/hybrid records and unrelated references cannot qualify text.

Generation controls require the native `generation_stop` discriminator,
`interrupted`/`delivery_unknown`, explicit matching assistant runtime, matching
native scope, valid causal shape when source identity is present, and a target
strictly greater than both the control's previous and assistant's generation.
They are separate server instructions, not per-chunk playback acknowledgments.
A control may precede a late retained obsolete chunk. Controls from another
runtime do not qualify historical assistant text; absent compatible receipts,
that text remains unknown even across restart.

Original message disclosure is a floor. A receipt/revision cannot make a private
source public. A receipt with a separately denied explicit scope is ignored.
Only whitelisted metadata for included assistant entries reaches the model;
unrelated source text, receipt payload fields and control source IDs are omitted.
These facts grant no execution authority and do not alter independent Agent work.

## One bounded snapshot and revisions

The helper consumes the existing unfiltered `recent(limit=100)` native suffix;
it adds no journal queries. Base history entries must precede the current source,
but later corrections and receipts in the full suffix still qualify them.
If a base message is in this contiguous suffix, its later revisions through that
snapshot are also present. User revisions fold in native sequence order against
their exact target and next revision. Only corrected text is used; unrelated
payload fields cannot overwrite source identity or widen original/intervening
disclosure. Assistant revisions are forbidden by the existing Store contract.
An invalid base revision or any unprocessable revision chain permanently omits
that user message from this prompt. Refusing a malformed correction must never
restore permission to export an older public draft. Denied or malformed scope
is likewise sticky; a later public label cannot untaint the chain. Canonical
journal acceptance/resolution stays unchanged, and counters are never coerced
to recover prompt permission.

Window hypotheses deduplicate by `(window_index, revision, sequence)` before
disclosure filtering. A later denied hypothesis cannot fall back to an older
public version. An older-index correction cannot displace a newer-index
hypothesis. The current utterance remains excluded from prior history. Evidence
outside the bounded suffix is unavailable and remains unknown; this is not an
all-history delivery projection or an atomic snapshot of future events.

## Verification and retained limits

The original local/authenticated-HTTP next-provider-prompt regression failed both
cases on integration base `c1eaed35`: no delivery context was supplied. It passed
after the repair. Native journal/provider-capture tests cover unknown, played,
interrupted and multiple chunks; receipts committed after current input; prior
completion plus later stop/reports; malformed/foreign/hybrid receipts; forged
text; reset; revision/disclosure ordering; and history/metadata/query bounds.
The fixtures generate deterministic text and mocked native HTTP, not model or
physical audio evidence. Final check results are recorded with the commit.

The combined delivery/recovery/speech/cancellation/window-HTTP/provider scope
passed 305 tests on Python 3.12.3 (130.06 seconds) and 305 on Python 3.11.15
(130.98 seconds), with imports verified to this isolated worktree. Two additional
private-assistant/public-receipt disclosure cases passed on both versions
(1.22/1.25 seconds); they add coverage without changing implementation. Ruff
lint/format passed for all 210 Python files; contract export left schemas
unchanged and the diff check passed. Independent review and full integration
CI remain separate checks.

Independent review at `79c9eff` then established a disclosure fallback: the Store
accepts legacy base revisions `true`, `1.0` and `0` plus their numeric successors,
but the strict context fold skipped private corrections and retained the older
public draft. Six of eight independent local/HTTP cases forwarded stale text;
the two valid-integer controls omitted it. The original result artifact SHA-256
is `77668d1d93a7bb199ed1160a9b8e42ef725a1c7ae6566e4ff3b92e4603578fa4`.
New regressions use actual owner-authenticated HTTP writes as well as local
journals, cover those bases and a valid base followed by `2.0` or a JSON-unsafe
counter, and test later public labels after malformed/denied scopes. Before the
sticky omission repair, 20 cases failed and 12 scope controls passed (7.59 s).
All 32 passed afterward (7.07 s), preserving the original no-export assertions.
The full focused scope then passed 339 tests on Python 3.12.3 (137.58 s) and
339 on Python 3.11.15 (138.33 s), with no warnings summary and worktree imports
verified. Retained combined log SHA-256 values are
`ed88504717e477cc46b048f601b78ef8cc1fde241096fcf6943d2f3c25a5ee2d`
(3.12) and `7744197e224699159a10af06ff740bbd364fde48a70ff940c083415c0ee02c8d`
(3.11), under `/tmp/kinaiya-77-revision-repair-effpy9ge`. The red and repaired
32-case logs are retained there as well. Ruff lint/format, unchanged schema
export and diff checks passed after the repair.

Typed Converse scheduling, Decisions context/contracts, native stop ownership,
canonical receipt schemas and #79 durable generation discovery remain separate.
Existing recovery/interruption/provider regressions must continue to pass.

Final independent review of `387aa9da` passed 168 cases (113 repository and 55 independent metadata cases) in 39.01 seconds. Its byte-identical original eight-case native prompt probe forwarded zero stale drafts after repair. One wrapper-induced pre-imported-anyio pytest warning is disclosed. Lead integration with the merged typed cleanup passed 336 cases in 127.47 seconds; the helper/tests match the reviewed worker and all nine typed/window readiness lines remain. Exact-head CI is required before merge. The [verification record](../../evaluation/evidence/converse-delivery-context-2026-10-28.json) preserves commands, hashes, failures and limits, including separate pre-existing typed-history issue #81.
