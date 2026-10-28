# ADR 0026: Semantic character playback and observed activity

Status: implementation for #65 under #8 / Roadmap §§16.1–16.4 and AT22. Full M5,
Converse emotional direction and the specialized work-animation library remain open.

Pack schema 3 adds bounded semantic variants and fallback edges while preserving
schema 1/2 compatibility and image/frame/memory budgets. The importer validates
all added data before activation and preserves immutable metadata across local
preview, storage and export. [The pack guide](../character-packs.md) defines the
format, exact bounds, conditions and fallback behavior.

`CharacterPlayback` selects locally from speech, activity and availability flags
with a monotonic presentation clock. It uses stable weighted selection at clip
boundaries, cooldowns and a short work-to-work hold. Speech/listening/blocked and
critical attention have immediate transitions. Reduced motion updates the actual
semantic first frame without waiting for work hysteresis. Hidden panes pause the
clock rather than replaying missed choices when they return. Sheet ownership,
bounded decoding, disposal and stale-load rejection remain renderer responsibilities.

Character View now gives confirmed playback priority over the armed microphone.
Queued audio alone remains insufficient to claim speaking. Native task states
produce generic work or blocked presentation, with blocked/approval/paused/stop
requests taking precedence across concurrent tasks. Successful polling marks
activity current; reconnect/read failure invalidates presentation freshness while
preserving task rows for inspection. No objective-text classifier or completed
tool receipt is used to fabricate a specific current operation. There is no new
notification policy or task-success inference.

An approach starts only for a fresh authenticated resource `initializing` or
`restoring` state with owner intent to enable Converse. It reaches full size in
1200 ms without changing layout/hit targets. A ready state or confirmed speech
ends approach immediately. Completion leaves the restoration label visible;
off, failed and stale states retain honest availability. Resource readiness means
the existing service-level contract; some model weights still load on demand.
No animation, load future or selector lies on the speech execution path.

The default schema3 pack retains the original 16-frame atlas and adds a second
16-frame work/waiting/attention atlas. [Its generation record](../../apps/kinaiya_flutter/assets/character/kinaiya-activity-v1.prompts.md)
preserves exact prompt, references, hashes, geometry and artistic limits. This is
a built-in OpenAI development asset; the product Meta image-generation workflow
is still unimplemented. Specific inspection/code/email/document semantics have
explicit generic fallbacks. Alert clips exist for supplied presentation intent,
without inventing an urgency classifier or new authority.

Verification covers untrusted v3 metadata and compatibility, deterministic
weighted/cooldown/fallback/dwell boundaries, actual decoded frame selection,
sheet races, reduced/hidden playback and independent app integration checks.
The integration checks distinguish armed capture, queued audio and confirmed
playback, native task freshness and early/late restoration. Physical microphone,
audible latency, full emotion/voice consistency and native-device acceptance are
separate unfinished gates.
