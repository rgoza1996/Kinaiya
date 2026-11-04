// Requirement-first native Call and local input-selector oracles.
// Controlled transport/audio fixtures are not native producer or microphone proof.
import 'dart:async';
import 'dart:typed_data';

import 'package:crypto/crypto.dart' as crypto;
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/audio/speech_audio.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';

import 'input_disclosure_test.dart' show withClient, character;
import 'live_capture_test.dart' show CaptureAudio;
import 'native_audio_clip_test.dart' as old;

List<int> _wav(int tag) {
  final bytes = old.wav();
  ByteData.sublistView(bytes).setInt16(44, tag, Endian.little);
  return List<int>.unmodifiable(bytes);
}

class _Original {
  _Original(
    this.session,
    this.utterance,
    this.request,
    List<int> wav,
    this.scope,
    int index,
  ) : bytes = List<int>.unmodifiable(wav) {
    source = JournalEntry(
      eventId: 'native-source-$index',
      sequence: 10 + 10 * index,
      sessionId: session.sessionId,
      threadId: session.threadId,
      epoch: session.epoch,
      kind: 'conversation.window',
      origin: 'authenticated-user',
      payload: {
        'utterance_id': utterance,
        'capture_ended': true,
        'user_speaking': false,
        'window_index': 0,
        'revision': 1,
        'text': 'Public synthetic spoken request.',
        'audio_sha256': crypto.sha256.convert(bytes).toString(),
        'content_scope': scope.wireValue,
        'start_ms': 0,
        'end_ms': ((bytes.length - 44) / 32).ceil(),
      },
    );
    reply = JournalEntry(
      eventId: 'native-reply-$index',
      sequence: source.sequence + 2,
      sessionId: session.sessionId,
      threadId: session.threadId,
      epoch: session.epoch,
      kind: 'conversation.converse',
      origin: 'converse',
      payload: {
        'native_audio': true,
        'window_utterance_id': utterance,
        'window_source_id': source.eventId,
        'generation_epoch': 5 + index,
        'runtime_id': 'native-runtime',
        'chunk_id': 'native-chunk-$index',
        'text': 'Public synthetic reply.',
      },
    );
  }
  final SessionInfo session;
  final String utterance, request;
  final ContentScope scope;
  final List<int> bytes;
  final done = Completer<NativeAudioClipAdmission>();
  late final JournalEntry source, reply;
  void complete() {
    if (!done.isCompleted) {
      done.complete(NativeAudioClipAdmission(source, false));
    }
  }
}

class _Api extends old.NativeClipApi {
  bool configured = true;
  final originals = <_Original>[];
  final published = <JournalEntry>[];
  Completer<void>? interruptGate;

  @override
  Future<Map<String, Object?>> converseHealth() async =>
      configured ? old.health(available: ready) : {'enabled': true};

  @override
  Future<NativeAudioClipAdmission> submitNativeAudioClip({
    required SessionInfo session,
    required String utteranceId,
    required String requestId,
    required List<int> wav,
    required ContentScope contentScope,
  }) {
    final original = _Original(
      session,
      utteranceId,
      requestId,
      wav,
      contentScope,
      originals.length,
    );
    originals.add(original);
    return original.done.future;
  }

  @override
  Future<List<JournalEntry>> transcript({
    required String sessionId,
    int after = 0,
    int limit = 200,
  }) async =>
      published.where((entry) => entry.sequence > after).take(limit).toList();

  @override
  Future<void> interruptSpeech(String id, {int? expectedGeneration}) {
    interrupts++;
    return interruptGate?.future ?? Future<void>.value();
  }

  void releaseAll() {
    for (final original in originals) {
      original.complete();
    }
    if (interruptGate != null && !interruptGate!.isCompleted) {
      interruptGate!.complete();
    }
  }
}

