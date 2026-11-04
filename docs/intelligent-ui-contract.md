# Intelligent UI v1 — bounded Converse artifacts

Issue #165 implements a bounded part of Roadmap §18. Agent generates the data;
Converse displays it. This does not establish external-Agent portability,
background subscriptions, arbitrary media viewers or mobile call media.

## Wire shape and limits

An artifact envelope contains `schema_version: "1.0"`, server-generated
`artifact_id`, `session_id`, `thread_id`, `epoch`, `task_id`,
`objective_revision`, integer `revision` (starts at 1), `title`, `summary`,
`components`, `created_at`, `updated_at` (Unix seconds), `content_sha256`,
`content_scope`, `view_state` (component ID to scalar value), and
`view_revision` (starts at 0). Metadata and completion receipts are native,
never accepted from a model. All objects forbid undeclared fields.
The owner API adds optional native `task_submit_sequence` (integer ≥0, default 0
for older/historical responses or unavailable tasks), recovered from the
authenticated task-submit journal receipt. It fences automatic display ordering,
is excluded from stored content and its digest, and is never accepted from model
publication arguments.

Publication: `publish_ui` arguments for creation are exactly `title` (1–160
characters), `summary` (1–2000), `components` (1–24). An optional `layout` is
`stack` (default) or `grid`. Publication is limited to 48 KiB UTF-8 JSON.
Updates instead contain `artifact_id`, `expected_revision`, `updates`
(1–24 objects containing `id` and complete replacement `data`). Only existing
component IDs can be patched; type/title and native task/session identity remain
fixed. The caller must still be the original task at the current objective
revision. A completed/old task cannot publish a late replacement.
A changed task disclosure scope requires a new artifact; it cannot reuse the
previous artifact's content label for different-scope data.

Components contain `id` (1–64 characters matching `[A-Za-z][A-Za-z0-9_-]*`),
`type`, `title` (at most 160 characters, default empty), and `data`.
IDs are unique. Supported data objects:

| type | data (all fields required unless stated) |
| --- | --- |
| `text` | `text` (1–8000 characters) |
| `status` | `text` (1–1000), optional `tone`: `neutral` / `success` / `warning` / `error` |
| `table` | `columns` (1–12 nonempty strings, each ≤80), `rows` (≤100 rows, each exactly the column count, scalar cells), optional `selectable` boolean (default true) |
| `chart` | `labels` (1–100 strings ≤80), `values` (same length, finite numbers with absolute value ≤1e12), optional `unit` (≤40), optional `style`: `bar` / `line` (default bar) |
| `diagram` | `nodes` (1–30 objects `{id,label,detail?}`; label ≤160, detail ≤1000), `edges` (≤60 objects `{from,to,label?}`; only known node IDs, labels ≤80) |
| `source` | `label` (1–160), `url` (≤2000, HTTPS only, no userinfo), optional `observed_at` (Unix seconds), `freshness`: `static` / `simulated` / `delayed` / `stale`; no automatic fetching |
| `number` | `label` (1–160), `value`, `min`, `max` (finite numbers ≤1e12 in absolute value; min ≤ value ≤ max), optional `step` (positive finite number) |
| `select` | `label` (1–160), `options` (1–30 unique nonempty strings ≤160), `value` (one option) |
| `ask` | `label` (1–160), `message` (1–2000); click only prefills Converse's ordinary composer, never submits or grants tool permission |
| `media` | only `asset_id` (`media_` plus 32 lowercase hexadecimal characters); at most one media component, backed by an exact native asset receipt described below |

Text rejects unpaired Unicode surrogates and C0/C1 controls except tab and LF.
JSON trees are bounded to 8192 visited keys/values/containers and depth 12,
including native envelope metadata. Oversized or deeper trees fail validation.
Diagram node IDs are nonempty strings up to 128 characters; component IDs retain
their stricter 64-character identifier rule. Identity fields are at most 128
characters. Epochs, revisions, cursors and source timestamps stay within the
web-safe integer magnitude (`9007199254740991`); timestamps may be fractional.

Table scalars are null, boolean, finite number (absolute value ≤1e12), or a
string ≤1000 characters. No arbitrary HTML, executable code, embedded webpage,
image fetch or client credential is accepted. Unknown types fail validation;
older clients show summary plus unsupported-content notice.

