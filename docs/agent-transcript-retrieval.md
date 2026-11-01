# Agent transcript retrieval

Decisions selects relevant conversational sections and references for Agent's
immediate attention. The complete transcript persists independently, including
ignored and unselected windows. Agent retrieves additional authorized history
when needed; routing does not decide what history is retained. This implements
the two paths in [Roadmap §5](../Roadmap.md#5-shared-transcript-as-common-state)
and [clarification §10.3](../clarification.md#103-full-transcript-is-preserved-and-accessible-to-agent).

This document describes the bounded #255 implementation on the integration
branch. It does not establish a merged release, model-quality result, unlimited
per-task context, Omni transport, or completion of M3.

## Model-facing pages

Prefer `read_transcript` with `paged: true`. A single reference can select its
current resolved view or its historical immutable row:

```json
{
  "paged": true,
  "event_ids": ["synthetic-source-id"],
  "entry_view": "resolved",
  "offset": 0,
  "max_chars": 2000
}
```

Each entry includes the actual ID, kind, origin, sequence, revision, timestamp,
complete payload metadata except top-level text, selected view, full-text and
full-entry hashes, offset, total character count, `next_offset` and `truncated`.
An absent text field projects to empty text without inventing speech. Explicit
nonstring text or unavailable metadata refuses.

When `next_offset` is nonnull, set the next request's `offset` to that value.
Keep the same event ID and `entry_view`, and carry the returned revision as
`expected_revision`, full `entry_sha256` as `expected_entry_sha256`, and the
envelope's `scope_token`. These bind the freshly read view. A changed resolved
revision or digest refuses the old continuation. Immutable text remains an
explicitly historical view, subject to current provenance and disclosure.
Offsets count Unicode code points, not UTF-8 bytes.

Discover retained immutable rows with:

```json
{"paged": true, "after": 0, "limit": 8}
```

The first native scan captures a real journal highwater. While `scan_exhausted`
is false, set the next request's `after` to the returned `next_after`, carrying
the fixed `through_sequence` and returned `scope_token`. Later appends do not
extend the existing scan. `next_offset` and `next_after` are response fields,
not accepted request keys.
For an entry with more text, continue separately by its ID with
`entry_view: "immutable"` and its returned text-continuation fields.

An empty authorized page does not imply the journal ended: denied entries can
produce an empty, nonexhausted page with an advancing cursor. A byte-limited
prefix leaves the cursor before the next unreturned permitted entry. Metadata
that cannot fit by itself refuses explicitly. Text completion and discovery
exhaustion are separate facts; the scan fields are explicitly null in ID mode.

The response contains at most 12,000 text characters and 131,072 UTF-8 JSON
bytes. `max_chars` defaults to 2,000 and is bounded to 1–12,000. Discovery accepts
1–20 rows, defaulting to eight. ID mode accepts exactly one reference. Argument
shape, unsafe counters, invalid Unicode and inconsistent continuation fields
are checked in trusted code.

## Scope and authority

The continuity token hashes the native task/objective revision, session,
thread, epoch, complete provider metadata and accumulated disclosure floor.
It is not an authorization credential. A permitted newly read dependency can
tighten the floor and change the returned token; the next page uses that token.
The actual accepted adapter is captured and checked across asynchronous reads.
Changing an owner default does not replace an accepted task's pinned adapter.

Current source revisions and their full bounded causal closure remain required.
Retrieved references persist as disclosure dependencies after prompt eviction;
they never become user instructions, objective sources or tool grants. The
existing limits remain 128 retained context roots and 256 provenance nodes.
Repeated slices reuse a root. Exhausting capacity refuses explicitly rather
than dropping earlier IDs, claiming false exhaustion or silently pruning history.
This increment does not provide arbitrary cumulative unlimited history in one
task or implement a new retention/deletion policy.

Before publication, a final guarded closure includes old and prospective roots
after earlier task, view and session reads. Selected raw/resolved views are
checked inside that pass; inconsistent duplicate legacy views refuse. New roots
are adopted only after disclosure checks. This is a bounded asynchronous
observation, not a globally atomic publication guarantee. Actual provider
uploads still require the existing native export admission, including later
corrections or disclosure withdrawal. Source/session reset, provider replacement
and stale task state cannot create a new disclosure grant.

## Native transport

`Store`, `LocalJournal` and `JournalClient` share `transcript_scan(request)`.
`POST /v1/transcript/scan` is Agent-only and accepts identity UTF-8 JSON without
query arguments or private capability headers. The strict versioned request and
page packets have 8,192-byte and 262,144-byte wire limits. Their codecs check
complete native row metadata, exact counters and identity, duplicate keys,
finite JSON, depth 512, 262,144 graph nodes/keys and raw/canonical byte limits.
Lexical depth checks run
before recursive JSON parsing. Schemas describe structural fields; native
complete-graph, request-binding and cross-field checks remain mandatory.

The Store observes current session scope, actual highwater, rows and lookahead
inside one SQLite read snapshot under its real lock. It never commits or rolls
back a foreign already-active transaction. Only its own read transaction can be
settled; uncertain cleanup leaves the scan unavailable.

The endpoint shares Session's actual 64-operation registry, 25-second request
deadline and five-second ingress limit. Shutdown fences admission and joins
retained native work. The client shares its existing 64-exchange ownership and
sole response close, with a 25-second total deadline, bounded streaming reads,
full original-request comparison, no redirects/compression and no automatic
retry. Cancellation or a deadline does not prove actual cleanup. Pending owners
remain retained and close uncertainty remains sticky. Responses and errors use
`Cache-Control: no-store`; malformed/media/conflict/unavailable outcomes remain
distinct from authentication refusals.

## Compatibility and verification

Legacy calls still return lists. Short entries keep their earlier shape. Clipped
entries explicitly name their view, truncation, total length and a hint to use
paged retrieval. A legacy aggregate that would silently omit another readable
entry instead refuses with a paging hint. New continuation arguments require
`paged: true`.

[Verification evidence](../evaluation/evidence/agent-transcript-paging-2026-11-01.json)
binds the exact integration source, tests and preserved failure artifacts. The
first 168-case source run produced 160 passes and eight failures. Two exposed
typed-child normalization defects. Six independently reviewed fixture errors
concerned the accepted provider versus owner default, malformed command setup,
strict speech-window shape and actual close/callback settlement. Their test-only
correction passed 14 targeted cases against the original source while preserving
the two genuine failures. A separate formatter change preserved the complete
module AST. The original tests, hashes and failed logs remain retained.

Additional frozen regressions captured parser-entry ordering, late page floors
and inconsistent duplicate legacy views before their respective repairs. The
late-floor cases already received native export refusals; they do not establish
external disclosure. The final scoped runs passed 595 codec and tool-contract cases
at `16ec2b4` and 182 transcript/runtime cases at `ff02f0c`, with 92 cases shared.

The separate old-regression run at `166bee3` remains 292 passed and four failed.
Follow-ups passed the four cases plainly on both versions and all 144 cases in
the relevant three-module order. One instrumented pre-feature case spent 2.075
seconds before actual close and reproduced the same two-second wait failure;
all measured actual closes joined exactly once. The original larger process's
trigger remains unresolved. No timeout or assertion was relaxed, and no overall
regression pass is claimed.

The evidence covers local SQLite/ASGI/client/runtime behavior with synthetic
measured providers. It adds two schemas while preserving 190 earlier schemas.
It does not qualify main, real inference, physical media, Omni transport,
unlimited per-task history or a globally atomic page-publication operation.
