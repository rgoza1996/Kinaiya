import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/character_view.dart';

import 'live_capture_test.dart' show CaptureAudio;

const eventId = 'synthetic-multiframe-entry';
const chunkId = 'synthetic-sentence';
const generation = 17;

/// Actual KinaiyaClient NDJSON decoding over controlled in-memory HTTP bytes.
/// This transport never opens a socket or calls a synthesis provider.
class NdjsonPlaybackTransport extends http.BaseClient {
  NdjsonPlaybackTransport() {
    frames.onCancel = () => responseReleased = true;
  }

  final frames = StreamController<List<int>>();
  bool responseReleased = false;
  final speechRequests = <Map<String, Object?>>[];
  final deliveries = <Map<String, Object?>>[];
  final interruptions = <Map<String, Object?>>[];
  final unexpected = <String>[];

  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) async {
    final raw = await request.finalize().bytesToString();
    final body = raw.isEmpty
        ? <String, Object?>{}
        : (jsonDecode(raw) as Map).cast<String, Object?>();
    final path = request.url.path;
    if (path == '/health') return _json({'enabled': true});
    expect(request.headers['Authorization'], 'Bearer synthetic-owner');
    switch (path) {
      case '/v1/transcript':
        return _json([
          {
            'schema_version': '1.0',
            'event_id': eventId,
            'sequence': 1,
            'session_id': 'converse',
            'thread_id': 'thread-converse',
            'epoch': 1,
            'kind': 'conversation.converse',
            'origin': 'converse',
            'idempotency_key': 'synthetic-notice',
            'payload': {
              'text': 'A public synthetic reply in several PCM frames.',
              'chunk_id': chunkId,
              'generation_epoch': generation,
              'outcome_event_id': 'synthetic-verified-outcome',
              'delivery': 'generated',
            },
          },
        ]);
      case '/v1/converse/speech':
        speechRequests.add(body);
        return http.StreamedResponse(
          frames.stream,
          200,
          headers: {'content-type': 'application/x-ndjson'},
        );
      case '/v1/converse/delivery':
        deliveries.add(body);
        return _json({});
      case '/v1/converse/interrupt':
        interruptions.add(body);
        return _json({});
      default:
        unexpected.add(path);
        return _json({'detail': 'Unexpected synthetic route'}, status: 500);
    }
  }

  http.StreamedResponse _json(Object value, {int status = 200}) =>
      http.StreamedResponse(
        Stream.value(utf8.encode(jsonEncode(value))),
        status,
        headers: {'content-type': 'application/json'},
      );

  void addFrames(Iterable<Map<String, Object?>> values) {
    frames.add(
      utf8.encode(values.map((value) => '${jsonEncode(value)}\n').join()),
    );
  }

  @override
  void close() {
    if (!frames.isClosed) unawaited(frames.close());
  }
}

/// Completion represents the injected backend's source-ended receipt only.
/// No physical speaker, microphone, or browser audibility is simulated.
class CompletionAudio extends CaptureAudio {
  final queued = <({String pcm, int sampleRate})>[];
  final ended = Completer<void>();
  int playbackWaits = 0;

  @override
  Future<bool> queuePcm(String value, int rate) async {
    queued.add((pcm: value, sampleRate: rate));
    return true;
  }

  @override
  Future<void> awaitPlayback() {
    playbackWaits++;
    return ended.future;
  }

  @override
  Future<void> stopPlayback() async {
    stops++;
    if (!ended.isCompleted) ended.complete();
  }
}

Map<String, Object?> frame(int rate, int samples, int value, int index) => {
  'type': 'audio',
  'audio': base64Encode(List<int>.filled(samples * 2, value)),
  'sample_rate': rate,
  'samples': samples,
  'chunk_index': index,
  'event_id': eventId,
  'generation_epoch': generation,
  'chunk_id': chunkId,
};

Future<void> pumpUntil(WidgetTester tester, bool Function() ready) async {
  for (var attempt = 0; attempt < 30 && !ready(); attempt++) {
    await tester.pump(const Duration(milliseconds: 10));
  }
  expect(
    ready(),
    isTrue,
    reason: 'Controlled local async operation completed.',
  );
}

Future<AppState> mount(
  WidgetTester tester,
  NdjsonPlaybackTransport transport,
  CompletionAudio audio,
) async {
  tester.view.physicalSize = const Size(1100, 1000);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  final api = KinaiyaClient(
    sessionBase: 'http://fixture',
    agentBase: 'http://fixture',
    converseBase: 'http://fixture',
    ownerToken: 'synthetic-owner',
    httpClient: transport,
  );
  final state = AppState()
    ..connect(ConnectionProfile.defaults(), injected: api)
    ..converseSession = SessionInfo(
      sessionId: 'converse',
      threadId: 'thread-converse',
      channel: 'converse',
      epoch: 1,
    );
  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(
        body: CharacterView(state: state, audio: audio, showCharacter: false),
      ),
    ),
  );
  await tester.pump();
  await state.refreshNotices();
  await pumpUntil(tester, () => transport.speechRequests.isNotEmpty);
  expect(transport.speechRequests, [
    {'event_id': eventId, 'generation_epoch': generation},
  ]);
  return state;
}

