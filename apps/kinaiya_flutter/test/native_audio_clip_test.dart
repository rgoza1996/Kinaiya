import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:crypto/crypto.dart' as crypto;
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/audio/speech_audio.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/state/native_audio_clip.dart';

import 'input_disclosure_test.dart'
    show ScopeClient, ImportAudio, withClient, character;

const utterance = 'b263f86c-8d0a-493a-972f-e8c7473e5c04';
const requestId = '202c67eb-adab-461b-80aa-23e9633bfca0';

SessionInfo session() => SessionInfo(
  sessionId: 'synthetic-session',
  threadId: 'synthetic-thread',
  channel: 'converse',
  epoch: 3,
);

Uint8List wav([int samples = 16]) {
  final bytes = Uint8List(44 + samples * 2);
  final data = ByteData.sublistView(bytes);
  void u16(int at, int value) => data.setUint16(at, value, Endian.little);
  void u32(int at, int value) => data.setUint32(at, value, Endian.little);
  u32(0, 0x46464952);
  u32(4, bytes.length - 8);
  u32(8, 0x45564157);
  u32(12, 0x20746d66);
  u32(16, 16);
  u16(20, 1);
  u16(22, 1);
  u32(24, 16000);
  u32(28, 32000);
  u16(32, 2);
  u16(34, 16);
  u32(36, 0x61746164);
  u32(40, samples * 2);
  return bytes;
}

Map<String, Object?> health({bool available = true}) => {
  'enabled': true,
  'native_audio': {
    'configured': true,
    'available': available,
    'mode': 'buffered_native_audio',
    'input_sample_rate': 16000,
    'max_seconds': 30,
  },
};

JournalEntry source() => JournalEntry(
  eventId: 'source-one',
  sequence: 4,
  sessionId: session().sessionId,
  threadId: session().threadId,
  epoch: 3,
  kind: 'conversation.window',
  origin: 'authenticated-user',
  payload: {
    'utterance_id': utterance,
    'capture_ended': true,
    'user_speaking': false,
    'window_index': 0,
    'revision': 1,
    'text': 'Public synthetic spoken task.',
    'audio_sha256': crypto.sha256.convert(wav()).toString(),
    'content_scope': 'public_synthetic',
    'start_ms': 0,
    'end_ms': 1,
  },
);

JournalEntry output({String sourceId = 'source-one', bool native = true}) =>
    JournalEntry(
      eventId: 'reply-one',
      sequence: 6,
      sessionId: session().sessionId,
      threadId: session().threadId,
      epoch: 3,
      kind: 'conversation.converse',
      origin: 'converse',
      payload: {
        'native_audio': native,
        'window_utterance_id': utterance,
        'window_source_id': sourceId,
        'generation_epoch': 5,
        'runtime_id': 'runtime-one',
        'chunk_id': 'chunk-one',
        'text': 'Give me some time.',
      },
    );

NativeAudioClipOwner owner(Object client) => NativeAudioClipOwner(
  client: client,
  contentScope: ContentScope.publicSynthetic,
  connectionGeneration: 2,
  sourceGeneration: 7,
  session: session(),
  utteranceId: utterance,
  requestId: requestId,
  turnNonce: 9,
);

class NativeClipApi extends ScopeClient {
  NativeClipApi({this.receiptChanges = const {}});

  final Map<String, Object?> receiptChanges;
  bool ready = true, exposeEvents = false;
  List<int>? uploaded;
  ContentScope? uploadedScope;
  JournalEntry? original, result;
  final admission = Completer<NativeAudioClipAdmission>();
  final delivery = <String>[];

  @override
  Future<Map<String, Object?>> converseHealth() async =>
      health(available: ready);

  @override
  Future<void> interruptSpeech(String id, {int? expectedGeneration}) async {
    interrupts++;
  }

