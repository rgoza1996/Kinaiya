/// Non-web stub: voice honestly unavailable, text only.
library;

import 'speech_audio.dart';

class _StubSpeechAudio implements SpeechAudio {
  @override
  Future<bool> get availableMic async => false;

  @override
  Future<bool> get availablePlayback async => false;

  @override
  Future<RecordedAudio?> chooseRecording() async => null;

  @override
  Future<String> startRecording() async => 'unavailable';

  @override
  Future<RecordedAudio?> stopRecording() async => null;

  @override
  Future<void> cancelRecording() async {}

  @override
  Future<bool> queuePcm(String base64, int sampleRate) async => false;

  @override
  Future<void> awaitPlayback() async {}

  @override
  Future<void> stopPlayback() async {}

  @override
  Future<bool> get isPlaying async => false;

  @override
  Future<List<Map<String, Object?>>> pollBridgeEvents() async => const [];

  @override
  Future<void> configureVad({required int endSilenceMs}) async {}

  @override
  Future<String> startArmed() async => 'unavailable';

  @override
  Future<void> stopArmed() async {}

  @override
  Future<String> beginClip() async => 'not-armed';

  @override
  Future<int> get captureEpoch async => 0;

  @override
  Future<RecordedAudio?> finishClip() async => null;

  @override
  Future<void> dispose() async {}
}

SpeechAudio createSpeechAudio() => _StubSpeechAudio();
