// Independent native-status oracles from the frozen owner/UI contract.
// Public synthetic fixtures only; no candidate source or real service access.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/audio/speech_audio.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/state/native_audio_clip.dart';

import 'input_disclosure_test.dart' show character, withClient;
import 'native_audio_clip_test.dart'
    show
        NativeClipApi,
        NativeClipAudio,
        health,
        output,
        requestId,
        session,
        source,
        utterance,
        wav;

const _marker = 'UNTRUSTED-SYNTHETIC-STATUS-DETAIL-451';

NativeAudioClipOwner _owner({
  ContentScope scope = ContentScope.publicSynthetic,
}) => NativeAudioClipOwner(
  client: Object(),
  connectionGeneration: 2,
  sourceGeneration: 7,
  session: session(),
  utteranceId: utterance,
  requestId: requestId,
  turnNonce: 9,
  contentScope: scope,
);

JournalEntry _copy(
  JournalEntry entry, {
  String? eventId,
  int? sequence,
  String? origin,
  String? sessionId,
  String? threadId,
  int? epoch,
  List<String>? causes,
  Map<String, Object?>? payload,
}) => JournalEntry(
  eventId: eventId ?? entry.eventId,
  sequence: sequence ?? entry.sequence,
  sessionId: sessionId ?? entry.sessionId,
  threadId: threadId ?? entry.threadId,
  epoch: epoch ?? entry.epoch,
  kind: entry.kind,
  origin: origin ?? entry.origin,
  causes: causes ?? entry.causes,
  payload: payload ?? Map<String, Object?>.of(entry.payload),
);

JournalEntry _routing(
  JournalEntry original, {
  String phase = 'classifying',
  String reason = 'public-fixture',
  int? sequence,
  String? sourceId,
}) => JournalEntry(
  eventId: 'public-routing-${sequence ?? original.sequence + 1}',
  sequence: sequence ?? original.sequence + 1,
  sessionId: original.sessionId,
  threadId: original.threadId,
  epoch: original.epoch,
  kind: 'routing.window_status',
  origin: 'decisions',
  causes: [sourceId ?? original.eventId],
  payload: {
    'source_event_id': sourceId ?? original.eventId,
    'source_sequence': original.sequence,
    'source_revision': original.payload['revision'],
    'phase': phase,
    'reason': reason,
  },
);

JournalEntry _decision(
  JournalEntry original, {
  String conversation = 'IGNORE',
  int? sequence,
}) => JournalEntry(
  eventId: 'public-decision-${sequence ?? original.sequence + 1}',
  sequence: sequence ?? original.sequence + 1,
  sessionId: original.sessionId,
  threadId: original.threadId,
  epoch: original.epoch,
  kind: 'routing.window_decided',
  origin: 'decisions',
  causes: [original.eventId],
  payload: {
    'decision_id': 'public-native-decision',
    'source_event_id': original.eventId,
    'source_sequence': original.sequence,
    'source_revision': original.payload['revision'],
    'conversation': conversation,
    'agent_route': 'IGNORE_FOR_IMMEDIATE_ROUTING',
    'reason_code': _marker,
  },
);

JournalEntry _converse(
  JournalEntry original, {
  String phase = 'preparing',
  int? sequence,
  Map<String, Object?> fields = const {},
}) => JournalEntry(
  eventId: 'public-converse-state-${sequence ?? original.sequence + 1}',
  sequence: sequence ?? original.sequence + 1,
  sessionId: original.sessionId,
  threadId: original.threadId,
  epoch: original.epoch,
  kind: 'converse.window_state',
  origin: 'converse',
  causes: [original.eventId],
  payload: {
    'source_event_id': original.eventId,
    'utterance_id': original.payload['utterance_id'],
    'runtime_id': 'public-runtime',
    'phase': phase,
    ...fields,
  },
);

class _Api extends NativeClipApi {
  _Api({super.receiptChanges});
  final entries = <JournalEntry>[];
  int speechCalls = 0, admissions = 0;

