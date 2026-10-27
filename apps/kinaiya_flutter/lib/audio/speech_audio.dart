/// Local speech-audio surface. Web uses the trusted JS bridge
/// (mic capture + PCM playback); every other platform reports honestly that
/// voice is unavailable and the UI degrades to text.
library;

import 'speech_audio_stub.dart'
    if (dart.library.js_interop) 'speech_audio_web.dart'
    as impl;

abstract class SpeechAudio {
  Future<bool> get availableMic;
  Future<bool> get availablePlayback;
  Future<String> startRecording();
  Future<RecordedAudio?> chooseRecording();
  Future<RecordedAudio?> stopRecording();
  Future<void> cancelRecording();
  Future<bool> queuePcm(String base64, int sampleRate);

  /// Resolves when actually-playing sources end (`onended`), or promptly
  /// with `stopped` after [stopPlayback]. Queued (synthesized) audio is NOT
  /// reported as played: callers must await this before acking `played`.
  Future<void> awaitPlayback();
  Future<void> stopPlayback();
  Future<bool> get isPlaying;

  /// Drains locally queued bridge events (recording/playback/armed/vad).
  Future<List<Map<String, Object?>>> pollBridgeEvents();

  /// Tunes end-of-utterance silence for armed listening (ms, clamped
  /// 400..5000 server-side of the bridge).
  Future<void> configureVad({required int endSilenceMs});

  /// User-armed continuous listening. Never enables the mic on its own:
  /// call only from an explicit user gesture. Returns `armed`, `idle` or
  /// `unavailable`. [stopArmed]/[dispose] release the kept mic track.
  Future<String> startArmed();
  Future<void> stopArmed();

  /// Per-utterance clip on the kept armed stream. [finishClip] keeps the
  /// stream alive; null/empty means no complete decodable clip.
  Future<String> beginClip();
  Future<int> get captureEpoch;
  Future<RecordedAudio?> finishClip();

  /// Releases mic tracks, stops playback, clears bridge state.
  Future<void> dispose();
}

class RecordedAudio {
  RecordedAudio(this.mime, this.bytes, {this.filename = 'recording.webm'});
  final String filename;
  final String mime;
  final List<int> bytes;
}

SpeechAudio speechAudio() => impl.createSpeechAudio();
