# Native finite-decoder integration

This work connects the [standalone decoder](media-decoder-v2.md) to the native
Session owner. It does not yet enable Session decoder start/resume. The full
[media protocol](media-protocol.md) remains the requirement; the joins below are
prerequisites, not replacement acceptance criteria.

## History types and naming

The selected history bodies are `Disclosure`, `Consumer`, `Blob`, `TrackRange`,
`Unit`, `ManifestPage`, `ManifestSeal` and `MediaRange`, with the closed shapes in
the media protocol. Their Python classes have the `Media` prefix, except that
the existing public descriptive name `MediaRange` is also its Python class
name. Its canonical digest domain is explicitly `MediaRange`, and its receipt
domain is `R<MediaRange>`. There is no `Range` alias. The remaining bodies use
the existing prefix-stripping convention. Existing digest domains do not change.

`Consumer.permit_sha256` hashes the entire canonical Consumer body except its
own root self-digest. It includes nested digests. This public hash is not a
credential: the private capability, native currentness, source, disclosure and
task checks remain independently required.

Point-frame intervals are valid. Structural TrackRange counts are nonnegative,
including zero, with exactly one complete audio or frame pair. Native producers
must additionally check the resolved track/format, actual samples/frames, body
and history. An empty ManifestPage is structurally representable; the native
publication transition determines its admissible chain and continuity. A
complete ManifestSeal requires InputEnd, but these DTOs cannot resolve that
reference or prove actual EOF. Manifest completion is separate from downstream
publication completion and physical cleanup.

## Verify, retain, register, then grant

The ordinary launcher previously verified its original bootstrap and immediately
released execution. Session needs to retain a linked local-process ChildOwner
before that release. The additive held interface has two stages:

1. `await pool.prepare(launch, original_grant)` creates and retains one original
   bootstrap, verifies its actual kernel process identity and returns an opaque
   `MediaPreparedChild`. The configured program has not received its exec grant.
2. Session must resolve and persist the actual linked ChildOwner under its
   original coordinator. Only then may it call
   `await pool.start_prepared(original_capsule)`, which rechecks the original
   process/currentness and grants that same process once.

The capsule's identity and binding properties return fresh historical metadata.
Copied fields, a copied capsule, an arbitrary receipt or a Boolean cannot select
the retained original. The pool resolves the exact capsule and original process
it owns. Session remains responsible for its own protected transaction and
publication guard; receiving an identity does not prove that registration ran.

Both stages retain their original work across cancelled observers. Exact replay
observes that work; it never respawns or invents a replacement after uncertainty.
Legacy `start` remains available to its existing internal callers, but cannot
release a launch enrolled through the held interface. A legacy launch cannot be
reinterpreted as a prepared launch. Fence, settle and close prevent a later
grant and retain the original descriptors/process/dependencies through existing
positive retirement. A held process is still an owned live claim.

## Resource and continuation authority

The held identity is not a CPU measurement, a resource reservation or a sealed
runtime qualification. In particular, a final `Process.wait()`, `/proc` sample,
child-reported baseline or test callback cannot establish exact cumulative
descendant CPU. Native integration still needs a qualified mechanism for those
measurements and enforcement before probe, including backpressure and
reconstruction. Unknown prior work remains charged against the same immutable
segment; a new child or segment index cannot renew that allowance.

The child has its own defensive timers, but they do not resolve native budget
references. The native controller must freeze a uniform meaning for the supplied
spent values and their relation to the original child's probe/reconstruction
before using the current open-plan budget in production. No parent accounting
policy is inferred from the child's index-dependent defensive arithmetic.

A positively retired physical child rotation between adjacent segments preserves
the source, known tracks, cursors, profile and continuity. That is segment
rollover. A semantic source/selection replacement follows its own generation and
fence transition; a new PID alone does not determine it.

## Remaining joins

Actual Session integration still requires native consumer/permit enrollment,
qualified runtime selection, charged budgets, complete probe/ready/open control,
protected decoded bodies and immutable input history, schema migration/audits,
and original reader/publication/process cleanup. It must retain every selected
track and format, source-global and per-track sequence, original clock and
sample/frame, and known timestamp gap. Reset/delete/stop/replacement must fence
before new side effects and join original owners before releasing reservations.

After those joins, actual provider inference and the complete
Converse/Decisions/Agent/playback workflow still need qualification, including
failure and interruption cases. These prerequisites do not qualify the
news-video/sign-language demonstration or AT64–AT66.