  @override
  Future<NativeAudioClipAdmission> submitNativeAudioClip({
    required SessionInfo session,
    required String utteranceId,
    required String requestId,
    required List<int> wav,
    required ContentScope contentScope,
  }) {
    admissions++;
    return super.submitNativeAudioClip(
      session: session,
      utteranceId: utteranceId,
      requestId: requestId,
      wav: wav,
      contentScope: contentScope,
    );
  }

  @override
  Future<List<JournalEntry>> transcript({
    required String sessionId,
    int after = 0,
    int limit = 200,
  }) async => entries.where((entry) => entry.sequence > after).toList();

  @override
  Stream<Map<String, Object?>> speech({
    required String eventId,
    required int generationEpoch,
  }) {
    speechCalls++;
    return super.speech(eventId: eventId, generationEpoch: generationEpoch);
  }
}

Future<AppState> _mount(
  WidgetTester tester,
  _Api api,
  NativeClipAudio audio,
) async {
  final state = withClient(api, BackendProfile.clean);
  await tester.pumpWidget(character(state, audio));
  await tester.pump(const Duration(milliseconds: 100));
  await tester.tap(find.byTooltip(nativeAudioImportHint));
  audio.chosen.complete(
    RecordedAudio('audio/wav', wav(), filename: 'public-status.wav'),
  );
  await tester.pump(const Duration(milliseconds: 100));
  expect(api.original, isNotNull);
  expect(api.admissions, 1);
  return state;
}

Future<void> _poll(WidgetTester tester, AppState state) async {
  await state.refreshNotices();
  await tester.pump(const Duration(milliseconds: 100));
}

Future<void> _confirm(WidgetTester tester, _Api api) async {
  api.admission.complete(NativeAudioClipAdmission(api.original!, false));
  await tester.pump(const Duration(milliseconds: 100));
}

Future<void> _finish(WidgetTester tester, AppState state) async {
  await tester.pumpWidget(const SizedBox.shrink());
  state.dispose();
}

List<String> _messages(AppState state) =>
    state.converseHistory.map((row) => row.text).toList();

