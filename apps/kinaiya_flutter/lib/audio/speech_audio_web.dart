/// Web implementation over the trusted `window.kinaiyaAudio` bridge.
/// Polling only: Dart never evaluates model-generated code.
library;

import 'dart:convert';
import 'dart:js_interop';
import 'dart:js_interop_unsafe';

import 'speech_audio.dart';

@JS('kinaiyaAudio')
external JSObject? get _bridge;

class _WebSpeechAudio implements SpeechAudio {
  bool get _ready => _bridge != null;

  JSPromise _call(String name, [JSAny? a, JSAny? b]) =>
      _bridge!.callMethod<JSPromise>(name.toJS, a, b);

  Future<String> _str(String name, [JSAny? a, JSAny? b]) async {
    final v = await _call(name, a, b).toDart;
    return (v as JSString).toDart;
  }

  @override
  Future<bool> get availableMic async {
    if (!_ready) return false;
    try {
      final caps = jsonDecode(await _str('capabilities')) as Map;
      return caps['mic'] == true;
    } catch (_) {
      return false;
    }
  }

  @override
  Future<bool> get availablePlayback async {
    if (!_ready) return false;
    try {
      final caps = jsonDecode(await _str('capabilities')) as Map;
      return caps['playback'] == true;
    } catch (_) {
      return false;
    }
  }

  @override
  Future<String> startRecording() =>
      _ready ? _str('startMic') : Future.value('unavailable');

  @override
  Future<RecordedAudio?> stopRecording() async {
    if (!_ready) return null;
    final raw = jsonDecode(await _str('stopMic')) as Map;
    final base64 = raw['base64'] as String? ?? '';
    if (base64.isEmpty) return null;
    return RecordedAudio(
      raw['mime'] as String? ?? 'audio/webm',
      base64Decode(base64),
    );
  }

  @override
  Future<void> cancelRecording() async {
    if (_ready) await _str('cancelMic');
  }

  @override
  Future<void> queuePcm(String base64, int sampleRate) async {
    if (_ready) {
      await _str('queuePcm', base64.toJS, sampleRate.toJS);
    }
  }

  @override
  Future<void> stopPlayback() async {
    if (_ready) await _str('stopAudio');
  }

  @override
  Future<bool> get isPlaying async {
    if (!_ready) return false;
    return (await _str('playbackActive')) == 'yes';
  }
}

SpeechAudio createSpeechAudio() => _WebSpeechAudio();
