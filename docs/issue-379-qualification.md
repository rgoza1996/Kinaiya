# Issue 379: provider fences and native publication

Baseline inspected: main `f863129cc7b3a7c9e2272d10f9aa6ae78fba65d7`.
The provider return fence merged in #375; #509 later added a test named
`test_caller_native_publication_never_emits_converse_or_routing_on_late_or_cancelled_media`.

The actual provider tests cover caller cancellation, original deadlines,
response/body/cleanup resistance, exactly one original close, explicit unknown
settlement, pre-existing cancellation, normal success and both media modes.
The existing final-fence fixture explicitly describes its exchange bridge as a
protocol double, not a native Session producer or physical-cleanup proof.

The #509 test awaits that fixture's provider task, manually appends a Converse
or routing event to a real `Store`, and manually appends to an
`agent_dispatches` list. Its negative cases prove the provider does not return a
value to that harness. They do **not** invoke the production Converse/Decisions
publication owner or an actual Agent dispatch. A real Store alone does not turn
the hand-written caller into the production caller.

## Native increment in PR #572

The separate native PR adds the protected, optional one-packet PCM lane described
in [the implementation contract](native-media-publication.md). Actual
`ConverseRuntime` and `DecisionsWorker` methods capture their real selected-model
leases and invoke an original `NativeBufferedMediaOwner` on Session's event loop.
Session retains the actual source, reader, worker, exchange and publication;
public records cannot reconstruct that authority. Provider candidates remain
private until the original return fence and authoritative publication guard.

The publication transaction holds the Store lock, owner mutex and original
source fence lock through SQLite commit. Source stop either fences first and
refuses publication, or follows the committed receipt. A lost caller response
does not establish that a committed effect is absent. Original unknown work
remains charged; replay observes the same attempt rather than dispatching again.

`tests/integration/test_native_media_publication.py` composes the actual caller,
Session/ASGI owner-channel authentication, sealed spool, Store, selected model
settings and original exchange bridge. Only HTTPX delivery/body/close is
controlled. Its acceptance observations include:

- Both modes publish healthy dialogue/routing through the actual native owner.
- Caller cancellation and original provider deadlines are exercised separately
  before response, during body and during original close; no publication escapes.
- Cancellation, expiration, model retirement and source stop while queued at the
  actual publication operation are rechecked before commit.
- A thread-side source fence at the real SQLite COMMIT waits for the original
  committed receipt. Observer cancellation after commit preserves that receipt.
- Earlier handled cancellation remains acceptable; replay does not issue another
  HTTP request or duplicate publication.
- Failed original close retains unknown cleanup, reader/source claims and model
  operation capacity; stop/delete cannot manufacture positive settlement.
- Ordinary authenticated journal append cannot forge the reserved native marker.
- A real Agent admits a permitted authenticated owner-text positive control.
  Native evidence does not create an Agent command, and direct admission using
  its published observation is refused by the actual Agent boundary.

The existing media owner/lifecycle and missing-runtime tests additionally cover
source reset/delete/shutdown and partial native namespaces. New V2 contract
round-trip/strict-validation tests do not widen the unchanged legacy schemas.

## Commands and evidence

All environments install `requirements-dev.lock` with hashes. The combined tree
adds native tests to the ordinary non-live suite. The guarded architecture check
is dispatched using:

```sh
gh workflow run ci.yml --ref codex/native-media-publication-379 -f suite=media
gh workflow run ci.yml --ref codex/native-media-publication-379 -f suite=compat
gh workflow run ci.yml --ref codex/native-media-publication-379 -f suite=full
```

These dispatches require the administrator's exact reviewed-commit allowlist;
PR events always remain on disposable hosted machines. The PR records exact
source SHAs, original failed iterations and subsequent observed results. A
passing smoke, protocol double or earlier source cut is not a final native
qualification. Preserve failures and report passed/skipped/failed separately.

## Remaining production gates

This increment is an in-process composition seam for a sealed canonical PCM16
mono 16 kHz WAV of at most one second. The default launcher does not install it;
general media start/resume still returns unavailable. It introduces no public
route, cross-service RPC, catalog media selection, model/device capability
qualification, managed physical model-worker termination, video pipeline or
automatic crash reconciliation. Local HTTP cleanup does not prove model or
device termination. Those broader #204/#449 prerequisites must remain explicit
before production wiring. No live model, camera, GPU, paid inference, service
configuration or model-quality claim is part of this qualification.