void main() {
  for (final scope in [ContentScope.localOnly, ContentScope.publicSynthetic]) {
    test(
      'source ACK captures and enforces the explicit ${scope.wireValue} scope',
      () {
        final owner = _owner(scope: scope);
        final original = _copy(
          source(),
          payload: {...source().payload, 'content_scope': scope.wireValue},
        );
        owner.confirm(original);
        expect(owner.admitted, true);
        expect(owner.acceptsStatus(_routing(original)), true);
        final other = scope == ContentScope.localOnly
            ? 'public_synthetic'
            : 'local_only';
        final refused = _owner(scope: scope);
        expect(
          () => refused.confirm(
            _copy(
              original,
              payload: {...original.payload, 'content_scope': other},
            ),
          ),
          throwsFormatException,
        );
        expect(refused.admitted, false);
      },
    );
  }

  test(
    'pre-ACK statuses are quarantined without notice or terminal authority',
    () {
      final owner = _owner();
      final original = source();
      final ignore = _decision(original);
      expect(owner.statusCandidate(ignore), true);
      expect(owner.acceptsStatus(ignore), false);
      owner.deferStatus(ignore);
      expect(owner.observeStatus(ignore), isNull);
      expect(owner.admitted, false);
      owner.confirm(original);
      final pending = owner.takePending();
      expect(pending.length, 1);
      expect(pending.single.eventId, ignore.eventId);
      final notice = owner.observeStatus(pending.single);
      expect(notice, NativeAudioNotice.noReply);
      expect(notice!.terminal, true);
      expect(notice.message, isNot(contains(_marker)));
    },
  );

  for (final field in ['event-id', 'sequence', 'revision']) {
    test(
      'a conflicting $field ACK cannot replace the confirmed source lineage',
      () {
        final owner = _owner()..confirm(source());
        final conflicting = _copy(
          source(),
          eventId: field == 'event-id' ? 'foreign-source' : null,
          sequence: field == 'sequence' ? source().sequence + 1 : null,
          payload: {
            ...source().payload,
            if (field == 'revision') 'revision': 2,
          },
        );
        expect(() => owner.confirm(conflicting), throwsFormatException);
        expect(owner.acceptsStatus(_routing(source())), true);
        expect(owner.bindReply(output()), true);
      },
    );
  }

  test('status quarantine coalesces highest sequence and captures immutable scalar data', () {
    final owner = _owner();
    final original = source();
    owner.deferStatus(_routing(original, sequence: 7, phase: 'busy'));
    final latest = _routing(original, sequence: 9, phase: 'retrying');
    owner.deferStatus(latest);
    owner.deferStatus(_routing(original, sequence: 8, phase: 'classifying'));
    latest.payload['phase'] = _marker;
    owner.confirm(original);
    final pending = owner.takePending();
    expect(pending.length, 1);
    expect(pending.single.sequence, 9);
    expect(pending.single.payload['phase'], 'retrying');
    expect(
      () => pending.single.payload['phase'] = _marker,
      throwsUnsupportedError,
    );
    expect(owner.takePending(), isEmpty);
  });

  test('four-group quarantine overflow stays bounded and retains the unadmitted owner', () {
    final owner = _owner();
    final original = source();
    for (var index = 0; index < 5; index++) {
      expect(
        () => owner.deferStatus(
          _routing(
            original,
            sourceId: 'public-source-$index',
            sequence: 6 + index,
          ),
        ),
        returnsNormally,
      );
    }
    expect(owner.statusOverflowed, true);
    expect(owner.admitted, false);
    owner.confirm(original);
    expect(owner.admitted, true);
    expect(owner.takePending().length, lessThanOrEqualTo(4));
    expect(
      owner.bindReply(output()),
      true,
      reason: 'Overflow does not discard original playback ownership',
    );
  });

  for (final sample in [
    ('classifying', 'public', NativeAudioNotice.classifying),
    ('busy', 'classification_capacity', NativeAudioNotice.busy),
    ('error', 'provider_unavailable', NativeAudioNotice.routingUnknown),
    ('error', 'provider_output_limit', NativeAudioNotice.routingUnknown),
  ]) {
    test(
      'routing ${sample.$1}/${sample.$2} stays conservative and uses a constant notice',
      () {
        final owner = _owner()..confirm(source());
        final notice = owner.observeStatus(
          _routing(source(), phase: sample.$1, reason: sample.$2),
        );
        expect(notice, sample.$3);
        expect(notice!.terminal, false);
        expect(notice.message, isNot(contains(_marker)));
        expect(owner.admitted, true);
        expect(owner.bindReply(output()), true);
      },
    );
  }

  test(
    'bare provider unavailable has the fixed unconfirmed-routing message',
    () {
      final owner = _owner()..confirm(source());
      final notice = owner.observeStatus(
        _routing(source(), phase: 'error', reason: 'provider_unavailable'),
      );
      expect(
        notice!.message,
        'Decisions could not confirm a route. Check Decisions for details.',
      );
      expect(notice.terminal, false);
    },
  );

  test(
    'WAIT is pending and a later exact IGNORE alone ends the local reply wait',
    () {
      final owner = _owner()..confirm(source());
      final waiting = owner.observeStatus(
        _decision(source(), conversation: 'WAIT_FOR_NEXT_WINDOW', sequence: 6),
      );
      expect(waiting, NativeAudioNotice.waiting);
      expect(waiting!.terminal, false);
      final ignored = owner.observeStatus(_decision(source(), sequence: 7));
      expect(ignored, NativeAudioNotice.noReply);
      expect(ignored!.terminal, true);
      expect(ignored.message, isNot(contains(_marker)));
    },
  );

  for (final fields in [
    {'reason': _marker, 'error_source': 'generation'},
    {'reason': _marker, 'error_source': 'provider_cleanup'},
    {
      'reason': _marker,
      'error_source': 'journal',
      'append_unconfirmed': true,
      'delivery': 'delivery_unknown',
    },
  ]) {
    test(
      'Converse error with ${fields['error_source']} never proves terminal failure',
      () {
        final owner = _owner()..confirm(source());
        final entry = _converse(source(), phase: 'error', fields: fields);
        expect(entry.payload.containsKey('source_sequence'), false);
        expect(entry.payload.containsKey('source_revision'), false);
        expect(
          owner.acceptsStatus(entry),
          true,
          reason:
              'Actual Converse producer omits sequence/revision payload fields',
        );
        final notice = owner.observeStatus(entry);
        expect(notice, NativeAudioNotice.unconfirmed);
        expect(notice!.terminal, false);
        expect(notice.message, isNot(contains(_marker)));
        expect(owner.bindReply(output()), true);
      },
    );
  }

  test('duplicate/out-of-order notices and any status after reply binding have no effects', () {
    final owner = _owner()..confirm(source());
    final busy = _routing(source(), phase: 'busy', sequence: 9);
    expect(owner.observeStatus(busy), NativeAudioNotice.busy);
    expect(owner.observeStatus(busy), isNull);
    expect(
      owner.observeStatus(
        _routing(source(), phase: 'classifying', sequence: 8),
      ),
      isNull,
    );
    expect(
      owner.observeStatus(_routing(source(), phase: 'busy', sequence: 10)),
      isNull,
    );
    expect(owner.bindReply(output()), true);
    expect(owner.hasReply, true);
    expect(owner.observeStatus(_decision(source(), sequence: 11)), isNull);
    expect(owner.ownsReply(output()), true);
  });

  for (final change in [
    'origin',
    'cause-missing',
    'cause-extra',
    'source',
    'sequence',
    'revision',
    'sequence-bool',
    'revision-missing',
    'event-sequence',
    'session',
    'thread',
    'epoch',
  ]) {
    test(
      'foreign routing lineage $change cannot produce even a terminal notice',
      () {
        final owner = _owner()..confirm(source());
        final original = _decision(source());
        final payload = Map<String, Object?>.of(original.payload);
        if (change == 'source') payload['source_event_id'] = 'foreign-source';
        if (change == 'sequence') {
          payload['source_sequence'] = source().sequence + 1;
        }
        if (change == 'revision') payload['source_revision'] = 2;
        if (change == 'sequence-bool') payload['source_sequence'] = true;
        if (change == 'revision-missing') payload.remove('source_revision');
        final foreign = _copy(
          original,
          origin: change == 'origin' ? 'agent-supervisor' : null,
          causes: change == 'cause-missing'
              ? []
              : change == 'cause-extra'
              ? [source().eventId, 'foreign']
              : null,
          sequence: change == 'event-sequence' ? source().sequence : null,
          sessionId: change == 'session' ? 'foreign-session' : null,
          threadId: change == 'thread' ? 'foreign-thread' : null,
          epoch: change == 'epoch' ? session().epoch + 1 : null,
          payload: payload,
        );
        expect(owner.acceptsStatus(foreign), false);
        expect(owner.observeStatus(foreign), isNull);
        expect(owner.bindReply(output()), true);
      },
    );
  }

  for (final change in ['origin', 'cause', 'source', 'utterance', 'runtime']) {
    test('foreign Converse status $change is not authoritative', () {
      final owner = _owner()..confirm(source());
      final original = _converse(
        source(),
        phase: 'error',
        fields: {'reason': _marker},
      );
      final payload = Map<String, Object?>.of(original.payload);
      if (change == 'source') payload['source_event_id'] = 'foreign-source';
      if (change == 'utterance') payload['utterance_id'] = 'foreign-utterance';
      if (change == 'runtime') payload.remove('runtime_id');
      final foreign = _copy(
        original,
        origin: change == 'origin' ? 'decisions' : null,
        causes: change == 'cause' ? ['foreign-source'] : null,
        payload: payload,
      );
      expect(owner.acceptsStatus(foreign), false);
      expect(owner.observeStatus(foreign), isNull);
    });
  }

  testWidgets(
    'pre-ACK IGNORE has no effect until the exact original admission confirms',
    (tester) async {
      final api = _Api();
      final audio = NativeClipAudio();
      final state = await _mount(tester, api, audio);
      api.entries.add(_decision(api.original!));
      await _poll(tester, state);
      expect(
        _messages(state),
        isNot(contains(NativeAudioNotice.noReply.message)),
      );
      expect(find.byTooltip('Stop speech'), findsOneWidget);
      expect(audio.queued, 0);
      await _confirm(tester, api);
      expect(
        _messages(state)
            .where((text) => text == NativeAudioNotice.noReply.message)
            .length,
        1,
      );
      expect(find.byTooltip('Stop speech'), findsNothing);
      expect(audio.queued, 0);
      expect(api.speechCalls, 0);
      expect(_messages(state).join('\n'), isNot(contains(_marker)));
      await _finish(tester, state);
    },
  );

  testWidgets(
    'classifier failure retains pending native ownership for a later real reply',
    (tester) async {
      final api = _Api();
      final audio = NativeClipAudio();
      final state = await _mount(tester, api, audio);
      await _confirm(tester, api);
      api.entries.add(
        _routing(api.original!, phase: 'error', reason: 'provider_unavailable'),
      );
      await _poll(tester, state);
      expect(
        _messages(state),
        contains(NativeAudioNotice.routingUnknown.message),
      );
      expect(find.byTooltip('Stop speech'), findsOneWidget);
      expect(audio.queued, 0);
      api.entries.add(api.result!);
      await _poll(tester, state);
      expect(audio.queued, 1);
      expect(audio.armed, 0);
      expect(api.speechCalls, 1);
      expect(api.delivery, isNot(contains('played')));
      audio.playback.complete();
      await tester.pump(const Duration(milliseconds: 100));
      expect(api.delivery, contains('played'));
      expect(api.admissions, 1);
      await _finish(tester, state);
    },
  );

  testWidgets(
    'a pre-ACK reply wins over an earlier quarantined terminal status',
    (tester) async {
      final api = _Api();
      final audio = NativeClipAudio();
      final state = await _mount(tester, api, audio);
      api.entries.addAll([_decision(api.original!), api.result!]);
      await _poll(tester, state);
      expect(audio.queued, 0);
      expect(
        _messages(state),
        isNot(contains(NativeAudioNotice.noReply.message)),
      );
      await _confirm(tester, api);
      expect(audio.queued, 1);
      expect(api.speechCalls, 1);
      expect(
        _messages(state),
        isNot(contains(NativeAudioNotice.noReply.message)),
      );
      expect(api.delivery, isNot(contains('played')));
      audio.playback.complete();
      await tester.pump(const Duration(milliseconds: 100));
      expect(api.delivery, contains('played'));
      await _finish(tester, state);
    },
  );

  testWidgets(
    'an invalid ACK retains unadmitted ownership and cannot activate quarantined status',
    (tester) async {
      final api = _Api(receiptChanges: {'audio_sha256': '0' * 64});
      final audio = NativeClipAudio();
      final state = await _mount(tester, api, audio);
      api.entries.add(_decision(api.original!));
      await _poll(tester, state);
      await _confirm(tester, api);
      expect(
        _messages(state),
        isNot(contains(NativeAudioNotice.noReply.message)),
      );
      expect(find.byTooltip('Stop speech'), findsOneWidget);
      expect(api.admissions, 1);
      expect(audio.queued, 0);
      expect(api.speechCalls, 0);
      expect(api.transcriptionScopes, isEmpty);
      await tester.tap(find.byTooltip('Stop speech'));
      await tester.pump(const Duration(milliseconds: 100));
      expect(find.byTooltip('Stop speech'), findsNothing);
      expect(
        api.admissions,
        1,
        reason: 'Owner Stop never retries the original clip',
      );
      await _finish(tester, state);
    },
  );

  for (final change in ['source-generation', 'turn']) {
    testWidgets('a stale $change owner cannot surface a terminal status', (
      tester,
    ) async {
      final api = _Api();
      final audio = NativeClipAudio();
      final state = await _mount(tester, api, audio);
      await _confirm(tester, api);
      if (change == 'turn') {
        state.turnNonce++;
      } else {
        state.applyConverseHealth(health(available: false));
        state.applyConverseHealth(health());
      }
      state.touch();
      await tester.pump();
      api.entries.add(_decision(api.original!));
      await _poll(tester, state);
      expect(
        _messages(state),
        isNot(contains(NativeAudioNotice.noReply.message)),
      );
      expect(audio.queued, 0);
      expect(api.speechCalls, 0);
      await _finish(tester, state);
    });
  }
}
