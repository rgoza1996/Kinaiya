# ADR 0005: Functional Flutter web client with independent Agent Chat

Status: accepted experimental implementation; tracks #8 and the integration gates in #5. Full M5 remains open.

Character View calls Converse and never parses slash commands. Agent Chat uses the independent Agent API and its canonical command registry. Authenticated journal polling delivers evidenced Agent notices without waiting for a new conversational turn. Shared Dart state preserves drafts across view changes. Commands are serialized while pending; unknown commands never become model work. Task cards show actual outcomes, evidence and failures across sessions, newest first. Approvals retain the backend action digest, revision and numeric expiry.

The web audio adapter uses complete independently decodable recordings, adaptive noise thresholds, separate speech/silence hysteresis, a selectable endpoint, bounded pending transcription, clip epochs and a 60-second clip limit. Capture resumes before STT completes. Sustained speech during playback stops every local audio source before asynchronously interrupting the expected server generation. Late media permissions, playback resumes, old source callbacks and old turn cleanup cannot take ownership of a newer turn. This applies lessons from the private Converse prototype without copying its shared-global pipeline.

Kokoro PCM frames queue in sentence order. Browser `onended` permits a played acknowledgment; decoded and queued frames alone do not. Interrupted elapsed delivery remains an estimate. Stopping speech does not cancel Agent work. These are implemented sequencing invariants, not a physical audio-quality or word-alignment claim.

Connection setup imports a user-selected owner-only login file or accepts manual input. The launcher exports that credential to a new mode-0600 file. Credentials remain in memory; HTTP requests reject redirects and service URLs reject userinfo, queries and fragments. Backend-reported cloud inference must match the selected disclosure consent before text/audio can be sent, including via Enter. The indicator labels its timestamped last check rather than claiming continuous health monitoring.

Per-role positive verification and owner controls are specified in [ADR 0014](0014-provider-consent.md). Missing provider metadata cannot inherit the other role's inference permission, and uncertain model switches invalidate the previous grant.

The original mascot artwork is included unchanged with a versioned manifest and procedural state motion. It is a single frame; additional animation poses are unfinished. A skippable book sheet links to the work surface.

## Verification

The actual in-app browser loaded Flutter source `5f3c680`. A synthetic WAV passed through Whisper, Converse, asynchronous Decisions, real Agent filesystem work and Kokoro. The browser displayed the natural reply and a separately labeled verified Agent result. The task wrote `proof` containing `7`, with SHA-256 evidence. Browser delivery records reached played through audio callbacks. No physical microphone or hearing measurement occurred.

With the Converse process gracefully unloaded and port 8762 closed, Agent Chat executed `/permissions` without model inference, submitted another real task and wrote `browser.txt` containing `standalone`, with matching SHA-256 evidence. Converse was restored afterward. Source references and sanitized task evidence are in [the browser record](../../evaluation/evidence/browser-2026-10-27.json). The processes had been loaded incrementally, so this is not a single-build process attestation.

Subsequent reviewed fixes at `36d6052` preserve spaces and history cursors across streamed chunks, avoid re-seeding empty history, serialize commands and disable credential-bearing redirects. Regression tests exercise pending STT while capture/barge continue, stale playback epochs, late permission resolution, command overlap, cloud-disclosure guards and canonical backend contracts. Local checks: 140 Python tests; Flutter analysis; Flutter tests; 13 Node bridge tests; release web build with bundled renderer resources. Exact CI results belong to the associated PR. The build emits a CupertinoIcons font warning; the app uses Material icons.

Native microphone/playback are explicit stubs. Partial STT, physical device testing, roadmap latency targets, broader settings, provider management, native platform builds and mobile calling remain unverified or unfinished. The current web build is an experimental reference client.