  @override
  Future<NativeAudioClipAdmission> submitNativeAudioClip({
    required SessionInfo session,
    required String utteranceId,
    required String requestId,
    required List<int> wav,
    required ContentScope contentScope,
  }) {
    uploaded = List<int>.of(wav);
    uploadedScope = contentScope;
    original = JournalEntry(
      eventId: 'native-source',
      sequence: 10,
      sessionId: session.sessionId,
      threadId: session.threadId,
      epoch: session.epoch,
      kind: 'conversation.window',
      origin: 'authenticated-user',
      payload: {
        'utterance_id': utteranceId,
        'capture_ended': true,
        'user_speaking': false,
        'window_index': 0,
        'revision': 1,
        'text': 'Public native task.',
        'audio_sha256': crypto.sha256.convert(wav).toString(),
        'content_scope': contentScope.wireValue,
        'start_ms': 0,
        'end_ms': ((wav.length - 44) / 32).ceil(),
        ...receiptChanges,
      },
    );
    result = JournalEntry(
      eventId: 'native-reply',
      sequence: 12,
      sessionId: session.sessionId,
      threadId: session.threadId,
      epoch: session.epoch,
      kind: 'conversation.converse',
      origin: 'converse',
      payload: {
        'native_audio': true,
        'window_utterance_id': utteranceId,
        'window_source_id': original!.eventId,
        'generation_epoch': 5,
        'runtime_id': 'native-runtime',
        'chunk_id': 'native-chunk',
        'text': 'Give me some time.',
      },
    );
    return admission.future;
  }

  @override
  Future<List<JournalEntry>> transcript({
    required String sessionId,
    int after = 0,
    int limit = 200,
  }) async => exposeEvents
      ? [original!, result!].where((entry) => entry.sequence > after).toList()
      : [];

  @override
  Stream<Map<String, Object?>> speech({
    required String eventId,
    required int generationEpoch,
  }) async* {
    yield {
      'audio': base64Encode([1, 0, 2, 0]),
      'sample_rate': 24000,
    };
  }

  @override
  Future<void> reportDelivery({
    required String eventId,
    required int generationEpoch,
    required String chunkId,
    required String status,
    double playedSeconds = 0,
  }) async {
    delivery.add(status);
  }
}

class NativeClipAudio extends ImportAudio {
  int queued = 0, armed = 0;
  final playback = Completer<void>();
  @override
  Future<String> startArmed() async {
    armed++;
    return 'armed';
  }

  @override
  Future<bool> queuePcm(String value, int rate) async {
    queued++;
    return true;
  }

  @override
  Future<void> awaitPlayback() => playback.future;
}