The strict Meta response transport uses nullable `arguments.ui_spec_json` to carry the complete create or update object as JSON text. Its logical text is at most 48 KiB UTF-8; unused transport fields must be null. The adapter rejects duplicate keys, non-finite numbers, invalid Unicode, extra native metadata and mixed create/update fields. Native storage performs the final component validation.

Rejected JSON/wrapper proposals can retain a constant decoding stage and reason
in the authenticated failed outcome, without raw model text or parser details.
The [diagnostic contract](ui-decode-diagnostics.md) preserves guard order, error
text and terminal failure; it adds no automatic repair, retry or completion.

After successful publication, Agent history retains a native receipt rather than
the original assistant payload. Its JSON is limited to 4096 UTF-8 bytes, with
artifact ID, content/objective revisions, digest and every component ID/type
preserved. Optional title/summary excerpts shrink to fit the escaped byte bound;
UI data is omitted. This prevents the ordinary observation limit from clipping
a large publication into malformed JSON. The receipt proves publication only.

## Persistence and API

Agent's owner-only API uses its existing bearer auth, redirect/CORS policy:

- `GET /v1/artifacts?session_id=...&thread_id=...&epoch=...&after=0&limit=20`
  returns `{items:[envelope],next_after:<integer>}`. Scope is required;
  limit 1–20. Stable ascending creation sequence, max 100 artifacts per task.
  The cursor is the final returned creation sequence even at the current end;
  an empty page preserves its incoming cursor. Content updates are retrieved
  by ID following `artifact.updated` journal references.
- `GET /v1/artifacts/{id}?session_id=...&thread_id=...&epoch=...` returns envelope;
  a wrong scope is not found.
- `POST /v1/artifacts/{id}/actions` accepts exactly `session_id`, `thread_id`,
  `epoch`, `expected_revision`, `component_id`, `value`, `idempotency_key`.
  It changes only validated presentation state. `table` accepts a row index or
  null, `diagram` a node ID, `number` an in-range finite number, `select` a known
  option. Other types reject actions. Responses return the updated envelope.
  Identical replay returns its original response; conflicting key reuse and
  stale content revision return conflict. Original replay remains valid after
  a later content revision; it does not reapply the old action. Tables marked
  `selectable: false` reject actions. No inferred authorization or model
  call follows an interaction.

Scope strings and action keys are bounded to 128 characters. Missing or malformed
HTTP fields return 422, native validation 400, absent/wrong-scope artifacts 404,
revision/idempotency/quota conflicts 409, and unconfigured or unavailable storage
503. Only the owner role may read or act; storage exception details are not
reflected. Storage bounds retained artifacts to 2500, action receipts to 128 per
artifact, and retained component/view/summary/action payloads to 64 MiB in UTF-8.
These are content quotas, separate from SQLite page and journal overhead.

Native storage uses bounded validation and atomic compare-and-swap revisions.
Its SQLite file lives in the operator's data directory, outside the Agent's
writable workspace. Publication returns native digest evidence; finish verifies
the cited artifact still matches the task/revision and receipt. `artifact.updated`
journal entries contain metadata references and summary, never large UI blobs.
Failed storage/journaling is visible; no success from a model claim alone.
Any uncertain storage, update-journal, tool-receipt or final-completion confirmation
stops that task without automatically creating another artifact. The local task
reports the fixed unconfirmed outcome; restart recovery uses authenticated durable
journal truth. An artifact receipt proves UI publication only. Publication cannot
clear a failed file/shell/MCP effect, and those effects cannot clear a failed
publication. Unfinished speech requires the same exact owner approval for
publication as other mutating actions, including under YOLO.
Converse accepts current-revision artifact references and digests only from an
authenticated supervisor success snapshot. When publication is the only native
effect proof, its completion notice uses fixed UI-publication-only wording and
omits the model's finish summary, including after plugin or MCP computation.
Publication revalidates captured source revisions after speech-authority reads,
then session identity after those source awaits, and discards local cancellation
or steering immediately before its synchronous storage transaction. These reads
form an admission snapshot, not a distributed transaction with future journal
changes. A later correction/reset can arrive after its relevant snapshot was
captured; stored effects are not rolled back. Owner reads remain bound to their
exact session/thread/epoch, so an old-epoch artifact cannot enter a new scope.

