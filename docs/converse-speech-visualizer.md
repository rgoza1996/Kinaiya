# Converse speech visualizer

The Converse bars show recent amplitude of **Kinaiya's locally playing speech**
(issue #606). They use the same output path for modular TTS and native PCM.
Listening/recording flags and provider mouth cues do not drive these bars.

The trusted web bridge's optional `playbackFrame` returns `{active, rms}`. It
reads the existing bounded PCM RMS envelope at `AudioContext.currentTime`, inside
the current scheduled chunk of a running, current output context. Its ordinary
windows are 20 ms; long chunks retain at most 1,000 windows, and at most 128
chunks retain metadata. The visualizer adds no PCM copy, microphone acquisition,
audio-context resume, source scheduling or delivery acknowledgment. Gaps,
suspension, unavailable metadata, invalid clocks, stop and completion return
quiet measurements. Viseme cues never replace measured amplitude.

`SpeechPlaybackAudio` is an optional local adapter capability, separate from
`SpeechMouthAudio`. Other platforms retain their existing text/audio capability
behavior. An adapter or older bridge without measurements produces no fabricated
activity. This is not a frequency spectrum, phoneme detector, proof of physical
audibility or a speech-delivery receipt.

The existing CharacterView playback owner samples at most every 33 ms, retaining
thirteen RMS levels (about 0.4 seconds). Bars are ordered from older to newer,
left to right. Heights use a fixed gain of four, clamped to the display range;
silence clears the history to its quiet baseline. The UI never animates random
or fixed heights. One capability read may be outstanding: a 100 ms timeout
quiets the display while the original read retains its slot until it settles.
Late results are fenced by the original playback identity, presentation
generation, connection, session/thread/epoch, turn, speech generation and native
source generation. Stop and disposal clear levels and cancel the owner's timer.

Mouth motion and character visibility do not control output measurement. Hidden
panes, Home, local motion disabled and device reduced motion stop sampling and
hide the waveform; the existing textual stage badge still conveys playback.
Individual amplitude updates are excluded from screen-reader announcements.
Desktop and narrow layouts use the same measurements with bounded geometry.

## Verification

- `web/audio_bridge.test.js`: real PCM envelopes at controlled output clocks;
  quiet/soft/loud samples, future audio, provider mouth independence, pause,
  invalid clocks, stop, stale source completion, bounded envelope reuse and
  source-ended delivery ownership. No microphone or provider is used.
- `test/speech_playback_test.dart`: malformed, non-finite, out-of-range and
  inactive measurements fail quiet.
- `test/converse_interaction_test.dart`: actual amplitude-to-bar geometry at
  desktop and 320 px, silence, reduced motion and flags without measurements.
- `test/speech_visualizer_integration_test.dart`: modular NDJSON and admitted
  native PCM, thirteen-level retention, silence, timeout ownership, late success
  and failure, stop/end/disposal, source/session/turn invalidation, motion and
  visibility. Controlled adapters establish UI ownership, not device audibility.

The existing [mouth playback contract](adr/0064-speech-mouth-playback.md) and
speech receipt/currentness rules remain independent.

The [recorded browser rehearsal](evidence/converse-visualizer-2026-11-14/README.md)
includes public PCM controls, retained native speech, desktop/narrow captures,
source hashes and the actual verification limits.