class _Audio extends CaptureAudio
    implements NativePcmCaptureAudio, AudioInputSelection {
  bool supported = true, armed = false, configured = false, playing = false;
  int arms = 0, disarms = 0, finishes = 0, queued = 0, discoveries = 0;
  final configurations = <bool>[];
  final successfulSelections = <String>[];
  final clipsToFinish = <List<int>>[];
  Completer<RecordedAudio?>? finishGate;
  final finishEntered = Completer<void>();
  Completer<String>? armGate;
  final armEntered = Completer<void>();
  Completer<void>? playback;
  bool disposed = false;

  @override
  bool get supportsNativePcmCapture => supported;
  @override
  Future<void> configureNativePcmCapture(bool value) async {
    configurations.add(value);
    if (value && !supported) throw StateError('Native PCM unavailable');
    configured = value;
  }

  @override
  Future<String> startArmed() async {
    arms++;
    if (!armEntered.isCompleted) armEntered.complete();
    final result = armGate == null ? 'armed' : await armGate!.future;
    armed = result == 'armed';
    return result;
  }

  @override
  Future<void> stopArmed() async {
    disarms++;
    armed = false;
  }

  @override
  Future<RecordedAudio?> finishClip() {
    finishes++;
    if (!finishEntered.isCompleted) finishEntered.complete();
    if (finishGate != null) return finishGate!.future;
    if (clipsToFinish.isEmpty) return Future<RecordedAudio?>.value();
    final original = clipsToFinish.removeAt(0);
    return Future.value(
      RecordedAudio('audio/wav', original, filename: 'native.wav'),
    );
  }

  @override
  Future<bool> get isPlaying async => playing;
  @override
  Future<bool> queuePcm(String value, int rate) async {
    queued++;
    playing = true;
    playback = Completer<void>();
    return true;
  }

  @override
  Future<void> awaitPlayback() => playback?.future ?? Future<void>.value();
  @override
  Future<void> stopPlayback() async {
    await super.stopPlayback();
    playing = false;
    if (playback != null && !playback!.isCompleted) playback!.complete();
  }

  @override
  Future<void> dispose() async {
    if (disposed) return;
    disposed = true;
    await stopArmed();
    await stopPlayback();
  }

  @override
  Future<List<AudioInputDevice>> audioInputs() async {
    discoveries++;
    return [
      const AudioInputDevice(
        opaqueId: 'opaque-input-a',
        label: 'Synthetic input A',
      ),
      const AudioInputDevice(
        opaqueId: 'opaque-input-b',
        label: 'Synthetic input B',
      ),
    ];
  }

  @override
  Future<String> selectAudioInput(String id) async {
    if (armed || (armGate != null && !armGate!.isCompleted)) return 'busy';
    if (!['opaque-input-a', 'opaque-input-b'].contains(id)) return 'invalid';
    successfulSelections.add(id);
    return 'selected';
  }

  @override
  Future<String> useDefaultAudioInput() async {
    if (armed || (armGate != null && !armGate!.isCompleted)) return 'busy';
    successfulSelections.add('default');
    return 'default';
  }
}

Future<void> _until(
  WidgetTester tester,
  bool Function() reached,
  String reason,
) async {
  for (var step = 0; step < 20 && !reached(); step++) {
    await tester.pump(const Duration(milliseconds: 100));
  }
  expect(reached(), isTrue, reason: reason);
}

Future<AppState> _mount(
  WidgetTester tester,
  _Api api,
  CaptureAudio audio,
) async {
  final state = withClient(api, BackendProfile.clean);
  await tester.pumpWidget(character(state, audio));
  await tester.pump(const Duration(milliseconds: 100));
  return state;
}

Future<void> _call(WidgetTester tester) async {
  await tester.tap(find.byKey(const ValueKey('call-kinaiya')));
  await tester.pump(const Duration(milliseconds: 100));
}

Future<void> _voice(WidgetTester tester, CaptureAudio audio) async {
  audio.events.add({'type': 'speech-start', 'epoch': audio.epoch});
  await tester.pump(const Duration(milliseconds: 150));
}

Future<void> _end(WidgetTester tester, CaptureAudio audio) async {
  audio.events.add({'type': 'speech-end', 'epoch': audio.epoch});
  await tester.pump(const Duration(milliseconds: 150));
}

void _noLegacy(_Api api) {
  expect(api.transcriptionScopes, isEmpty);
  expect(api.windows, isEmpty);
  expect(api.turnScopes, isEmpty);
}

Future<void> _close(
  WidgetTester tester,
  AppState state,
  _Api api,
  CaptureAudio audio,
) async {
  await tester.pumpWidget(const SizedBox.shrink());
  api.releaseAll();
  await tester.pump(const Duration(milliseconds: 100));
  api.releaseAll();
  await tester.pump();
  await audio.dispose();
  state.dispose();
}