## Display ownership

Converse polls bounded artifact pages for its own authenticated session/thread
and epoch. Late results after reconnect/session changes are discarded. A new
artifact may take the main stage only when the current result is unpinned and
not being edited, and its known native task-submit sequence is at least the
current result's sequence. A late older task cannot take the stage; an unknown
historical task remains available through the manual chooser. Close retains the artifact in the result chooser; reopening
does not call a model. Unresolved presentation actions retain their exact private idempotency intent across matching-backend/scope reconnects, with at most 32 unresolved intents before new writes are refused; other hosts, credentials and epochs cannot inherit them. Persistent backend artifacts outlive the view/call.

The character remains at one element identity while animating to the lower-right
corner. The real transcript sits above the corner portrait and can collapse;
history and reachable text/voice/Stop controls retain their owner. Narrow screens
stack the artifact and rail. Existing motion/reduced-motion settings apply.
Disable rich rendering to retain summary/data fallback and ordinary controls.
Simulated data is labeled explicitly; source timestamps and staleness labels
must not be replaced with an invented live feed.

## Native video and SAM presentation

An optional `media` component has exactly `data: {"asset_id":"media_<32 lowercase hex>"}`.
At most one media component is admitted per artifact. No model path, URL,
HTML, upload bytes, provider, or overlay instructions are accepted. Ordinary
`publish_ui` remains the only generated publication route; the native store
checks the asset's exact task, objective revision, session, thread, epoch,
and disclosure scope on both creation and patches. A private asset cannot be
published using a public Task label. Native runtime publication projection
must already include the private consultation floor; the voice task's source
admission scope is not changed by this component.

Only trusted native source/projector wiring calls
`NativeMediaAssets(path).register(*, binding, content_scope, source_bytes,
source_sha256, rendered_mp4, overlay_sha256, width, height, duration_seconds,
fps_num, fps_den, frame_count)`. `binding` contains exactly `task_id`,
`objective_revision`, `session_id`, `thread_id`, and `epoch`. The same instance
is injected as `ArtifactStore(path, media_assets=instance)`. Artifact storage
does not close this shared registry; its native owner closes it after runtime
and adapter cleanup. No HTTP registration operation exists.

The protected registry retains immutable original/highlighted MP4 byte pairs,
each variant at most 32 MiB, at most 16 assets and 128 MiB combined bytes.
Identical native registration returns the original receipt, including after
restart; quota refusal does not evict existing receipts. It verifies original
SHA256 and computes rendered SHA256. Actual video decoding, validated SAM
masks, coordinate/timing interpretation, and rendered frame qualification are
the trusted projector's responsibilities. MP4 signatures alone do not prove
these properties.

The receipt contains `schema_version: "artifact_media.v1"`, opaque `asset_id`,
nested `binding`, `content_scope`, `source` and `highlighted` objects (each
exactly `sha256`, `bytes`, `mime_type: "video/mp4"`), `overlay_sha256`, `width`,
`height`, `duration_seconds`, `fps_num`, `fps_den`, `frame_count`, and
`publication_only: true`. It contains no owner credential or filesystem path.
The authenticated owner API adds:

- `GET /v1/artifacts/{artifact_id}/media/{asset_id}?session_id=...&thread_id=...&epoch=...`
  returns that metadata receipt.
- `GET /v1/artifacts/{artifact_id}/media/{asset_id}/content?session_id=...&thread_id=...&epoch=...&variant=source|highlighted`
  returns exact `video/mp4` bytes with `Cache-Control: no-store` and `nosniff`.

Both require the owner credential and the actual scoped artifact to contain
the asset reference. Knowing an asset ID never permits global lookup. Wrong
scope/reference returns 404, malformed query 422, and unavailable native
storage 503. No external redirect, remote source, capture, inference, task
submission, or permission grant follows playback. The compiled web player
fetches with its existing owner header, verifies byte count and SHA256, and
uses a local blob URL; it is initially muted and does not autoplay. This
presentation establishes neither transcription accuracy nor task success.

`tests/unit/test_native_media_assets.py` covers synthetic storage, scope,
publication, patch, quota, and in-process owner API behavior. These fixtures
contain only synthetic MP4 signature bytes and do not run a video decoder,
SAM, or any provider.
