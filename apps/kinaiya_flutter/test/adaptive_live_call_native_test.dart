import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/io_client.dart';
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/audio/speech_audio.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/adaptive_live_capture.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/character_view.dart';

import 'capture_modes_test.dart' show nativeCaptureFixture;
import 'live_capture_test.dart' show CaptureAudio, ControlledApi;

class _NativeLoopbackHttp extends HttpOverrides {
  @override
  HttpClient createHttpClient(SecurityContext? context) =>
      super.createHttpClient(context)..findProxy = (_) => 'DIRECT';
}

class _ObservedNotices extends http.BaseClient {
  _ObservedNotices(this.entries);
  final List<Map<String, Object?>> entries;
  final http.Client inner = IOClient(
    _NativeLoopbackHttp().createHttpClient(null),
  );
  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) async {
    final result = await inner.send(request);
    if (!request.url.path.endsWith('/v1/transcript')) return result;
    final bytes = <int>[];
    final stream = result.stream.transform(
      StreamTransformer<List<int>, List<int>>.fromHandlers(
        handleData: (chunk, sink) {
          bytes.addAll(chunk);
          if (bytes.length > 2 * 1024 * 1024) {
            throw StateError('Public notice fixture exceeds bound');
          }
          sink.add(chunk);
        },
        handleDone: (sink) {
          final value = jsonDecode(utf8.decode(bytes));
          if (value is List) {
            entries.addAll(
              value.map((e) => (e as Map).cast<String, Object?>()),
            );
          }
          sink.close();
        },
      ),
    );
    return http.StreamedResponse(
      stream,
      result.statusCode,
      headers: result.headers,
      contentLength: result.contentLength,
      request: result.request,
      reasonPhrase: result.reasonPhrase,
      isRedirect: result.isRedirect,
      persistentConnection: result.persistentConnection,
    );
  }

  @override
  void close() => inner.close();
}