void main() {
  testWidgets(
    'native import owns fast polled audio without arming or legacy fallback',
    (tester) async {
      final api = NativeClipApi();
      final sound = NativeClipAudio();
      final state = withClient(api, BackendProfile.clean);
      await tester.pumpWidget(character(state, sound));
      await tester.pump(const Duration(milliseconds: 100));
      await tester.tap(find.byTooltip(nativeAudioImportHint));
      sound.chosen.complete(
        RecordedAudio('audio/wav', wav(), filename: 'public-native.wav'),
      );
      await tester.pump(const Duration(milliseconds: 100));
      expect(api.uploaded, wav());
      expect(api.transcriptionScopes, isEmpty);
      expect(api.turnScopes, isEmpty);
      api.exposeEvents = true;
      await state.refreshNotices();
      await tester.pump(const Duration(milliseconds: 50));
      expect(
        sound.queued,
        0,
        reason: 'Original POST admission is still unconfirmed',
      );
      api.admission.complete(NativeAudioClipAdmission(api.original!, false));
      await tester.pump(const Duration(milliseconds: 100));
      expect(sound.queued, 1);
      expect(sound.armed, 0);
      expect(api.delivery, contains('playing'));
      expect(api.delivery, isNot(contains('played')));
      sound.playback.complete();
      await tester.pump(const Duration(milliseconds: 100));
      expect(api.delivery, contains('played'));
      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
    },
  );

  for (final mismatch in <String, Map<String, Object?>>{
    'hash': {'audio_sha256': '0' * 64},
    'scope': {'content_scope': 'public_synthetic'},
    'duration': {'end_ms': 2},
    'start': {'start_ms': 1},
  }.entries) {
    testWidgets(
      'native receipt ${mismatch.key} mismatch never authorizes polled speech',
      (tester) async {
        final api = NativeClipApi(receiptChanges: mismatch.value);
        final sound = NativeClipAudio();
        final state = withClient(api, BackendProfile.clean);
        await tester.pumpWidget(character(state, sound));
        await tester.pump(const Duration(milliseconds: 100));
        await tester.tap(find.byTooltip(nativeAudioImportHint));
        sound.chosen.complete(
          RecordedAudio('audio/wav', wav(), filename: 'public-native.wav'),
        );
        await tester.pump(const Duration(milliseconds: 100));
        expect(api.uploaded, wav());
        if (mismatch.key == 'scope') {
          expect(
            api.original!.payload['content_scope'],
            isNot(api.uploadedScope!.wireValue),
            reason: 'The receipt fixture must actually differ from the request',
          );
        }
        api.exposeEvents = true;
        await state.refreshNotices();
        await tester.pump(const Duration(milliseconds: 50));
        api.admission.complete(NativeAudioClipAdmission(api.original!, false));
        await tester.pump(const Duration(milliseconds: 100));
        expect(sound.queued, 0);
        expect(sound.armed, 0);
        expect(api.delivery, isEmpty);
        expect(api.transcriptionScopes, isEmpty);
        expect(api.turnScopes, isEmpty);
        await tester.pumpWidget(const SizedBox.shrink());
        state.dispose();
      },
    );
  }

  for (final unavailable in [false, true]) {
    testWidgets(
      'native ${unavailable ? 'unavailability' : 'invalid WAV'} never falls back',
      (tester) async {
        final api = NativeClipApi()..ready = !unavailable;
        final sound = NativeClipAudio();
        final state = withClient(api, BackendProfile.clean);
        await tester.pumpWidget(character(state, sound));
        await tester.pump(const Duration(milliseconds: 100));
        await tester.tap(find.byTooltip(nativeAudioImportHint));
        if (!unavailable) {
          sound.chosen.complete(RecordedAudio('audio/webm', [1, 2, 3]));
        }
        await tester.pump(const Duration(milliseconds: 100));
        expect(api.uploaded, isNull);
        expect(api.transcriptionScopes, isEmpty);
        expect(api.turnScopes, isEmpty);
        expect(sound.armed, 0);
        await tester.pumpWidget(const SizedBox.shrink());
        state.dispose();
      },
    );
  }
  test(
    'canonical WAV is copied and bounded at the real thirty-second ceiling',
    () {
      final input = wav();
      final frozen = canonicalNativeAudioWav(input);
      input[44] = 12;
      expect(frozen[44], 0);
      expect(() => frozen[44] = 1, throwsUnsupportedError);
      expect(canonicalNativeAudioWav(wav(480000)).length, 960044);
      expect(() => canonicalNativeAudioWav(wav(480001)), throwsFormatException);
      expect(() => canonicalNativeAudioWav(wav(0)), throwsFormatException);
    },
  );

  for (final offset in [0, 4, 8, 12, 16, 20, 22, 24, 28, 32, 34, 36, 40]) {
    test('noncanonical WAV field at $offset cannot be silently converted', () {
      final invalid = wav()..[offset] ^= 1;
      expect(() => canonicalNativeAudioWav(invalid), throwsFormatException);
    });
  }

  test(
    'native health distinguishes unknown, legacy and malformed declarations',
    () {
      expect(const NativeAudioSupport.unknown().known, false);
      final legacy = NativeAudioSupport.fromHealth({'enabled': true});
      expect(legacy.known, true);
      expect(legacy.configured, false);
      expect(NativeAudioSupport.fromHealth(health()).available, true);
      final unavailable = NativeAudioSupport.fromHealth(
        health(available: false),
      );
      expect(unavailable.configured, true);
      expect(unavailable.available, false);
      for (final bad in [
        null,
        false,
        {'configured': true},
        {
          'configured': false,
          'available': true,
          'mode': 'buffered_native_audio',
          'input_sample_rate': 16000,
          'max_seconds': 30,
        },
      ]) {
        final malformed = NativeAudioSupport.fromHealth({'native_audio': bad});
        expect(malformed.configured, true);
        expect(malformed.available, false);
      }
    },
  );

  test(
    'a fast polled output waits for its exact original admission source',
    () {
      final clip = owner(Object());
      final reply = output();
      expect(clip.candidate(reply), true);
      expect(clip.accepts(reply), false);
      clip.defer(reply);
      clip.defer(reply);
      clip.confirm(source());
      final pending = clip.takePending();
      expect(pending.length, 1);
      expect(clip.accepts(pending.single), true);
      expect(clip.accepts(output(sourceId: 'another-source')), false);
      expect(clip.accepts(output(native: false)), false);
      expect(clip.takePending(), isEmpty);
    },
  );

  test(
    'import permission cannot survive a client, session, source or turn change',
    () {
      final client = Object();
      final clip = owner(client);
      bool current({
        Object? api,
        int connection = 2,
        int source = 7,
        SessionInfo? active,
        int nonce = 9,
      }) => clip.current(
        currentClient: api ?? client,
        connection: connection,
        source: source,
        session: active ?? session(),
        nonce: nonce,
      );
      expect(current(), true);
      expect(current(api: Object()), false);
      expect(current(connection: 3), false);
      expect(current(source: 8), false);
      expect(current(nonce: 10), false);
      expect(
        current(
          active: SessionInfo(
            sessionId: session().sessionId,
            threadId: session().threadId,
            channel: 'converse',
            epoch: 4,
          ),
        ),
        false,
      );
    },
  );

  test('health loss and disconnect retire native source generations', () {
    final state = AppState();
    state.applyConverseHealth(health());
    final generation = state.nativeAudioSourceGeneration;
    state.applyConverseHealth(health());
    expect(state.nativeAudioSourceGeneration, generation);
    state.applyConverseHealth({'enabled': true}, updateNativeAudio: false);
    expect(
      state.nativeAudio.available,
      true,
      reason: 'Owner control acknowledgements are not capability health reads',
    );
    state.applyConverseHealth(health(available: false));
    expect(state.nativeAudioSourceGeneration, greaterThan(generation));
    state.disconnect();
    expect(state.nativeAudio.known, false);
    state.dispose();
  });

  test(
    'native recording sends the original WAV once with exact owner metadata',
    () async {
      final calls = <http.Request>[];
      final transport = MockClient((request) async {
        calls.add(request);
        return http.Response(
          jsonEncode({
            'replay': false,
            'entry': {
              'schema_version': '1.0',
              'event_id': 'source-one',
              'sequence': 4,
              'session_id': session().sessionId,
              'thread_id': session().threadId,
              'epoch': 3,
              'kind': 'conversation.window',
              'origin': 'authenticated-user',
              'causes': [],
              'task_refs': [],
              'payload': source().payload,
            },
          }),
          200,
        );
      });
      final api = KinaiyaClient(
        sessionBase: 'http://session.invalid',
        agentBase: 'http://agent.invalid',
        converseBase: 'http://converse.invalid',
        ownerToken: 'public-synthetic-owner',
        httpClient: transport,
      );
      final bytes = canonicalNativeAudioWav(wav());
      final admitted = await api.submitNativeAudioClip(
        session: session(),
        utteranceId: utterance,
        requestId: requestId,
        wav: bytes,
        contentScope: ContentScope.publicSynthetic,
      );
      expect(admitted.entry.eventId, 'source-one');
      expect(admitted.replay, false);
      expect(calls.length, 1);
      expect(calls.single.url.path, '/v1/converse/native-audio/clips');
      expect(
        calls.single.headers['Authorization'],
        'Bearer public-synthetic-owner',
      );
      final payload = jsonDecode(calls.single.body) as Map<String, Object?>;
      expect(payload, {
        'session_id': session().sessionId,
        'thread_id': session().threadId,
        'epoch': 3,
        'utterance_id': utterance,
        'request_id': requestId,
        'content_scope': 'public_synthetic',
        'audio_base64': base64Encode(bytes),
      });
      api.close();
    },
  );
}