void main() {
  testWidgets(
    'native Call sends only original canonical PCM, even with semantic windows enabled',
    (tester) async {
      final api = _Api();
      final audio = _Audio()..clipsToFinish.add(_wav(101));
      final state = await _mount(tester, api, audio);
      state.streamingWindows = true;
      try {
        await _call(tester);
        await _voice(tester, audio);
        await _end(tester, audio);
        await _until(
          tester,
          () => api.originals.length == 1,
          'original native admission entered',
        );
        expect(audio.configurations, contains(true));
        expect(api.originals.single.bytes, _wav(101));
        expect(
          api.originals.single.session.sessionId,
          state.converseSession!.sessionId,
        );
        expect(
          api.originals.single.session.epoch,
          state.converseSession!.epoch,
        );
        expect(
          () => api.originals.single.bytes[44] = 0,
          throwsUnsupportedError,
        );
        _noLegacy(api);
      } finally {
        await _close(tester, state, api, audio);
      }
    },
  );

  for (final unavailable in ['missing', 'capture', 'owner']) {
    testWidgets(
      'configured native Call never falls back when unavailable=$unavailable',
      (tester) async {
        final api = _Api()..ready = unavailable != 'owner';
        final audio = unavailable == 'missing'
            ? CaptureAudio()
            : (_Audio()..supported = unavailable != 'capture');
        final state = await _mount(tester, api, audio);
        try {
          await _call(tester);
          audio.events.add({'type': 'speech-end', 'epoch': audio.epoch});
          await tester.pump(const Duration(milliseconds: 300));
          expect(api.originals, isEmpty);
          _noLegacy(api);
          expect(
            find.textContaining(
              RegExp(
                'unavailable|unsupported|not supported|cannot',
                caseSensitive: false,
              ),
            ),
            findsWidgets,
          );
        } finally {
          await _close(tester, state, api, audio);
        }
      },
    );
  }

  testWidgets(
    'native unconfigured Call keeps existing legacy transcription path',
    (tester) async {
      final api = _Api()..configured = false;
      final audio = CaptureAudio();
      final state = await _mount(tester, api, audio);
      try {
        state.streamingWindows = false;
        await _call(tester);
        await _voice(tester, audio);
        await _end(tester, audio);
        await _until(
          tester,
          () => api.transcriptionScopes.isNotEmpty,
          'legacy original transcription entered',
        );
        expect(api.originals, isEmpty);
      } finally {
        await _close(tester, state, api, audio);
      }
    },
  );

  testWidgets('native end events must match the actual current capture epoch', (
    tester,
  ) async {
    final api = _Api();
    final audio = _Audio()..clipsToFinish.add(_wav(102));
    final state = await _mount(tester, api, audio);
    try {
      await _call(tester);
      await _voice(tester, audio);
      final finishesBefore = audio.finishes;
      audio.events.add({'type': 'speech-end', 'epoch': audio.epoch - 1});
      await tester.pump(const Duration(milliseconds: 300));
      expect(api.originals, isEmpty);
      expect(audio.finishes, finishesBefore);
      await _end(tester, audio);
      await _until(
        tester,
        () => api.originals.length == 1,
        'current epoch completed native clip',
      );
      expect(api.originals.single.bytes, _wav(102));
      _noLegacy(api);
    } finally {
      await _close(tester, state, api, audio);
    }
  });

  testWidgets(
    'whole-utterance overflow stops capture visibly without submitting a prefix',
    (tester) async {
      final api = _Api();
      final audio = _Audio()..clipsToFinish.add(_wav(103));
      final state = await _mount(tester, api, audio);
      try {
        await _call(tester);
        await _voice(tester, audio);
        audio.events.add({
          'type': 'native-clip-overflow',
          'epoch': audio.epoch,
        });
        await tester.pump(const Duration(milliseconds: 300));
        expect(api.originals, isEmpty);
        _noLegacy(api);
        expect(audio.disarms, greaterThan(0));
        expect(
          find.textContaining(
            RegExp('30|long|overflow|limit', caseSensitive: false),
          ),
          findsWidgets,
        );
      } finally {
        await _close(tester, state, api, audio);
      }
    },
  );

  testWidgets(
    'one pending native clip survives a third utterance without overwrite',
    (tester) async {
      final api = _Api();
      final audio = _Audio()
        ..clipsToFinish.addAll([_wav(111), _wav(222), _wav(333)]);
      final state = await _mount(tester, api, audio);
      try {
        await _call(tester);
        await _voice(tester, audio);
        await _end(tester, audio);
        await _until(
          tester,
          () => api.originals.length == 1,
          'first original held',
        );
        final first = api.originals.single;
        await _voice(tester, audio);
        await _end(tester, audio);
        expect(api.originals.length, 1);
        expect(first.done.isCompleted, isFalse);
        await _voice(tester, audio);
        await _end(tester, audio);
        expect(api.originals.length, 1);
        expect(first.done.isCompleted, isFalse);
        expect(audio.disarms, greaterThan(0));
        expect(
          find.byTooltip('Stop Live listen'),
          findsOneWidget,
          reason:
              'logical owner remains stoppable after actual capture capacity stops',
        );
        first.complete();
        await _until(
          tester,
          () => api.originals.length == 2,
          'same pending clip dispatches after original settles',
        );
        expect(api.originals[0], same(first));
        expect(api.originals[0].bytes, _wav(111));
        expect(api.originals[1].bytes, _wav(222));
        expect(api.originals.map((original) => original.bytes).toList(), [
          _wav(111),
          _wav(222),
        ]);
        _noLegacy(api);
      } finally {
        await _close(tester, state, api, audio);
      }
    },
  );

  for (final mismatchedAck in [false, true]) {
    testWidgets(
      'unknown first native admission cannot release pending second clip mismatchedAck=$mismatchedAck',
      (tester) async {
        final api = _Api();
        final audio = _Audio()
          ..clipsToFinish.addAll([_wav(141), _wav(242), _wav(343)]);
        final state = await _mount(tester, api, audio);
        try {
          await _call(tester);
          await _voice(tester, audio);
          await _end(tester, audio);
          await _until(
            tester,
            () => api.originals.length == 1,
            'first native POST is actually held',
          );
          final first = api.originals.single;
          api.interruptGate = Completer<void>();
          final priorInterrupts = api.interrupts, priorStops = audio.stops;
          audio.events.add({'type': 'barge', 'epoch': audio.epoch});
          await _until(
            tester,
            () => api.interrupts > priorInterrupts && audio.stops > priorStops,
            'second speech retains held interrupt and stops local PCM',
          );
          expect(api.interruptGate!.isCompleted, isFalse);
          await _voice(tester, audio);
          await _end(tester, audio);
          await _until(
            tester,
            () => audio.finishes >= 2,
            'second actual completed clip is returned while first POST remains held',
          );
          expect(api.originals, hasLength(1));
          expect(first.done.isCompleted, isFalse);
          expect(audio.clipsToFinish, [_wav(343)]);
          final disarmsBeforeFailure = audio.disarms;
          if (mismatchedAck) {
            final original = first.source;
            final wrongHash = JournalEntry(
              eventId: original.eventId,
              sequence: original.sequence,
              sessionId: original.sessionId,
              threadId: original.threadId,
              epoch: original.epoch,
              kind: original.kind,
              origin: original.origin,
              payload: {...original.payload, 'audio_sha256': '0' * 64},
            );
            first.done.complete(NativeAudioClipAdmission(wrongHash, false));
          } else {
            first.done.completeError(
              TimeoutException('Synthetic original ACK unavailable'),
            );
          }
          await _until(
            tester,
            () => audio.disarms > disarmsBeforeFailure,
            'unconfirmed original admission stops actual capture',
          );
          expect(api.originals.single, same(first));
          // Remove the independent interrupt gate: it must not hide a broken
          // admission guard that would otherwise auto-submit the pending clip.
          api.interruptGate!.complete();
          await tester.pump(const Duration(milliseconds: 300));
          expect(api.originals, hasLength(1));
          expect(api.originals.single.bytes, _wav(141));
          expect(audio.clipsToFinish, [_wav(343)]);
          expect(
            find.textContaining(
              RegExp(
                'unconfirmed|unknown|busy|pending|reconcil',
                caseSensitive: false,
              ),
            ),
            findsWidgets,
          );
          _noLegacy(api);
        } finally {
          await _close(tester, state, api, audio);
        }
      },
    );
  }

  testWidgets(
    'native spoken barge stops owned PCM while original interrupt remains held',
    (tester) async {
      final api = _Api();
      final audio = _Audio()..clipsToFinish.add(_wav(121));
      final state = await _mount(tester, api, audio);
      try {
        await _call(tester);
        await _voice(tester, audio);
        await _end(tester, audio);
        await _until(
          tester,
          () => api.originals.length == 1,
          'native source submitted',
        );
        final original = api.originals.single;
        original.complete();
        api.published.addAll([original.source, original.reply]);
        await state.refreshNotices();
        await _until(
          tester,
          () => audio.queued == 1,
          'actual UI queues original native reply PCM',
        );
        expect(audio.playing, isTrue);
        api.interruptGate = Completer<void>();
        final interrupts = api.interrupts, stops = audio.stops;
        audio.events.add({'type': 'barge', 'epoch': audio.epoch});
        await _until(
          tester,
          () => audio.stops > stops,
          'local PCM stop before interrupt completion',
        );
        expect(audio.playing, isFalse);
        expect(api.interrupts, interrupts + 1);
        expect(api.interruptGate!.isCompleted, isFalse);
        expect(api.originals.single, same(original));
        _noLegacy(api);
      } finally {
        await _close(tester, state, api, audio);
      }
    },
  );

  for (final fence in ['stop', 'reconnect', 'session', 'source']) {
    testWidgets(
      'held native finish cannot export after $fence ownership fence',
      (tester) async {
        final api = _Api();
        final audio = _Audio()..finishGate = Completer<RecordedAudio?>();
        final state = await _mount(tester, api, audio);
        try {
          await _call(tester);
          await _voice(tester, audio);
          await _end(tester, audio);
          await _until(
            tester,
            () => audio.finishEntered.isCompleted,
            'original finish is held',
          );
          if (fence == 'stop') {
            await _call(tester);
          } else if (fence == 'reconnect') {
            final profile = state.connection!;
            state.disconnect();
            state.connect(profile, injected: _Api());
          } else if (fence == 'session') {
            final before = state.converseSession!;
            state.converseSession = SessionInfo(
              sessionId: before.sessionId,
              threadId: before.threadId,
              epoch: before.epoch + 1,
              channel: 'converse',
            );
          } else {
            state.applyConverseHealth(old.health(available: false));
          }
        } finally {
          if (!audio.finishGate!.isCompleted) {
            audio.finishGate!.complete(RecordedAudio('audio/wav', _wav(131)));
          }
          await tester.pump(const Duration(milliseconds: 300));
          try {
            expect(api.originals, isEmpty);
            _noLegacy(api);
            expect(audio.queued, 0);
          } finally {
            await _close(tester, state, api, audio);
          }
        }
      },
    );
  }

  testWidgets(
    'input selector is browser-local and refuses an active capture change',
    (tester) async {
      final api = _Api();
      final audio = _Audio();
      final state = await _mount(tester, api, audio);
      try {
        await tester.tap(find.byKey(const ValueKey('voice-options')));
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 300));
        await tester.ensureVisible(
          find.byKey(const ValueKey('audio-input-refresh')),
        );
        await tester.pump();
        await tester.tap(find.byKey(const ValueKey('audio-input-refresh')));
        await tester.pump(const Duration(milliseconds: 100));
        expect(audio.discoveries, greaterThan(0));
        expect(audio.arms, 0);
        await tester.ensureVisible(
          find.byKey(const ValueKey('audio-input-device')),
        );
        await tester.pump();
        await tester.tap(find.byKey(const ValueKey('audio-input-device')));
        await tester.pump();
        await tester.tap(find.text('Synthetic input B').last);
        await tester.pump(const Duration(milliseconds: 100));
        expect(audio.successfulSelections, ['opaque-input-b']);
        await tester.ensureVisible(
          find.byKey(const ValueKey('audio-input-default')),
        );
        await tester.pump();
        await tester.tap(find.byKey(const ValueKey('audio-input-default')));
        await tester.pump();
        expect(audio.successfulSelections, ['opaque-input-b', 'default']);
        expect(api.originals, isEmpty);
        _noLegacy(api);
        await tester.tap(find.byTooltip('Close voice options'));
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 300));
        await _call(tester);
        final selectedBefore = audio.successfulSelections.length;
        await tester.tap(find.byKey(const ValueKey('voice-options')));
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 300));
        expect(
          find.byKey(const ValueKey('audio-input-notice')),
          findsOneWidget,
        );
        await tester.ensureVisible(
          find.byKey(const ValueKey('audio-input-default')),
        );
        await tester.pump();
        await tester.tap(find.byKey(const ValueKey('audio-input-default')));
        await tester.pump();
        expect(audio.successfulSelections.length, selectedBefore);
      } finally {
        await _close(tester, state, api, audio);
      }
    },
  );
}
