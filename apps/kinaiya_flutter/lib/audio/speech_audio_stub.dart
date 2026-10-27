/// Non-web stub: voice honestly unavailable, text only.
library;

import 'speech_audio.dart';

class _StubSpeechAudio implements SpeechAudio {
  @override
  Future<bool> get availableMic async => false;

  @override
  Future<bool> get availablePlayback async => false;

  @override
  Future<String> startRecording() async => 'unavailable';

  @override
  Future<RecordedAudio?> stopRecording() async => null;

  @override
  Future<void> cancelRecording() async {}

  @override
  Future<void> queuePcm(String base64, int sampleRate) async {}

  @override
  Future<void> stopPlayback() async {}

  @override
  Future<bool> get isPlaying async => false;
}

SpeechAudio createSpeechAudio() => _StubSpeechAudio();
