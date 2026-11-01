# 0103: Inspect and revoke exact unspent authorizations

November 1, 2026. A bounded increment for Options [#208](https://github.com/rgoza1996/Kinaiya/issues/208).

## Authority and boundary

Permissions now embeds Authorization activity. Broad service grants are a
separate summary from exact one-use owner reviews. The summary reports the
configured write/shell/network/YOLO settings and workspace availability. It
does not attest that an operation's isolation has run, widen scoped rules,
grant filesystem access, or replace the execution/settings owners.

The activity inventory covers native owner-review permits with retained
identity. States are requested, approved_unspent, used, expired, revoked,
reissued and retired. Used means the trusted broker spent the permit at its
admission boundary, not that an external effect completed. Missing/pruned
records are not evidence of cancellation or of earlier effects.
Inventory expiry is display-only integer Unix seconds rounded down; the broker
retains its exact wall-clock expiration and monotonic deadline.

Each review captures the native approval/task/session/thread/epoch/objective,
tool and normalized action digest. Additional SHA256 values bind the exact
original arguments and authenticated source/revision, selected tool/rule,
harness and pathless workspace metadata. No arguments, transcript, objective,
credential or filesystem path is added to this inventory or its receipts.
Renewal preserves this original binding with a new unapproved permit ID.

The core broker now maintains a monotonically increasing per-permit revision
and separate owner-reviewed bit. Owner confirmation, native spend and native
retirement update this revision. Revocation compares the exact original
binding, broker instance, expected revision and approved_unspent state under
the task's existing approval lock, then performs a synchronous one-way CAS.
The final effect broker still checks/spends the same permit. There is no await
between the final revoke check and native retirement, so a queued operation
either spends first or observes retirement. Revocation does not cancel an
already admitted operation, kill a process, recall a disclosure, or rerun
inference. Hard Deny, source/currentness, isolation and disclosure ceilings
are unchanged. Deny/expiry/renewal remain with their existing owners.

## Owner API and exact recovery

All Agent routes require owner authentication and return `Cache-Control:no-store`:

- `GET /v1/agent/options/authorizations?after=<approval_id>&limit=50`
  returns `schema_version`, `broker_instance`, `broad_grants`, bounded
  `authorizations`, `next_after` and `limits`.
- `POST /v1/agent/options/authorizations/revoke` accepts strict
  `AuthorizationRevoke {schema_version,broker_instance,idempotency_key,
  identity,expected_revision,expected_state:approved_unspent}`. It returns
  the original request, `status:revoked|not_revoked`, historical
  `observed_state`, native `revision` and independent `journal_status`.
- `POST /v1/agent/options/authorizations/revocations/lookup` accepts the
  **same original request** and reads retained native receipt or exact
  positive Session proof. It never retires a permit or retries publication.
  `unobserved` means unknown, including missing/pruned metadata.

Bodies are bounded to 8 KiB with one absolute five-second read deadline.
The broker has 1024 live/retained permits and 256 closed records. There are
at most 10000 revocation keys/receipts per native broker and 32 actual
metadata/publication tasks. Keys are not evicted/reused to admit a new
revocation. A full owner refuses new operations. Permits and their receipt
cache remain process-local; no historical receipt can recreate one.

Before native CAS, a deterministic `approval.resolved` AppendRequest is
frozen from the original request alone. Its metadata is local_only and its
key hashes the full original body. Native retirement and journal confirmation
are separate facts. Lost or refused append ACK never reverses retirement.
An exact repeated revoke may retry only that frozen append after its previous
actual task has settled. At most one actual append task per original request
exists. Metadata tasks stay owned after HTTP disconnect, enter workspace and
runtime-settled gates, and keep the shared journal alive through shutdown.

Agent-only `POST /v1/transcript/authorization-revocations/lookup` looks up only
the exact original Agent writer/key. It validates bounded stored request and
entry, complete original AppendRequest projection, native agent-supervisor
origin, event identity and sequence. It returns redacted positive metadata.
The strict existing submit exchange owns bounded transport/response cleanup.
A matching committed event can prove a **historical** revoke after broker
restart/RAM loss. A different current broker forbids a new CAS; historical
proof neither grants its current permits nor proves old worker cancellation.
No missing-row, revision or instance observation is an absence fence here.
Generic journal append parsing/transport behavior is unchanged; exact
projection verification and positive-only lookup narrow the new claim.

## Flutter and evidence

The embedded panel retains at most 16 exact outstanding requests in RAM,
matched to Agent/Session endpoint and owner. Closing the view or reconnecting
to the same owner preserves them. Check reads the exact receipt first, then
can retry only the unchanged original request at its original broker. A new
broker can supply historical positive proof, but never a new revoke of an
old permit. Unknown remains inspectable; disappearance is not settlement.
Other permits remain usable. First dispatch fences the rendered row/page;
late replies cannot alter a replacement connection. Native revocation and
journal-confirmation notices are distinct. No credentials or requests are
saved in preferences or logs.

This increment does not implement general workspace-read rules, per-task
network transport policy, exact plugin rules, or retained-context erasure.
Those remain separate Options work. No tests, live calls, model/provider
calls, devices or credential probes were run for this increment. Static
checks/schema export and independent review are reported separately from
future CI/runtime qualification.

Source evidence: native and Flutter independent review found no remaining blocking
findings after exact first-dispatch fencing, integral display expiry and permit
registration cleanup repairs. Native Ruff/format/AST, five schema exports and full
Flutter analysis passed. CI and preview qualification remain pending.
