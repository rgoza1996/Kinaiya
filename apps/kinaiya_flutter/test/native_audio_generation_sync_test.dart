// Independent native-generation synchronization oracles; synthetic audio only.
// Existing tests and the pending production synchronization patch are untouched.
import 'dart:async';
import 'dart:convert';

import 'package:crypto/crypto.dart' as crypto;
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/audio/speech_audio.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/state/native_audio_clip.dart';

import 'input_disclosure_test.dart' show ScopeClient, character, withClient;
import 'native_audio_clip_test.dart' show NativeClipAudio, health, wav;
import 'provider_consent_test.dart' show localProvider;
import 'window_capture_test.dart' show WindowApi, WindowAudio, arm;

class _Clip {
  _Clip(this.source, this.reply);
  final JournalEntry source, reply;
  final admitted = Completer<NativeAudioClipAdmission>();
}

JournalEntry _entry(
  JournalEntry original, {
  String? eventId,
  int? sequence,
  String? sessionId,
  String? threadId,
  int? epoch,
  String? origin,
  Map<String, Object?> payload = const {},
}) => JournalEntry(
  eventId: eventId ?? original.eventId,
  sequence: sequence ?? original.sequence,
  sessionId: sessionId ?? original.sessionId,
  threadId: threadId ?? original.threadId,
  epoch: epoch ?? original.epoch,
  kind: original.kind,
  origin: origin ?? original.origin,
  payload: {...original.payload, ...payload},
);

class _Api extends ScopeClient {
  int serverGeneration = 2;
  final clips = <_Clip>[];
  final entries = <JournalEntry>[];
  final speechRequests = <(String, int)>[];
  final delivery = <(String, String)>[];
  bool replayPage = false;

  @override
  Future<Map<String, Object?>> converseHealth() async => health();

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
    final ordinal = clips.length + 1;
    final original = JournalEntry(
      eventId: 'public-native-source-$ordinal',
      sequence: ordinal * 10,
      sessionId: session.sessionId,
      threadId: session.threadId,
      epoch: session.epoch,
      kind: 'conversation.window',
      origin: 'authenticated-user',
      payload: {
        'utterance_id': utteranceId,
        'window_index': 0,
        'revision': 1,
        'capture_ended': true,
        'user_speaking': false,
        'text': 'Public spoken task $ordinal.',
        'audio_sha256': crypto.sha256.convert(wav).toString(),
        'content_scope': contentScope.wireValue,
        'start_ms': 0,
        'end_ms': ((wav.length - 44) / 32).ceil(),
      },
    );
    final reply = JournalEntry(
      eventId: 'public-native-reply-$ordinal',
      sequence: ordinal * 10 + 2,
      sessionId: session.sessionId,
      threadId: session.threadId,
      epoch: session.epoch,
      kind: 'conversation.converse',
      origin: 'converse',
      payload: {
        'native_audio': true,
        'window_source_id': original.eventId,
        'window_utterance_id': utteranceId,
        'generation_epoch': serverGeneration,
        'runtime_id': 'public-runtime-after-restart',
        'chunk_id': 'public-native-chunk-$ordinal',
        'text': 'Give me some time on it.',
      },
    );
    final clip = _Clip(original, reply);
    clips.add(clip);
    return clip.admitted.future;
  }

  @override
  Future<List<JournalEntry>> transcript({
    required String sessionId,
    int after = 0,
    int limit = 200,
  }) async =>
      entries.where((entry) => replayPage || entry.sequence > after).toList();

  @override
  Stream<Map<String, Object?>> speech({
    required String eventId,
    required int generationEpoch,
  }) async* {
    speechRequests.add((eventId, generationEpoch));
    yield {
      'audio': base64Encode([1, 0, 2, 0]),
      'sample_rate': 24000,
      'index': 0,
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
    delivery.add((eventId, status));
  }
}

class _Audio extends NativeClipAudio {
  final selections = <Completer<RecordedAudio?>>[];
  final endings = <Completer<void>>[];

  @override
  Future<RecordedAudio?> chooseRecording() {
    final selection = Completer<RecordedAudio?>();
    selections.add(selection);
    return selection.future;
  }

  @override
  Future<void> awaitPlayback() {
    final ended = Completer<void>();
    endings.add(ended);
    return ended.future;
  }

  @override
  Future<void> stopPlayback() async {
    await super.stopPlayback();
    for (final ended in endings) {
      if (!ended.isCompleted) ended.complete();
    }
  }

