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
  Future<RecordedAudio?> stopRecording();
  Future<void> cancelRecording();
  Future<void> queuePcm(String base64, int sampleRate);
  Future<void> stopPlayback();
  Future<bool> get isPlaying;
}

class RecordedAudio {
  RecordedAudio(this.mime, this.bytes);
  final String mime;
  final List<int> bytes;
}

SpeechAudio speechAudio() => impl.createSpeechAudio();
