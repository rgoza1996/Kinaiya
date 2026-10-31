# ADR 0079: Adaptive microphone detection and top-aligned Options

Status: Accepted implementation for #194; acoustic/model qualification remains separate.

## Behavior

Options pages anchor their contents at the top through fades, including reduced
motion. Other scene transitions retain their original default alignment.

The web microphone bridge already had noise adaptation and hysteresis. Retain
its user-armed capture, epoch fences, bounded clip duration and separate live
window path. Adapt Korina's initial 900 ms room calibration and ten-second
quiet-period recalibration: bounded 240 RMS observations, 60th percentile noise
estimate, floor 0.002–0.035, blend 70% previous/30% new. Speech and local playback
pause sampling; starting calibration requires the user to remain quiet. This is
an amplitude detector, not semantic speech recognition or echo cancellation.

Reference: `rgoza1996/Korina-Agent`, `Korina/js/vad.js`, master
`9db02e6c39e4667f1b547d0aa5a9551d9c023708`. Korina is the prototype; its UI state,
network paths and turn ownership are not imported. The adapted source retains
the MIT notice in `apps/kinaiya_flutter/web/KORINA_VAD_LICENSE.txt`, distributed
with the web client.

## Owner settings

Options → Voice embeds Dynamic VAD (default on), sensitivity (lower/balanced/
higher, balanced default), and queued room recalibration. The fallback clip
path additionally exposes speech confirmation (default 300 ms, 100–2000 bounds),
interrupt confirmation (220 ms, 100–2000), and ending silence (1500 ms,
400–5000; current UI presets 800/1500/2500). Sensitivity multiplies thresholds
by 1.25/1/0.75. Fixed detection uses speech 0.035/silence 0.022 before scaling.
Settings and calibration persist across clips while the mounted view owns them;
they are not durable app preferences. Reopening Options reads that owner state.

The optional adapter capability keeps older/native text-only implementations
compatible and avoids advertising unsupported controls. Validate the complete
profile and its acknowledgment. Invalid fields are rejected atomically; failed
or timed-out acknowledgments remain visibly unconfirmed. Connection/widget
replacement invalidates stale UI completions. No settings navigation, setting
change or calibration request acquires a microphone, starts/stops playback,
or changes a conversation turn. Explicit Call still owns capture.

Continuous windows report acoustic metadata without ending turns or locally
interrupting playback. Decisions retains live wait/respond/interrupt ownership.
Clip-only timing controls are hidden in live mode.

## Evidence boundaries

Deterministic browser fakes and Flutter checks cover profile validation, clipping
and timing boundaries, retained tuning, continuous windows, no implicit capture,
UI ownership and top alignment. These do not establish real-room accuracy,
physical microphone behavior, native mobile capture, model quality or latency.
Multimodal/model qualification is tracked separately in #196.