  void ended() {
    expect(endings, isNotEmpty);
    expect(endings.last.isCompleted, false);
    endings.last.complete();
  }
}

Future<AppState> _mount(WidgetTester tester, _Api api, _Audio audio) async {
  final state = withClient(api, BackendProfile.clean);
  await tester.pumpWidget(character(state, audio));
  await tester.pump(const Duration(milliseconds: 100));
  // A previous server/runtime or a rejected clip can leave this optimistic
  // local counter ahead of the new server's independently owned generation.
  state.speechEpoch = 40;
  return state;
}

Future<_Clip> _import(WidgetTester tester, _Api api, _Audio audio) async {
  final before = api.clips.length;
  await tester.tap(find.byTooltip(nativeAudioImportHint));
  await tester.pump();
  expect(audio.selections, isNotEmpty);
  audio.selections.last.complete(
    RecordedAudio('audio/wav', wav(), filename: 'public-generation.wav'),
  );
  await tester.pump(const Duration(milliseconds: 100));
  expect(api.clips.length, before + 1);
  return api.clips.last;
}

Future<void> _poll(WidgetTester tester, AppState state) async {
  await state.refreshNotices();
  await tester.pump(const Duration(milliseconds: 100));
}

Future<void> _confirm(WidgetTester tester, _Clip clip) async {
  clip.admitted.complete(NativeAudioClipAdmission(clip.source, false));
  await tester.pump(const Duration(milliseconds: 100));
}

Future<void> _finish(WidgetTester tester, AppState state) async {
  await tester.pumpWidget(const SizedBox.shrink());
  state.dispose();
}