Future<void> finish(WidgetTester tester, AppState state) async {
  await tester.pumpWidget(const SizedBox.shrink());
  await tester.pump();
  state.dispose();
  await tester.pump();
}

void expectAuthority(Map<String, Object?> receipt) {
  expect(receipt['schema_version'], '1.0');
  expect(receipt['event_id'], eventId);
  expect(receipt['generation_epoch'], generation);
  expect(receipt['chunk_id'], chunkId);
}

void main() {
  for (final rate in [44100, 24000]) {
    testWidgets(
      '$rate Hz NDJSON frames queue in order and require stream end plus backend completion before played',
      (tester) async {
        final transport = NdjsonPlaybackTransport();
        final audio = CompletionAudio();
        final state = await mount(tester, transport, audio);
        final values = [
          frame(rate, rate ~/ 10, 0, 0),
          frame(rate, rate ~/ 5, 1, 1),
          frame(rate, rate ~/ 20, 2, 2),
        ];
        transport.addFrames(values.take(1));
        await pumpUntil(tester, () => transport.deliveries.isNotEmpty);
        expect(audio.queued.single.sampleRate, rate);
        expect(audio.playbackWaits, 0);
        expect(transport.deliveries.map((value) => value['status']), [
          'playing',
        ]);
        expectAuthority(transport.deliveries.single);

        // Two NDJSON records in one transport fragment still queue separately.
        transport.addFrames(values.skip(1));
        await pumpUntil(tester, () => audio.queued.length == 3);
        expect(audio.queued.map((value) => value.sampleRate), [
          rate,
          rate,
          rate,
        ]);
        expect(
          audio.queued.map((value) => value.pcm),
          values.map((value) => value['audio']),
        );
        expect(
          audio.playbackWaits,
          0,
          reason: 'The NDJSON body is still open.',
        );
        expect(
          transport.deliveries.where((value) => value['status'] == 'played'),
          isEmpty,
        );

        final closed = transport.frames.close();
        await pumpUntil(tester, () => audio.playbackWaits == 1);
        await closed;
        expect(audio.ended.isCompleted, isFalse);
        expect(
          transport.deliveries.where((value) => value['status'] == 'played'),
          isEmpty,
        );
        audio.ended.complete();
        await pumpUntil(
          tester,
          () =>
              transport.deliveries.any((value) => value['status'] == 'played'),
        );
        expect(transport.deliveries.map((value) => value['status']), [
          'playing',
          'played',
        ]);
        final played = transport.deliveries.last;
        expectAuthority(played);
        expect(played['played_seconds'], closeTo(0.35, 0.000001));
        expect(transport.unexpected, isEmpty);
        expect(tester.takeException(), isNull);
        await finish(tester, state);
      },
    );
  }

  testWidgets(
    'interrupt after first 44100 Hz frame fences later NDJSON frames and never acknowledges played',
    (tester) async {
      final transport = NdjsonPlaybackTransport();
      final audio = CompletionAudio();
      final state = await mount(tester, transport, audio);
      transport.addFrames([frame(44100, 4410, 0, 0)]);
      await pumpUntil(tester, () => transport.deliveries.isNotEmpty);
      final oldNonce = state.turnNonce;
      await tester.tap(find.byTooltip('Stop speech'));
      await pumpUntil(tester, () => transport.interruptions.isNotEmpty);
      expect(audio.stops, 1);
      expect(state.turnNonce, greaterThan(oldNonce));
      expect(state.speechEpoch, generation + 1);
      expect(transport.interruptions, [
        {'session_id': 'converse', 'expected_generation': generation},
      ]);
      transport.addFrames([frame(44100, 8820, 1, 1), frame(44100, 2205, 2, 2)]);
      // Finish the finite response so the decoder can consume or discard all
      // late records. onCancel also runs on normal stream completion.
      final closed = transport.frames.close();
      await pumpUntil(tester, () => transport.responseReleased);
      await closed;
      await tester.pump();
      expect(audio.queued, hasLength(1));
      expect(audio.queued.single.sampleRate, 44100);
      expect(audio.playbackWaits, 0);
      for (final receipt in transport.deliveries) {
        expectAuthority(receipt);
      }
      expect(
        transport.deliveries.where((value) => value['status'] == 'played'),
        isEmpty,
      );
      expect(transport.unexpected, isEmpty);
      expect(tester.takeException(), isNull);
      await finish(tester, state);
    },
  );
}
