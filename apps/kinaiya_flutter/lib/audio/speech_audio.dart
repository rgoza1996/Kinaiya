/// Local speech-audio surface. Web uses the trusted JS bridge
/// (mic capture + PCM playback); every other platform reports honestly that
/// voice is unavailable and the UI degrades to text.
library;

export 'speech_mouth.dart';
export 'vad_settings.dart';

import 'vad_settings.dart';

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

/// Optional semantic-window capture; legacy and unsupported hosts stay explicit.
abstract interface class WindowSpeechAudio {
  Future<void> configureWindows(bool enabled);
  Future<Map<String, Object?>?> snapshotWindow();
  Future<bool> get userSpeaking;
  Future<int> nextUtterance();
}

/// Optional trusted adapter capability. Configuration never acquires a mic.
abstract interface class AdaptiveVadAudio {
  bool get supportsVadSettings;
  Future<VadSettings> configureVadSettings(VadSettings settings);
  Future<void> recalibrateVad();
}

/// A browser-local audio output. [opaqueId] must never be persisted, sent to a
/// service, or included in a journal. A selection is not proof of audibility.
class AudioOutputDevice {
  const AudioOutputDevice({
    required this.opaqueId,
    required this.label,
    this.selected = false,
  });

  final String opaqueId;
  final String label;
  final bool selected;
}

/// Optional output routing for this audio owner only. Discovery never opens a
/// microphone. Picker calls must begin directly in an owner's button gesture.
/// A pending change retains its original operation; observing it must not be
/// timed out and replaced. Stop/dispose still fence queued playback.
abstract interface class AudioOutputSelection {
  Future<List<AudioOutputDevice>> audioOutputs();
  Future<String> selectAudioOutput(String opaqueId);
  Future<String> chooseAudioOutput();
  Future<String> useDefaultAudioOutput();
}

/// Optional original-PCM capture for an explicitly armed native-audio Call.
/// Configuration never acquires a microphone. Existing capture operations keep
/// their ownership semantics; finishClip yields complete mono16k PCM16 WAV.
abstract interface class NativePcmCaptureAudio {
  bool get supportsNativePcmCapture;
  Future<void> configureNativePcmCapture(bool enabled);
}

/// Session-local preference, not proof that a microphone has been acquired.
class AudioInputDevice {
  const AudioInputDevice({
    required this.opaqueId,
    required this.label,
    this.selected = false,
  });

  final String opaqueId, label;
  final bool selected;
}

enum AudioInputDiscoveryReason { identifiersUnavailable }

class AudioInputDiscoveryUnavailable implements Exception {
  const AudioInputDiscoveryUnavailable(this.reason);

  final AudioInputDiscoveryReason reason;
}

/// Discovery and selection do not acquire microphone permission or persist IDs.
/// A chosen input must be acquired exactly; disappearance never selects default.
abstract interface class AudioInputSelection {
  Future<List<AudioInputDevice>> audioInputs();
  Future<String> selectAudioInput(String opaqueId);
  Future<String> useDefaultAudioInput();
}

SpeechAudio speechAudio() => impl.createSpeechAudio();
