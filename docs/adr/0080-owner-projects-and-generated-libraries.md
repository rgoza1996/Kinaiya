# ADR 0080: Owner Projects and generated output libraries

Status: Accepted implementation; part of #196.

## Product behavior

The sidebar replaces Files with **Generated Documents** and labels the media
destination **Generated Media**. Documents and media list actual native Agent
write receipts, including name, file type, creation time and source Agent session.
There is no general filesystem browser, directory scan or sample user library.
File type describes the recorded filename extension, not a content validation.
Each write is a separate recorded output version. The creation time is the
native write-receipt time, displayed in local device time. The record is historic;
it does not claim that those bytes still exist at the original path.

Projects are owner labels grouping Agent sessions. Create a project, then add or
remove the currently selected Agent session. Opening an associated session uses
the existing Agent session-selection boundary. A project grants no file access,
does not choose a worker directory or model, and does not start tasks.

## Native contract

All routes require owner authentication:

- `GET /v1/agent/library?kind=documents|media&after=0`: scan at most 100 native
  `agent.tool` journal records after the cursor. Return schema 1.0, kind, items
  and next_cursor (last scanned sequence, even when no matching output exists).
  Null means the scan found no further records. Each item includes event/sequence,
  relative path/name/type, created_at, SHA256, task/session/thread/epoch and
  objective revision. Only successful `write_file` receipts from
  `agent-supervisor` with native file evidence and a workspace-relative path
  qualify. Failed writes, read evidence, external tool assertions and arbitrary
  model text do not qualify. No file content is opened or returned.
- `GET /v1/agent/projects?after=<id>` returns up to 20 projects and next_cursor.
  Project metadata contains schema 1.0, project_id, name, UTC created_at, revision
  and session_ids. At most 500 projects and 200 sessions per project.
- `POST /v1/agent/projects` accepts strict schema 1.0, idempotency_key and
  operation. `create` requires name (1–120 readable characters), optionally an
  existing Agent session_id. `attach`/`detach` require project_id,
  expected_revision and Agent session_id. No path or permission fields exist.
  The owner journal validates session identity before a new association.
- `GET /v1/agent/projects/changes/{key}` retrieves a persisted receipt; 404 means
  no committed receipt is visible, not proof that a prior write cannot commit.

Project state and exact key/body receipts commit in one SQLite transaction.
Same key/body returns the original result after restart. Stale revisions and
key/body conflicts return 409, invalid selection 400, malformed wire 422,
unavailable storage 503. No automatic write retry. Unconfirmed client writes
remain visible across page close/reopen; explicit retry reuses the same body/key.
Storage is private `projects.sqlite` outside the worker workspace.

## Evidence boundaries

Synthetic API/store tests cover authentication, persisted receipts, revision
conflicts, valid Agent association and rejection of foreign/path-traversing
output receipts. Flutter checks cover strict parsing, explicit project creation,
narrow layouts and navigation while existing capture stays armed. These do not
qualify external media generators, general document editors, filesystem download
links, native mobile capture, or the multimodal model work tracked in #196.