void main() {
  testWidgets(
    'a confirmed native clip adopts the lower server generation after an actual failed import',
    (tester) async {
      final api = _Api();
      final audio = _Audio();
      final state = await _mount(tester, api, audio);
      final failed = await _import(tester, api, audio);
      failed.admitted.completeError(
        ApiException(503, 'Public synthetic refusal'),
      );
      await tester.pump(const Duration(milliseconds: 100));
      expect(audio.queued, 0);
      expect(api.speechRequests, isEmpty);
      final clip = await _import(tester, api, audio);
      final optimistic = state.speechEpoch;
      expect(optimistic, greaterThan(api.serverGeneration));
      api.entries.addAll([clip.source, clip.reply]);
      await _poll(tester, state);
      expect(state.speechEpoch, optimistic);
      expect(
        audio.queued,
        0,
        reason: 'An unconfirmed original POST grants no playback',
      );
      await _confirm(tester, clip);
      expect(state.speechEpoch, api.serverGeneration);
      expect(audio.queued, 1);
      expect(audio.armed, 0);
      expect(api.speechRequests, [(clip.reply.eventId, api.serverGeneration)]);
      expect(api.delivery, contains((clip.reply.eventId, 'playing')));
      expect(api.delivery, isNot(contains((clip.reply.eventId, 'played'))));
      expect(api.transcriptionScopes, isEmpty);
      expect(api.turnScopes, isEmpty);
      audio.ended();
      await tester.pump(const Duration(milliseconds: 100));
      expect(api.delivery, contains((clip.reply.eventId, 'played')));
      expect(tester.takeException(), isNull);
      await _finish(tester, state);
    },
  );

  for (final change in [
    'origin',
    'session',
    'thread',
    'epoch',
    'utterance',
    'source',
    'not-native',
  ]) {
    testWidgets(
      'a foreign $change reply cannot lower the optimistic native generation',
      (tester) async {
        final api = _Api();
        final audio = _Audio();
        final state = await _mount(tester, api, audio);
        final clip = await _import(tester, api, audio);
        await _confirm(tester, clip);
        final optimistic = state.speechEpoch;
        final foreign = _entry(
          clip.reply,
          eventId: 'public-foreign-$change',
          origin: change == 'origin' ? 'agent-supervisor' : null,
          sessionId: change == 'session' ? 'foreign-session' : null,
          threadId: change == 'thread' ? 'foreign-thread' : null,
          epoch: change == 'epoch' ? clip.reply.epoch + 1 : null,
          payload: {
            if (change == 'utterance')
              'window_utterance_id': 'foreign-utterance',
            if (change == 'source') 'window_source_id': 'foreign-source',
            if (change == 'not-native') 'native_audio': false,
          },
        );
        api.entries.add(foreign);
        await _poll(tester, state);
        expect(state.speechEpoch, optimistic);
        expect(audio.queued, 0);
        expect(api.speechRequests, isEmpty);
        // A distinct, actually current reply remains a positive control after
        // the untrusted record. No old source is silently treated as admitted.
        api.entries.add(_entry(clip.reply, sequence: clip.reply.sequence + 1));
        await _poll(tester, state);
        expect(state.speechEpoch, api.serverGeneration);
        expect(audio.queued, 1);
        audio.ended();
        await tester.pump(const Duration(milliseconds: 100));
        await _finish(tester, state);
      },
    );
  }

  for (final change in [
    'source-generation',
    'session',
    'thread',
    'epoch',
    'turn',
  ]) {
    testWidgets(
      'an obsolete native $change owner cannot lower generation or play',
      (tester) async {
        final api = _Api();
        final audio = _Audio();
        final state = await _mount(tester, api, audio);
        final clip = await _import(tester, api, audio);
        await _confirm(tester, clip);
        final old = state.converseSession!;
        if (change == 'source-generation') {
          state.applyConverseHealth(health(available: false));
          state.applyConverseHealth(health());
        } else if (change == 'turn') {
          state.turnNonce++;
        } else {
          state.converseSession = SessionInfo(
            sessionId: change == 'session' ? 'new-session' : old.sessionId,
            threadId: change == 'thread' ? 'new-thread' : old.threadId,
            epoch: change == 'epoch' ? old.epoch + 1 : old.epoch,
            channel: 'converse',
          );
        }
        state.touch();
        await tester.pump(const Duration(milliseconds: 100));
        final currentEpoch = state.speechEpoch;
        api.entries.addAll([clip.source, clip.reply]);
        await _poll(tester, state);
        expect(state.speechEpoch, currentEpoch);
        expect(audio.queued, 0);
        expect(api.speechRequests, isEmpty);
        expect(api.delivery, isEmpty);
        expect(tester.takeException(), isNull);
        await _finish(tester, state);
      },
    );
  }

  testWidgets(
    'an old client receipt cannot synchronize, but the reconnected client can',
    (tester) async {
      final oldApi = _Api();
      final audio = _Audio();
      final state = await _mount(tester, oldApi, audio);
      final oldClip = await _import(tester, oldApi, audio);
      final selected = state.converseSession!;
      final profile = state.connection!;
      state.disconnect();
      final freshApi = _Api();
      state.connect(profile, injected: freshApi);
      state.converseSession = selected;
      state.converseProvider = localProvider();
      state.agentProvider = localProvider();
      state.speechEpoch = 60;
      state.touch();
      await tester.pump(const Duration(milliseconds: 100));
      final currentEpoch = state.speechEpoch;
      oldApi.entries.addAll([oldClip.source, oldClip.reply]);
      oldClip.admitted.complete(
        NativeAudioClipAdmission(oldClip.source, false),
      );
      await tester.pump(const Duration(milliseconds: 100));
      expect(state.speechEpoch, currentEpoch);
      expect(audio.queued, 0);
      expect(oldApi.speechRequests, isEmpty);
      final freshClip = await _import(tester, freshApi, audio);
      await _confirm(tester, freshClip);
      freshApi.entries.addAll([freshClip.source, freshClip.reply]);
      await _poll(tester, state);
      expect(state.speechEpoch, freshApi.serverGeneration);
      expect(audio.queued, 1);
      expect(freshApi.speechRequests, [
        (freshClip.reply.eventId, freshApi.serverGeneration),
      ]);
      expect(audio.armed, 0);
      audio.ended();
      await tester.pump(const Duration(milliseconds: 100));
      await _finish(tester, state);
    },
  );

  testWidgets(
    'duplicate old native replies never interrupt a newer clip playback',
    (tester) async {
      final api = _Api();
      final audio = _Audio();
      final state = await _mount(tester, api, audio);
      final first = await _import(tester, api, audio);
      await _confirm(tester, first);
      api.entries.addAll([first.source, first.reply]);
      await _poll(tester, state);
      expect(audio.queued, 1);
      audio.ended();
      await tester.pump(const Duration(milliseconds: 100));
      api.serverGeneration = 3;
      final second = await _import(tester, api, audio);
      await _confirm(tester, second);
      api.entries.addAll([second.source, second.reply]);
      await _poll(tester, state);
      expect(audio.queued, 2);
      expect(state.speechEpoch, 3);
      expect(audio.endings.last.isCompleted, false);
      final stops = audio.stops, nonce = state.turnNonce;
      api.replayPage = true;
      await _poll(tester, state);
      expect(state.speechEpoch, 3);
      expect(state.turnNonce, nonce);
      expect(audio.stops, stops);
      expect(audio.queued, 2);
      expect(audio.endings.last.isCompleted, false);
      expect(api.speechRequests, [
        (first.reply.eventId, 2),
        (second.reply.eventId, 3),
      ]);
      audio.ended();
      await tester.pump(const Duration(milliseconds: 100));
      expect(
        api.delivery
            .where((row) => row == (second.reply.eventId, 'played'))
            .length,
        1,
      );
      await _finish(tester, state);
    },
  );

  for (final field in ['runtime_id', 'generation_epoch', 'chunk_id']) {
    testWidgets(
      'a conflicting native $field tuple cannot replace pending original playback',
      (tester) async {
        final api = _Api();
        final audio = _Audio();
        final state = await _mount(tester, api, audio);
        final clip = await _import(tester, api, audio);
        await _confirm(tester, clip);
        api.entries.addAll([clip.source, clip.reply]);
        await _poll(tester, state);
        expect(audio.queued, 1);
        expect(audio.endings.last.isCompleted, false);
        expect(api.delivery, contains((clip.reply.eventId, 'playing')));
        expect(api.delivery.where((row) => row.$2 == 'played'), isEmpty);
        final generation = state.speechEpoch;
        final nonce = state.turnNonce;
        final stops = audio.stops;
        final conflicting = _entry(
          clip.reply,
          eventId: 'public-conflicting-native-$field',
          sequence: clip.reply.sequence + 1,
          payload: {
            field: field == 'generation_epoch'
                ? api.serverGeneration + 1
                : 'public-conflicting-$field',
          },
        );
        api.entries.add(conflicting);
        await _poll(tester, state);
        expect(state.speechEpoch, generation);
        expect(state.turnNonce, nonce);
        expect(audio.stops, stops);
        expect(audio.queued, 1);
        expect(audio.endings.last.isCompleted, false);
        expect(api.speechRequests, [
          (clip.reply.eventId, api.serverGeneration),
        ]);
        expect(api.delivery.where((row) => row.$2 == 'played'), isEmpty);
        audio.ended();
        await tester.pump(const Duration(milliseconds: 100));
        expect(
          api.delivery
              .where((row) => row == (clip.reply.eventId, 'played'))
              .length,
          1,
        );
        expect(
          api.delivery.where((row) => row == (conflicting.eventId, 'played')),
          isEmpty,
        );
        expect(tester.takeException(), isNull);
        await _finish(tester, state);
      },
    );
  }

  testWidgets(
    'legacy rolling replies retain higher-only generation synchronization',
    (tester) async {
      final api = WindowApi();
      final audio = WindowAudio()..speaking = false;
      final state = await arm(tester, api, audio);
      for (var n = 0; n < 11; n++) {
        await tester.pump(const Duration(milliseconds: 100));
      }
      expect(api.received, isNotEmpty);
      state.speechEpoch = 9;
      final selected = state.converseSession!;
      final reply = JournalEntry(
        eventId: 'public-legacy-low-reply',
        sequence: 1,
        sessionId: selected.sessionId,
        threadId: selected.threadId,
        epoch: selected.epoch,
        kind: 'conversation.converse',
        origin: 'converse',
        payload: {
          'text': 'Public legacy reply.',
          'chunk_id': 'public-legacy-low-chunk',
          'generation_epoch': 2,
          'window_source_id': 'public-legacy-source',
          'window_utterance_id': api.received.first['utterance_id'],
          'window_behavior': 'RESPOND_INTERRUPT_USER',
        },
      );
      api.entries.add(reply);
      await _poll(tester, state);
      expect(state.speechEpoch, 9);
      expect(audio.queued, 0);
      api.entries.add(
        _entry(
          reply,
          eventId: 'public-legacy-higher-reply',
          sequence: 2,
          payload: {
            'generation_epoch': 10,
            'chunk_id': 'public-legacy-higher-chunk',
          },
        ),
      );
      await _poll(tester, state);
      expect(state.speechEpoch, 10);
      expect(audio.queued, 1);
      api.first.complete({});
      await tester.pump();
      await _finish(tester, state);
    },
  );
}
