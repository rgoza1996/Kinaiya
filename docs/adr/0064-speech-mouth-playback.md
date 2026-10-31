# ADR 0064: Mouth movement follows speech playback

Status: Accepted

## Requirement and scope

The owner requested closed, half-open and open mouth states driven by the audio
waveform on the Converse mascot interaction page. Timed viseme shapes are
preferred when a provider supplies them, with an audio-level fallback otherwise.
The approved character artwork, existing speech/session ownership and playback
acknowledgments must be preserved.

This implements the approximate amplitude fallback allowed by Roadmap §16.4.
It does not infer phonemes from loudness or claim word-aligned speech. Current
providers do not emit viseme timing, so their playback uses the volume envelope.

## Decision

The browser audio bridge derives bounded RMS envelopes from the actual PCM it
queues. Sampling uses `AudioContext.currentTime` and each chunk's scheduled
start/end, rather than request arrival, transcript text or a speaking flag.
Typical envelope windows are 20 ms; long chunks use coarser windows to retain at
most 1,000 per chunk. At most 128 queued chunks retain mouth metadata. Audio
outside that metadata budget still plays and the mouth fails closed.

Silence below RMS 0.008 closes the mouth immediately. Hysteresis separates
closed/half/open levels, with 25 ms attack and 45 ms release smoothing. Suspended
audio contexts, gaps, stale playback generations, natural completion, Stop speech
and disposal close the mouth. These observations never acknowledge delivery;
the existing audio completion path still owns acknowledgments.

An optional `visemes` array on an audio chunk contains up to 256 ordered,
non-overlapping chunk-relative cues. Every cue is wholly inside the PCM duration:

```json
{"visemes": [{"start_ms": 0, "end_ms": 75, "viseme": "oh"}]}
```

Accepted symbols are `sil`, `PP`, `FF`, `TH`, `DD`, `kk`, `CH`, `SS`, `nn`, `RR`,
`aa`, `E`, `ih`, `oh` and `ou`. Bounds must be finite. Any invalid list uses the
PCM envelope instead. Missing cues and gaps between cues also use the envelope.
Cue openness remains gated by audible energy so silence cannot open the mouth.
The native server already forwards audio chunk fields; no model/provider
configuration or server protocol change is required.

Flutter reads the optional `SpeechMouthAudio` capability every 33 ms only while
the default interaction portrait is visible and speech is playing. Reads time out
after 100 ms; the underlying read retains its slot until it settles, preventing
a stalled bridge from accumulating reads. Results are fenced by presentation generation, turn nonce, speech epoch
and mounted state. Hidden panes, Home, hidden characters, imported packs and
reduced motion do not poll. The portrait alone listens for mouth updates.

The supplied neutral face stays fixed. A native painter changes only a small
mouth region, with half/open, rounded, pursed and teeth shapes. Imported character
packs retain their existing speaking animation. The new saved
`speech_mouth_motion` preference defaults on; older strict 6/7/8-field preference
records migrate to that default, while new 9-field records require a boolean.
Local and device reduced motion take priority without discarding the saved
choice. Turning mouth movement off does not stop audio or change a session.

## Traceability and verification

| Contract | Verification |
| --- | --- |
| Playback clock, RMS silence, smoothing, stop/end and cue admission | `web/audio_bridge.test.js` |
| Invalid bridge frames fail closed | `test/speech_mouth_test.dart` |
| Stable approved face, mouth-only pixel changes, cue shapes and reduced motion | `test/converse_mouth_test.dart` |
| Streamed cues, preference/navigation lifecycle and stale callback fencing | `test/speech_mouth_integration_test.dart` |
| Strict migration, persistence and visible switch | Presentation preference, controller and settings tests |

Tests use controlled audio clocks and synthetic playback. A separate local demo
can exercise the actual browser audio bridge without provider calls. They do not
measure physical speaker/display latency or establish native mobile audio
support. Native audio implementations without the optional capability retain
their existing playback behavior.

### Local verification, 2026-10-30

Baseline: `57c70ca` on `origin/main`. Pinned Flutter 3.47.8
(`737ba8f885`), Dart 3.13.7; locked dependencies resolved offline without changes.
From `apps/kinaiya_flutter`:

- `dart format --output=none --set-exit-if-changed lib test`: passed.
- `flutter analyze --no-pub`: passed.
- `flutter test --no-pub --reporter compact`: 637 passed, exit 0. An earlier
  redirected run reported all 637 passing but the command ended with signal 15;
  the complete rerun above exited normally.
- `node --test web/audio_bridge.test.js`: 33 passed.
- `flutter build web --release --no-pub --no-web-resources-cdn`: passed.
- Same build with `-t lib/companion_main.dart`: passed.
- `git diff --check`: passed.

The release builds emitted a missing CupertinoIcons font-family warning; no
new icon or font dependency was added. The isolated browser preview exercised
real Web Audio PCM playback, silence and Stop, volume shapes, synthetic timed
`oh` cues, and reduced motion at 1280×720. No provider or microphone was used.
Independent code reviews found no blocking defect. Native/device latency and
provider-supplied timing remain unverified as scoped above.
