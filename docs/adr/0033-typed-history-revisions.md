# ADR 0033: project native revisions before selecting typed history

Tracks [#81](https://github.com/rgoza1996/Kinaiya/issues/81). A typed Converse
turn previously selected raw `conversation.user` payloads from the recent
journal and checked only the original payload's disclosure scope. A native
revision could make a public draft local-only while the next typed provider
still received that stale draft. Canonical `Store.resolve` already returned
the private correction; the prompt consumer had not applied it.

## One snapshot, shared revision rules

`services/converse/history.py` extracts the existing live-window native suffix
and user revision fold introduced and repaired for #77. Both consumers use
the existing contiguous `recent(limit=100)` suffix, sliced before scope
filtering and sorted in native sequence order. The journal's existing suffix
byte limit also remains in effect. There is no extra query, history paging,
per-message resolve, migration or prompt-provider call.

Only the exact session/thread/epoch participates. User bases originate from
`authenticated-user`; revisions originate from `authenticated-user` or
`converse`, target a captured base, follow its original sequence, and advance
its revision by exactly one. Revisions must be strict integers in
1..9,007,199,254,740,991 and carry string text. Only corrected text replaces
the original text; source identity and the original message position remain.
Other component origins and orphan revisions cannot supply history.

The original and every intervening explicit disclosure scope form a floor.
A denied or malformed scope, invalid base revision, or unprocessable targeted
revision permanently omits that user message from this prompt. Later public
labels cannot untaint it, and rejection cannot fall back to the stale draft.
Some legacy boolean/float counters remain accepted by canonical journal
validation/resolution. The prompt projection does not coerce them or change
canonical storage; it omits their malformed chains.

The shared fold takes a disclosure predicate so the callers retain their
established consent behavior. Live-window history keeps its strict policy for
missing scope. Typed history keeps `entry_allowed`'s legacy unscoped consent
path. A present non-string scope is denied before that policy's membership
lookup; unknown string scopes are also denied. Recognized local/cloud and
Contributor disclosure rules are unchanged. This is not a new authorization
or consent mechanism.

## Typed integration and preserved behavior

`typed_history` folds revisions before merging permitted user and native
Converse assistant messages in their original sequence order, then selects
the existing `context_turns` suffix. It retains full typed-history text, the
default twelve turns and configured turn counts. It does not import the live
window's eight-row or 2,000-character limits. Live-window message and delivery
metadata bounds remain eight rows and 8 KiB, with the unchanged window tests.

The runtime changes only provider-history construction. Its raw `existing`
snapshot still determines idempotency/replay and admission. The current user
input, typed delivery-note block, native appends, generation and stop
ownership, provider cleanup and independent Agent controls are unchanged.

## Verification and limits

The original public synthetic probe was retained byte-for-byte (SHA-256
`26cc035710b4c51744c68997b4d3700a3b823162c8c5ad8e7f392e3e044acd62`).
On approved base `dad69559f49aad81f4008e53880bb8f1d6b98502`, both LocalJournal
and owner/Converse-authenticated HTTP journal cases forwarded the stale public
draft to a recording provider with Contributor metadata. Canonical native
resolution returned the local-only correction. After this change the same
probe forwarded neither the stale draft nor the denied correction in both
cases. All data, credentials and provider replies were synthetic; no external
model, Contributor endpoint or live service was contacted.

New tests inspect actual typed `provider.stream` prompts using native local
and authenticated HTTP journals. They cover all recognized scope pairs for
local, Contributor-class and ordinary cloud metadata; missing-scope legacy
consent; consecutive corrections; malformed bases, revisions and scopes;
later public labels; ordering, full text and configured turn counts; reset;
bounded/orphan history; one existing suffix read without resolve; raw replay
identity; and unchanged delivery notes. Pure projection cases additionally
cover foreign scope/origin, sequence order and malformed native-shaped data
that the current append contract would reject. Such unit cases are not native
writer or real-provider evidence. Results and exact commands are recorded in
`evaluation/evidence/typed-history-revisions-2026-10-28.json`.

The new projection/typed-prompt tests and unchanged #77 delivery suite passed
301 cases on Python 3.12.3 (49.58 seconds). The combined history, typed cleanup,
window recovery, speech, cancellation, window HTTP, owner controls, session and
provider scope passed 618 cases on Python 3.12.3 (201.80 seconds) and 618 on
Python 3.11.15 (203.57 seconds), with imports verified to this worktree. Each
combined run reported the existing Starlette/httpx deprecation warning. All
174 Python files passed Ruff lint/format; contract schema equality and
`git diff --check` passed. The #75 recovery, #76 typed cleanup, #77 delivery
tests and global disclosure policy remain byte-identical to the approved base.

This is a bounded snapshot projection, not an all-history read or an atomic
promise about corrections committed after capture. A base outside the suffix
is not refilled from older history. Existing typed unscoped consent and its
delivery-note representation remain separate policy/design limits. Deterministic
prompt capture proves the text offered to these fixtures, not external model
behavior, physical audio, full M2 acceptance or task completion. Independent
review and exact-head integration CI are separate gates.

Independent final review at `ea313330` found no actionable issue: 599 cases passed in 67.11 seconds without warnings, including 376 repository cases and 223 independent policy/equivalence/metadata cases. The unchanged original two-journal probe forwarded zero stale drafts/private corrections. A reviewer-only invalid SpeechWindow fixture and its correction are retained in the review manifest. Lead integration with native generation recovery and typed cleanup passed 637 cases in 144.32 seconds with the existing TestClient warning. Helper/new-test bytes match the reviewed worker, and runtime AST outside history construction/imports is unchanged from corrected #79. The verification record preserves commands, hashes, original failures and policy limits. Dependency and final exact-head CI remain required before merge.

Dependency #83 passed final CI40741559469 (1,694 tests on each supported Python, Flutter/JS/build/schema/audit) and merged as `86b88b3c`. Merging that main left the typed-history integration source tree byte-identical to its tested snapshot, including the preserved native counter imports. This change still requires its own exact-head CI.
