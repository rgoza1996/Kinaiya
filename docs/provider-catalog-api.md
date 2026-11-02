# Owner provider catalog

Version `1.0`, October 31, 2026. [ADR0086](adr/0086-owner-provider-catalog.md),
[#208](https://github.com/rgoza1996/Kinaiya/issues/208). This protected catalog
stores declarations and credential generations. Saving a profile does not
apply it to a role, load a model, contact an endpoint or authorize disclosure.

## Independent service availability

The Session, Agent, Converse and Decisions launchers expose the same catalog
under `/v1/providers`, using separate connections to protected
`<data-dir>/providers.sqlite`. Every route requires the owner bearer and an
allowed browser origin and returns `Cache-Control: no-store`. Service/model
credentials cannot manage it. The Flutter editor uses the Session root so
turning Agent or Converse off does not remove the editor.
An unavailable/corrupt optional catalog exposes a fixed `503` without taking
the journal or independent host-configured runtime offline.

The database must be an owner-only regular file, outside the worker workspace,
with no path alias and an owner-controlled parent. It stores secrets in native
storage with mode `0600`; it is not an encrypted external secret manager.
Host-managed backups must protect this database as credential material.

## Inspection

`GET /v1/providers` returns a transactionally coherent `schema_version`,
`revision`, `profiles`, `credentials`, `known_references`, `limits` and explicit
change/reference boundaries. Profile metadata contains a stable ID, immutable
integer generation, configuration SHA256, label, removal state, non-secret
configuration and declared interface capabilities. Credential metadata contains
an ID, opaque generation, registered source/reference and removal state.

`credential_available` and credential `available` mean only that the declared
stored generation is available for selection. They do not prove endpoint
authentication or readiness. Capabilities remain `qualification: not_checked`
and `readiness: unknown`. Inspection never reads environment values, key files
or model endpoints. It never returns raw credentials, credential hashes or
filesystem paths to keys.

## Exact catalog changes

`POST /v1/providers/changes` accepts strict `schema_version: "1.0"`,
`expected_revision`, unique `idempotency_key`, `operation` and only its fields:

| Operation | Fields |
| --- | --- |
| `profile_upsert` | `profile_id`, `label`, typed `config` |
| `profile_remove` | `profile_id` |
| `credential_import` | `credential_id`, `key` |
| `credential_reference` | `credential_id`, `source`, optional `reference` only for `host_env` |
| `credential_remove` | `credential_id` |

Native discriminated configs cover Meta, local native LM Studio,
OpenAI-compatible text, Laya and native d1 classification, and the independent
Meta STT/OpenRouter TTS declarations. Kind-specific fields and bounds
are defined by `ProviderChange` and `ProfileConfig` in
`services/settings/provider_catalog.py`. Endpoints reject embedded credentials,
query/fragment, and unapproved external origins; remote endpoints require HTTPS
and explicit cloud permission. LM Studio is loopback-only. Response reserve
must fit any declared context. Laya uses its classification fields rather than
conversational reasoning controls.

The [native d1 declaration](adr/0106-native-d1-decisions.md) is Decisions-only,
loopback-only and text-only in this increment. It requires a server-root URL with
an explicit port and an owner-entered minimum confidence. It has no output,
reasoning, Laya artifact/token-window or cloud controls. Exact catalog selection
and protocol identity are distinct from runtime/model qualification; declaring
an Omni model does not enable audio capture or native speech output.

Known reference sources are `muse`, `openrouter`, or an alias declared in host
`provider_credential_references: {"alias": "ENVIRONMENT_VARIABLE"}`. A request
cannot provide an arbitrary key-file path or environment name. Explicit
registration/rotation snapshots the current key into a new immutable native
generation and retires earlier generations for new selection; later external
changes do not retarget it. Profiles bound to a retired credential must be
explicitly updated. GET never performs this
registration. An imported key must be a bounded single ASCII token (at most
16384 bytes). The router bounds JSON at 65536 bytes, depth 32 and 4096 nodes,
rejects duplicate keys/nonfinite numbers, and uses fixed validation errors so
invalid secret input is never echoed by ordinary framework validation.

One SQLite transaction binds CAS, credential/profile generation, revision and
receipt. Identical key/body replay returns the historical receipt; a conflicting
body or stale new revision returns `409`. A keyed HMAC kept in protected storage
compares exact requests without publishing a guessable credential digest.
The response says `status: catalog_saved`, saved revision, operation/key,
non-secret result and `change_boundary: role_owner`. It is not active-role proof.

`GET /v1/providers/changes/{key}` returns that redacted historical receipt. `404`
means no receipt is visible yet, not that a delayed POST failed. Because a GET
receipt deliberately cannot prove secret-body equality, the owner UI reconciles
credential imports/reference registrations by explicit identical POST replay.
No automatic retry occurs. Transport failures retain the bounded exact request
in endpoint/owner-bound RAM until confirmation or definite initial refusal;
application restart loses that recovery. Secrets never enter durable client
preferences or diagnostics.

## Bounds and removal

At most 32 profiles, 64 generations per profile, 32 credentials, 32 generations
per credential and 10000 operation receipts are retained. Bounds refuse further
changes instead of silently evicting pinned evidence. Removing a profile or
credential is a tombstone for new selection. It is not secure erasure and does
not rewrite historical versions or a previously captured adapter's key. Roles
must still enforce their own generation pin, task admission, disclosure,
resource ownership and cleanup boundary before applying these declarations.
Explicit historical lookups require the native `for_new=False` path; new
selection always requires the current active generation. Removal followed by
replacement cannot resurrect retired versions. Stored blobs, strings and JSON
are bounded before allocation and revalidated before factory or API use.
