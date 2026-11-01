# 0086: Protected provider declarations and credential generations

October 31, 2026. Implements a provider-management foundation for the full
[#208](https://github.com/rgoza1996/Kinaiya/issues/208) Options goal. See the
[API contract](../provider-catalog-api.md) for exact operations and limits.

## Decision

Replace the old read-only Providers cards with a traditional embedded catalog
and forms for profile creation/editing/removal and credential registration,
rotation/removal. Model/provisioning state remains distinct from declaration
state. A saved declaration neither starts a provider call nor changes a running
role. No fake sign-in, endpoint-test success, discovered modality or ready badge
is supplied. Meta sign-in/OAuth remains unfinished; owner API-key registration
is explicit. LM Studio uses its local native protocol. Compatible endpoints can
represent OpenRouter or owner-local text services without asserting capability.

The native catalog and credential vault share protected SQLite transactions,
immutable bounded versions and exact revision/idempotency receipts. Keys stay
in owner-only native storage; inspection returns opaque credential generations,
not key material or public key digests. Explicit host reference registration
snapshots keys once. Independent service launchers share the database but do
not require another role to be running to inspect/edit declarations. Their
existing lifespan owners remain responsible for stopping runtime work before
the catalog connection closes. Invalid optional catalog storage leaves fixed
unavailable provider routes while preserving the independent journal/runtime.
Rotation retires prior generations for new selection; exact historical versions
are available only through the protected historical lookup path.

The Flutter provider transport owns finite response/body/cleanup bounds and
never shares active generation or voice transports. Injected/mock clients must
supply their own provider transport factory; a missing factory cannot open a
real connection. Uncertain writes retain exact endpoint/owner-bound request
objects in bounded RAM across closing Options. Redacted GET receipts may settle
non-secret changes; secret-bearing operations require an explicit exact replay
to confirm request equality before dropping retained secret material.

## Measured LM Studio complete requests

The native LM Studio adapter now implements `prepare_complete` for Agent and
auxiliary text. Preparation counts the exact formatted prompt under a native
model/configuration snapshot and closes its socket before returning a
synchronously discardable callback. Consumption reacquires an owned operation,
revalidates the same model instance, configuration/template and exact count,
then predicts under that lease. An alias rebound, changed authority, partial
result or unconfirmed cleanup cannot produce a completed response. Existing
output/frame bounds and actual native terminal/socket-close requirements stay
in force. Agent primary and auxiliary factories construct independent adapters;
only `reasoning_effort: none` is supported for this adapter.

This adds an interface and launcher path, not fresh model qualification. It does
not make an OpenAI-compatible adapter measured, enable LM Studio multimodality,
or undo negative LFM3/Gemma evidence. No new inference was performed for this
increment; the cumulative $10 paid-test budget and reservations are unchanged.

## Remaining role application

ADR0087 subsequently adds explicit exact-generation Agent primary and auxiliary
selection. The original foundation boundary below remains the rule for catalog
saves and the still unfinished other role owners.

The catalog is a foundation. Role selection must capture immutable profile and
credential generations before applying a provider. Accepted Agent tasks keep
their old adapters and pins. Converse/Decisions/STT/TTS need their own settled
admission and resource boundaries; ordered fallbacks require per-attempt
disclosure, budget and provenance. These remain unfinished until separately
implemented and verified. A catalog save is never reported as delivering them.

## Evidence

Static Ruff/compilation, Dart analysis/format and source review are the checks
used during implementation. No new automated tests or endpoint/model/credential
probes were run for this increment. Subsequent CI and browser observations must
state their actual revision and must not imply provider quality or readiness.