class PublicPcmAudio extends CaptureAudio
    implements SegmentedPcmCaptureAudio, IncrementalSpeakingAudio {
  bool armed = false, configured = false, speaking = true;
  int? pcmEpoch;
  int received = 0, offset = 0, sequence = 1;
  int arms = 0, acknowledgments = 0, releases = 0, armStops = 0;
  final armStopStacks = <StackTrace>[];
  Completer<void>? heldStop;
  String rotationFault = '';
  IncrementalPcmRotation? lastRotation;
  IncrementalPcmPacket? pending;
  Completer<IncrementalPcmState>? waiting;

  @override
  Future<bool> get availablePlayback async => false;
  @override
  bool get supportsIncrementalPcmCapture => true;
  @override
  bool get supportsIncrementalPcmRollover => true;
  @override
  bool get supportsIncrementalSpeaking => true;
  @override
  Future<void> configureIncrementalPcmCapture(bool enabled) async {
    expect(armed, isFalse);
    expect(pcmEpoch, isNull);
    configured = enabled;
  }

  @override
  Future<String> startArmed() async {
    expect(configured, isTrue);
    arms++;
    armed = true;
    pcmEpoch = ++epoch;
    return 'armed';
  }

  @override
  Future<void> stopArmed() async {
    armStopStacks.add(StackTrace.current);
    armStops++;
    if (heldStop != null) await heldStop!.future;
    armed = false;
    _wake();
  }

  IncrementalPcmState state() => IncrementalPcmState.fromJson({
    'capture_epoch': pcmEpoch,
    'sample_rate': 16000,
    'buffered_samples': received - offset,
    'received_samples': received,
    'sample_offset': offset,
    'next_sequence': sequence,
    'active': armed,
    'fault': null,
    'cleanup_pending': false,
    'max_stream_samples': maxIncrementalPcmSamples,
    'max_chunk_samples': maxIncrementalPcmChunkSamples,
  });
  void _wake() {
    final original = waiting;
    waiting = null;
    original?.complete(state());
  }

  void feed([int count = 160]) {
    expect(armed, isTrue);
    received += count;
    _wake();
  }

  @override
  Future<IncrementalPcmState> incrementalPcmState() async => state();
  @override
  Future<IncrementalPcmPacket?> peekIncrementalPcm() async {
    if (pending != null) return pending;
    if (received == offset) return null;
    final count = min(received - offset, maxIncrementalPcmChunkSamples);
    return pending = IncrementalPcmPacket.fromJson({
      'capture_epoch': pcmEpoch,
      'sequence': sequence,
      'sample_offset': offset,
      'sample_count': count,
      'sample_rate': 16000,
      'user_speaking': speaking,
      'pcm16_base64': base64Encode(Uint8List(count * 2)),
    });
  }

  @override
  Future<void> acknowledgeIncrementalPcm(IncrementalPcmPacket original) async {
    expect(original, same(pending));
    expect(original.epoch, pcmEpoch);
    offset += original.sampleCount;
    sequence++;
    acknowledgments++;
    pending = null;
  }

  @override
  Future<IncrementalPcmState> waitIncrementalPcm(int captureEpoch) {
    expect(captureEpoch, pcmEpoch);
    if (!armed || received > offset) return Future.value(state());
    return (waiting ??= Completer<IncrementalPcmState>()).future;
  }

  @override
  Future<bool> incrementalUserSpeaking(int captureEpoch) async {
    expect(captureEpoch, pcmEpoch);
    if (!armed) throw StateError('Public synthetic input is stopped');
    return speaking;
  }

  @override
  Future<IncrementalPcmRotation> rotateIncrementalPcm(
    IncrementalPcmBoundary original,
  ) async {
    final previous = lastRotation;
    if (previous != null &&
        previous.epoch == pcmEpoch &&
        previous.original.matches(original)) {
      return previous;
    }
    if (!armed || rotationFault == 'refused') {
      throw StateError('Public synthetic local rotation refused');
    }
    expect(original.epoch, pcmEpoch);
    expect(original.sampleOffset, offset);
    expect(original.nextSequence, sequence);
    expect(pending, isNull);
    _wake();
    received -= offset;
    offset = 0;
    sequence = 1;
    pcmEpoch = ++epoch;
    final receipt = lastRotation = IncrementalPcmRotation.fromJson({
      'schema_version': '1.0',
      'protocol': 'browser_pcm_segment_v1',
      'original': original.body,
      'capture_epoch': pcmEpoch,
    }, original);
    if (rotationFault == 'lost') {
      rotationFault = '';
      throw StateError('Public synthetic rotation receipt lost');
    }
    return receipt;
  }

  @override
  Future<void> releaseIncrementalPcm(int captureEpoch) async {
    expect(armed, isFalse);
    expect(captureEpoch, pcmEpoch);
    pcmEpoch = null;
    pending = null;
    received = offset = 0;
    sequence = 1;
    releases++;
  }
}

Future<void> nativeSpin(WidgetTester tester, bool Function() done) async {
  for (var i = 0; i < 500 && !done(); i++) {
    await tester.runAsync(
      () => Future<void>.delayed(const Duration(milliseconds: 10)),
    );
    await tester.pump(const Duration(milliseconds: 16));
  }
  expect(done(), isTrue);
}

