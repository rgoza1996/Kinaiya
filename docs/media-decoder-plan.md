# Initial decoder track planning

Related: [media protocol](media-protocol.md), [private bootstrap](media-bootstrap.md),
[issue #204](https://github.com/rgoza1996/Kinaiya/issues/204).

`packages/resources/media_decode_plan.py` supplies the explicit initial
probe-index/track/format/clock correspondence needed before opening a decoder
segment. It is a native composition helper, with no public route or permission
authority. It does not execute a decoder or qualify the supplied metadata.

`build_decode_plan` receives resolved source/seal references and admission,
seal, selected profile, probe and DecodeReady records. The native caller must
first authenticate the original peer and resolve the exact stored references;
well-formed objects supplied to this function do not establish either fact.
The helper revalidates their complete actual values through the strict media
codec and checks source/fence/reservation/policy, profile, byte-count/hash and
probe agreement. It accepts only finite recording plans for the selected PyAV
driver with present implementation/runtime/schema metadata. Those declarations
still need actual asset, isolation, codec and lifecycle qualification.

Every `TrackSelection` explicitly names an original `ProbeTrack.index`, an
allocated native track ID and a complete selected Format. Every `ProbeClock`
names that same original index and its first actual signed presentation PTS,
timebase and provenance. Every probe track must appear exactly once in both
sets, including multiple tracks of the same kind. Ready-track array order is
never used to infer an association. Formats must be in the selected profile,
match the native track digest/kind and use the declared identity or explicitly
selected transform. Original dimensions/duration and selected output geometry
remain bounded; an output scale does not waive original decode limits.

For this initial driver, the common origin is selected as the exact minimum
first presentation time among timed tracks. Arithmetic is rational, preserves
negative original PTS and refuses an unrepresentable reduced result. Missing
PTS, fabricated frame-index clocks and provider-reported timing cannot fill the
original decoder observation. Untimed images use zero PTS/origin and retain
their positive declared timebase, including a non-unit timebase. This selection
applies to this helper; it does not tighten the general media Track grammar.

The returned `DecodePlan` contains independent immutable canonical byte pins
for the seven input records/references, an exact origin and immutable selected
track/format/clock tuples. Mutating caller models afterward cannot rewrite it.
Its SHA-256 uses the domain `kinaiya.media.decode-plan.v1` plus a NUL byte,
then the seven pins in signature order, each prefixed by its uint32be length,
then compact sorted-key JSON for origin and ordered track details. Track
details include the probe index, track/format byte hashes, signed first PTS,
timebase, provenance and output geometry. This is an internal correspondence
digest, not an admission, physical observation or public cleanup receipt.

Pre-owner acyclic launch configuration, probe-charged original segment budget,
FD3 startup binding, real decoder execution, continuation counters/budgets and
sealed sandbox/inner-process ownership still require native integration.
The initial plan cannot reset a continuing source's counters, mint another
owner intent, certify EOF or replace a currentness/qualification check before
effects. Media consumers, complete transcripts, Agent delivery and the full
AT64–AT66/sign-language demonstration remain separate pending work.
