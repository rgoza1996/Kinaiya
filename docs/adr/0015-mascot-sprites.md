# ADR 0015: Default mascot frame animation

Status: experimental implementation for #37 / M5. The full character-pack gate,
expression library, pack import/editor and work-costume variants remain open.

The default manifest now selects a real 16-frame PNG atlas derived from the
existing owner-supplied mascot. Idle, listening, thinking and speaking each use a
row of four poses, played forward and back locally. The original image is unchanged
and remains the fixed fallback. The visible character is 220 logical pixels.
The [bundled generation record](../../apps/kinaiya_flutter/assets/character/kinaiya-core-v1.prompts.md)
contains both prompts, source/output hashes, actual dimensions and asset lineage.
The built-in OpenAI image tool produced these development assets; the product's
planned Meta image-generation integration is not implemented by this work.

Manifest schema 2 declares sheet dimensions, frame geometry, checksums, durations,
semantic mappings, anchor/scale bounds, provenance, license and theme metadata.
The current renderer aligns a contained frame using the anchor and inherits the
app's existing theme; live theme/voice changes and pack installation are outside
this change. Schema 1 remains readable. Unknown presentation states use idle.

Validation rejects unsafe asset paths, bad metadata/checksums, invalid PNG headers,
out-of-bounds frame IDs and exceeded encoded/pixel/count/timing budgets. A sheet
may contain unused trailing padding: the generated image is 1254 square with
313-square cells and a two-pixel unused right/bottom strip. The grid's floored
capacity and actual texture dimensions bound every selected source rectangle.
This does not certify perfect pose registration or seamless artistic continuity.

The renderer checks texture limits before decoding, checks the decoded image's
dimensions, rejects animated PNG codecs, disposes codecs/images and drops stale
pack/bundle loads. A decode failure displays the original image and a visible note.
Asset loading never holds speech or runtime work. Reduced motion fixes the first
frame, and Flutter TickerMode mutes hidden animation. Stable image semantics avoid
announcing internal frame numbers. Speaking motion is approximate presentation
while playback is active, without phoneme or amplitude synchronization.

## Verification

Deterministic tests wait for the actual engine-decoded image and inspect the
source rectangle passed to the painter after clock advances. A changing fallback
label cannot satisfy these checks. Coverage includes state mapping, real default
asset loading, reduced-motion stop/restart, TickerMode, pending/replaced pack and
bundle ownership, ByteData view offsets, corrupt PNG payloads, safe fallback and
trailing atlas padding. All use local fixtures and require no paid inference.
The lead also inspected the actual release web build in the in-app browser.

Only four core states are supplied. More expressions, precisely registered motion,
weighted variants/cooldowns, warmup choreography and native device checks remain
unfinished; no full M5 or production-readiness claim is made.