void main() {
  final url = Platform.environment['KINAIYA_LIVE_STREAM_URL'];
  final token = Platform.environment['KINAIYA_LIVE_STREAM_TOKEN'];
  final scope = Platform.environment['KINAIYA_LIVE_STREAM_SCOPE'];
  final consumerRequested =
      Platform.environment['KINAIYA_CALL_LIVE_CONSUMER'] == 'true';
  final rotationFault =
      Platform.environment['KINAIYA_CALL_ROTATION_FAULT'] ?? '';
  final heldReply = Platform.environment['KINAIYA_CALL_HELD_REPLY'] == 'true';
  testWidgets(
    'actual Call retains held microphone stop through disconnect and late PCM',
    (tester) async {
      final raw = jsonDecode(scope!) as Map<String, Object?>;
      final snapshot = {
        ...nativeCaptureFixture()['snapshot'] as Map<String, Object?>,
        'default_mode': 'adaptive_live',
        'selected_mode': 'adaptive_live',
        'adaptive_live_enabled': true,
      };
      final api = ControlledApi(
        serviceBase: '$url/session',
        converseBase: url!,
        ownerToken: token!,
        liveFactory: () =>
            IOClient(_NativeLoopbackHttp().createHttpClient(null)),
        journalFactory: () =>
            IOClient(_NativeLoopbackHttp().createHttpClient(null)),
        captureFactory: () => MockClient((request) async {
          expect(request.method, 'GET');
          return http.Response(
            jsonEncode(snapshot),
            200,
            headers: {'content-type': 'application/json'},
          );
        }),
      );
      final audio = PublicPcmAudio();
      final activities = <VoiceActivity>[];
      final state = AppState()
        ..connect(ConnectionProfile.defaults(), injected: api)
        ..converseSession = SessionInfo(
          sessionId: raw['session_id'] as String,
          threadId: raw['thread_id'] as String,
          channel: 'converse',
          epoch: raw['epoch'] as int,
        );
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: CharacterView(
              state: state,
              audio: audio,
              onActivityChanged: activities.add,
            ),
          ),
        ),
      );
      await tester.pump(const Duration(milliseconds: 100));
      await tester.tap(find.byTooltip('Start Live listen (armed mic)'));
      await nativeSpin(
        tester,
        () => activities.any((activity) => activity.listening),
      );
      expect(audio.arms, 1);
      expect(audio.clips, 0);
      audio.feed();
      await nativeSpin(tester, () => audio.acknowledgments == 1);
      audio.feed();
      await nativeSpin(tester, () => audio.acknowledgments == 2);
      expect(audio.armed, isTrue);
      expect(audio.speaking, isTrue);
      expect(audio.received, 320);
      final stopped = audio.heldStop = Completer<void>();
      await tester.tap(find.byTooltip('Stop Live listen'));
      await tester.pump(const Duration(seconds: 4));
      expect(activities.last.stopUnconfirmed, isTrue);
      expect(AdaptiveLiveCapture.hasRetainedCapture, isTrue);
      expect(audio.armed, isTrue);
      expect(audio.releases, 0);
      expect(audio.arms, 1);
      // A late physical packet remains local after admission is fenced.
      audio.feed();
      state.disconnect();
      await tester.pump();
      expect(audio.acknowledgments, 2);
      expect(AdaptiveLiveCapture.hasRetainedCapture, isTrue);
      expect(api.openJournalWake, throwsStateError);
      stopped.complete();
      await nativeSpin(tester, () => !AdaptiveLiveCapture.hasRetainedCapture);
      expect(audio.armed, isFalse);
      expect(audio.releases, 1);
      expect(audio.configured, isFalse);
      expect(audio.clips, 0);
      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
    },
    skip: url == null || token == null || scope == null || consumerRequested,
  );

  testWidgets(
    heldReply
        ? 'actual buffered noninterrupting reply preserves its utterance after provider completion'
        : rotationFault.isEmpty
        ? 'actual empty backchannel preserves capture and empty full reply advances without playback'
        : 'actual $rotationFault local rotation retires its original capture',
    (tester) async {
      final raw = jsonDecode(scope!) as Map<String, Object?>;
      final observed = <Map<String, Object?>>[];
      final snapshot = {
        ...nativeCaptureFixture()['snapshot'] as Map<String, Object?>,
        'default_mode': 'adaptive_live',
        'selected_mode': 'adaptive_live',
        'adaptive_live_enabled': true,
      };
      final api = ControlledApi(
        serviceBase: '$url/session',
        converseBase: url!,
        ownerToken: token!,
        liveFactory: () =>
            IOClient(_NativeLoopbackHttp().createHttpClient(null)),
        journalFactory: () => _ObservedNotices(observed),
        captureFactory: () => MockClient(
          (_) async => http.Response(
            jsonEncode(snapshot),
            200,
            headers: {'content-type': 'application/json'},
          ),
        ),
      );
      final audio = PublicPcmAudio()..rotationFault = rotationFault;
      final activities = <VoiceActivity>[];
      final state = AppState()
        ..connect(ConnectionProfile.defaults(), injected: api)
        ..converseSession = SessionInfo(
          sessionId: raw['session_id'] as String,
          threadId: raw['thread_id'] as String,
          channel: 'converse',
          epoch: raw['epoch'] as int,
        );
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: CharacterView(
              state: state,
              audio: audio,
              onActivityChanged: activities.add,
            ),
          ),
        ),
      );
      await tester.pump(const Duration(milliseconds: 100));
      await tester.tap(find.byTooltip('Start Live listen (armed mic)'));
      await nativeSpin(tester, () => activities.any((a) => a.listening));
      audio.feed();
      await nativeSpin(tester, () => audio.acknowledgments == 1);
      bool observedEffect(String effect) => observed.any((entry) {
        final payload = entry['payload'] as Map<String, Object?>;
        return payload['phase'] == 'live_output_observed' &&
            payload['live_effect'] == effect;
      });
      await nativeSpin(tester, () => observedEffect('converse_backchannel'));
      await tester.pump();
      expect(audio.epoch, 1);
      expect(
        audio.armed,
        isTrue,
        reason:
            '${audio.armStopStacks.join('\n')}\n'
            'resourceStale=${state.resourceStatusStale},enabled=${state.converseEnabled},authenticated=${state.sessionAuthenticated}',
      );
      expect(audio.speaking, isTrue);
      audio.feed();
      await nativeSpin(tester, () => audio.acknowledgments == 2);
      if (heldReply) {
        await nativeSpin(tester, () => observedEffect('converse_reply'));
        await tester.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 50)),
        );
        await tester.pump();
        expect(audio.epoch, 1);
        expect(audio.arms, 1);
        expect(audio.speaking, isTrue);
        expect(audio.armed, isTrue);
        final full = observed.singleWhere((entry) {
          final payload = entry['payload'] as Map;
          return payload['phase'] == 'live_output_observed' &&
              payload['live_effect'] == 'converse_reply';
        });
        expect(
          (full['payload'] as Map)['provider_output'],
          'Public synthetic buffered reply.',
        );
        expect((full['payload'] as Map)['published_chunks'], 0);
        await tester.tap(find.byTooltip('Stop Live listen'));
        await nativeSpin(tester, () => !AdaptiveLiveCapture.hasRetainedCapture);
        expect(audio.releases, 1);
        await tester.pumpWidget(const SizedBox.shrink());
        state.dispose();
        return;
      }
      if (rotationFault.isNotEmpty) {
        await nativeSpin(tester, () => !AdaptiveLiveCapture.hasRetainedCapture);
        expect(observedEffect('converse_reply'), isTrue);
        expect(audio.epoch, rotationFault == 'lost' ? 2 : 1);
        expect(audio.armed, isFalse);
        expect(audio.releases, 1);
        expect(audio.arms, 1);
        expect(audio.clips, 0);
        await tester.pumpWidget(const SizedBox.shrink());
        state.dispose();
        return;
      }
      await nativeSpin(tester, () => audio.epoch == 2);
      expect(observedEffect('converse_reply'), isTrue);
      expect(audio.arms, 1);
      expect(audio.speaking, isTrue);
      final outputs = observed.where(
        (entry) => (entry['payload'] as Map)['phase'] == 'live_output_observed',
      );
      expect(outputs, hasLength(2));
      expect(
        outputs.every((e) => (e['payload'] as Map)['provider_output'] == ''),
        isTrue,
      );
      audio.feed();
      await nativeSpin(tester, () => audio.acknowledgments == 3);
      expect(audio.armed, isTrue);
      await tester.tap(find.byTooltip('Stop Live listen'));
      await nativeSpin(tester, () => !AdaptiveLiveCapture.hasRetainedCapture);
      expect(audio.releases, 1);
      expect(audio.arms, 1);
      expect(audio.clips, 0);
      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
    },
    skip: url == null || token == null || scope == null || !consumerRequested,
  );
}
