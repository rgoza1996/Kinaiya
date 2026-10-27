/// Web implementation over the trusted `window.kinaiyaAudio` bridge.
/// Polling only: Dart never evaluates model-generated code.
///
/// The bridge mixes sync string returns (capabilities, cancelMic,
/// stopAudio, playbackActive, pollEvents, ...) with async Promise returns
/// (startMic, stopMic, queuePcm, ...). [_str0]/[_str1] normalize both: a
/// [JSPromise] is awaited, a plain [JSString] is read directly. Calling a
/// sync bridge function through a `JSPromise` binding silently fails, which
/// is why capability checks used to report voice unavailable everywhere.
library;

import 'dart:convert';
import 'dart:js_interop';
import 'dart:js_interop_unsafe';

import 'speech_audio.dart';

@JS('kinaiyaAudio')
external JSObject? get _bridge;

class _WebSpeechAudio implements SpeechAudio, WindowSpeechAudio {
  bool get _ready => _bridge != null;

  JSAny _call0(String name) => _bridge!.callMethod<JSAny>(name.toJS);

  JSAny _call1(String name, JSAny a) =>
      _bridge!.callMethod<JSAny>(name.toJS, a);

  JSAny _call2(String name, JSAny a, JSAny b) =>
      _bridge!.callMethod<JSAny>(name.toJS, a, b);

  /// Normalizes the bridge's dual sync/async calling semantics: a
  /// [JSPromise] is awaited, a plain value is read directly. Uses [isA],
  /// the platform-consistent JS-interop check.
  Future<String> _strOf(JSAny result) async {
    if (result.isA<JSPromise>()) {
      final JSAny? v = await (result as JSPromise).toDart;
      if (v == null) return '';
      final Object? dart = v.dartify();
      if (dart is String) return dart;
      return '$dart';
    }
    final Object? dart = result.dartify();
    if (dart is String) return dart;
    if (dart == null) return '';
    return '$dart';
  }

  Future<String> _str0(String name) async => _strOf(_call0(name));
  Future<String> _str1(String name, JSAny a) async => _strOf(_call1(name, a));

  @override
  Future<bool> get availableMic async {
    if (!_ready) return false;
    try {
      final caps = jsonDecode(await _str0('capabilities')) as Map;
      return caps['mic'] == true;
    } catch (_) {
      return false;
    }
  }

  @override
  Future<bool> get availablePlayback async {
    if (!_ready) return false;
    try {
      final caps = jsonDecode(await _str0('capabilities')) as Map;
      return caps['playback'] == true;
    } catch (_) {
      return false;
    }
  }

  @override
  Future<RecordedAudio?> chooseRecording() async {
    if (!_ready) return null;
    final text = await _str0('chooseRecording');
    if (text.isEmpty) return null;
    final value = jsonDecode(text) as Map;
    return RecordedAudio(
      value['mime'] as String,
      base64Decode(value['base64'] as String),
      filename: value['filename'] as String,
    );
  }

  @override
  Future<String> startRecording() =>
      _ready ? _str0('startMic') : Future.value('unavailable');

  @override
  Future<RecordedAudio?> stopRecording() async {
    if (!_ready) return null;
    try {
      final raw = jsonDecode(await _str0('stopMic')) as Map;
      final base64 = raw['base64'] as String? ?? '';
      if (base64.isEmpty) return null;
      return RecordedAudio(
        raw['mime'] as String? ?? 'audio/webm',
        base64Decode(base64),
      );
    } catch (_) {
      return null;
    }
  }

  @override
  Future<void> cancelRecording() async {
    if (_ready) {
      try {
        await _str0('cancelMic');
      } catch (_) {}
    }
  }

  @override
  Future<bool> queuePcm(String base64, int sampleRate) async {
    if (!_ready) return false;
    try {
      return await _strOf(_call2('queuePcm', base64.toJS, sampleRate.toJS)) ==
          'queued';
    } catch (_) {
      return false;
    }
  }

  @override
  Future<void> awaitPlayback() async {
    if (!_ready) return;
    try {
      await _str0('awaitPlayback');
    } catch (_) {}
  }

  @override
  Future<void> stopPlayback() async {
    if (_ready) {
      try {
        await _str0('stopAudio');
      } catch (_) {}
    }
  }

  @override
  Future<bool> get isPlaying async {
    if (!_ready) return false;
    try {
      return (await _str0('playbackActive')) == 'yes';
    } catch (_) {
      return false;
    }
  }

  @override
  Future<List<Map<String, Object?>>> pollBridgeEvents() async {
    if (!_ready) return const [];
    try {
      final raw = jsonDecode(await _str0('pollEvents'));
      if (raw is List) {
        return raw
            .whereType<Map>()
            .map((e) => e.cast<String, Object?>())
            .toList();
      }
    } catch (_) {}
    return const [];
  }

  @override
  Future<void> configureVad({required int endSilenceMs}) async {
    if (!_ready) return;
    try {
      await _str1(
        'vadConfigure',
        jsonEncode(<String, Object?>{'endSilenceMs': endSilenceMs}).toJS,
      );
    } catch (_) {}
  }

  @override
  Future<String> startArmed() =>
      _ready ? _str0('startArmed') : Future.value('unavailable');

  @override
  Future<void> stopArmed() async {
    if (_ready) {
      try {
        await _str0('stopArmed');
      } catch (_) {}
    }
  }

  @override
  Future<int> get captureEpoch async =>
      _ready ? int.tryParse(await _str0('captureEpoch')) ?? 0 : 0;

  @override
  Future<void> configureWindows(bool enabled) async {
    if (_ready) await _str1('windowConfigure', enabled.toJS);
  }

  @override
  Future<Map<String, Object?>?> snapshotWindow() async {
    if (!_ready) return null;
    final raw = await _str0('windowSnapshot');
    if (raw.isEmpty) return null;
    return (jsonDecode(raw) as Map).cast<String, Object?>();
  }

  @override
  Future<bool> get userSpeaking async =>
      _ready && await _str0('windowSpeaking') == 'yes';

  @override
  Future<int> nextUtterance() async =>
      _ready ? int.tryParse(await _str0('nextWindowUtterance')) ?? 0 : 0;

  @override
  Future<String> beginClip() =>
      _ready ? _str0('beginClip') : Future.value('not-armed');

  @override
  Future<RecordedAudio?> finishClip() async {
    if (!_ready) return null;
    try {
      final raw = jsonDecode(await _str0('finishClip')) as Map;
      final base64 = raw['base64'] as String? ?? '';
      if (base64.isEmpty) return null;
      return RecordedAudio(
        raw['mime'] as String? ?? 'audio/webm',
        base64Decode(base64),
      );
    } catch (_) {
      return null;
    }
  }

  @override
  Future<void> dispose() async {
    if (_ready) {
      try {
        await _str0('dispose');
      } catch (_) {}
    }
  }
}

SpeechAudio createSpeechAudio() => _WebSpeechAudio();
