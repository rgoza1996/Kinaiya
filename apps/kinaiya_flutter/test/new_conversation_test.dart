// Fresh native session navigation: synthetic transport/audio only.
import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';

import 'input_disclosure_test.dart' show ScopeClient, withClient, character;
import 'live_capture_test.dart' show CaptureAudio;

SessionInfo session(String id, {String channel = 'converse'}) => SessionInfo(
  sessionId: id,
  threadId: 'thread-$id',
  epoch: 1,
  channel: channel,
  name: 'Converse',
);

class _Api extends ScopeClient {
  int creates = 0, lists = 0;
  final created = Completer<SessionInfo>();
  Completer<List<SessionInfo>>? listing;
  final durableHistory = <String, List<String>>{
    'old': ['Prior native history'],
  };

  @override
  Future<SessionInfo> createSession(String channel, {String name = ''}) {
    expect(channel, 'converse');
    expect(name, 'Converse');
    creates++;
    return created.future;
  }

  @override
  Future<List<SessionInfo>> listSessions() {
    lists++;
    return listing?.future ?? Future.value([session('old')]);
  }
}

class _Audio extends CaptureAudio {
  int arms = 0;
  @override
  Future<bool> get isPlaying async => false;
  @override
  Future<String> startArmed() async {
    arms++;
    return 'armed';
  }
}

AppState _stateFor(_Api api) => AppState()
  ..client = api
  ..sessionAuthenticated = true
  ..converseSession = session('old')
  ..converseHistory.add(ChatMsg(role: 'user', text: 'Old visible history'))
  ..converseDraft = 'Unsent draft';

void main() {
  test('explicit fresh creation never lists or reuses named history', () async {
    final api = _Api();
    final state = _stateFor(api);
    final agent = session('agent', channel: 'agent');
    state.agentSession = agent;
    final original = state.startNewConverseSession(idle: () => true);
    expect(
      identical(original, state.startNewConverseSession(idle: () => true)),
      isTrue,
    );
    await Future<void>.value();
    expect(api.creates, 1);
    expect(api.lists, 0);
    expect(state.converseSession!.sessionId, 'old');
    expect(state.converseSessionCreating, isTrue);
    api.created.complete(session('fresh'));
    await original;
    expect(state.converseSession!.sessionId, 'fresh');
    expect(state.converseHistory, isEmpty);
    expect(state.converseDraft, 'Unsent draft');
    expect(state.agentSession, same(agent));
    expect(api.durableHistory['old'], ['Prior native history']);
    expect(state.converseSessionCreating, isFalse);
    expect(
      (await state.ensureSession('converse', name: 'Converse')).sessionId,
      'fresh',
    );
    expect(api.lists, 0);
    state.dispose();
  });

  for (final change in ['connection', 'idle', 'session']) {
    test('late $change change cannot install new session', () async {
      final api = _Api();
      final state = _stateFor(api);
      var idle = true;
      final original = state.startNewConverseSession(idle: () => idle);
      final failure = expectLater(original, throwsStateError);
      await Future<void>.value();
      if (change == 'connection') state.client = _Api();
      if (change == 'idle') idle = false;
      if (change == 'session') state.converseSession = session('other');
      api.created.complete(session('fresh'));
      await failure;
      expect(
        state.converseSession!.sessionId,
        change == 'session' ? 'other' : 'old',
      );
      expect(state.converseHistory.single.text, 'Old visible history');
      expect(api.creates, 1);
      state.dispose();
    });
  }

  test(
    'older implicit acquisition cannot replace explicit fresh session',
    () async {
      final api = _Api()..listing = Completer<List<SessionInfo>>();
      final state = _stateFor(api)..converseSession = null;
      final implicit = state.ensureSession('converse', name: 'Converse');
      final failure = expectLater(implicit, throwsStateError);
      final fresh = state.startNewConverseSession(idle: () => true);
      await Future<void>.value();
      api.created.complete(session('fresh'));
      await fresh;
      api.listing!.complete([session('old')]);
      await failure;
      expect(state.converseSession!.sessionId, 'fresh');
      expect(api.creates, 1);
      state.dispose();
    },
  );

  test('busy or non-idle creation dispatches nothing', () {
    final api = _Api();
    final state = _stateFor(api);
    state.busyConverse = true;
    expect(
      () => state.startNewConverseSession(idle: () => true),
      throwsStateError,
    );
    state.busyConverse = false;
    expect(
      () => state.startNewConverseSession(idle: () => false),
      throwsStateError,
    );
    expect(api.creates, 0);
    expect(state.converseSession!.sessionId, 'old');
    state.dispose();
  });

  testWidgets('History creates one fresh native conversation without capture', (
    tester,
  ) async {
    final api = _Api(), audio = _Audio();
    final state = withClient(api, BackendProfile.clean);
    await tester.pumpWidget(character(state, audio));
    await tester.pump(const Duration(milliseconds: 100));
    state.converseHistory.add(
      ChatMsg(role: 'user', text: 'Old visible history'),
    );
    state.touch();
    await tester.pump();
    await tester.tap(find.byKey(const ValueKey('conversation-history')));
    await tester.pump(const Duration(milliseconds: 300));
    expect(find.byType(AlertDialog), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey('new-conversation')));
    await tester.pump();
    expect(api.creates, 1);
    expect(audio.arms, 0);
    expect(state.converseSessionCreating, isTrue);
    api.created.complete(session('fresh'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));
    expect(find.byType(AlertDialog), findsNothing);
    expect(state.converseSession!.sessionId, 'fresh');
    expect(state.converseHistory, isEmpty);
    expect(audio.arms, 0);
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
  });

  testWidgets('History new conversation is disabled during Call', (
    tester,
  ) async {
    final api = _Api(), audio = _Audio();
    final state = withClient(api, BackendProfile.clean);
    await tester.pumpWidget(character(state, audio));
    await tester.pump(const Duration(milliseconds: 100));
    await tester.tap(find.byKey(const ValueKey('call-kinaiya')));
    await tester.pump(const Duration(milliseconds: 100));
    state.converseHistory.add(ChatMsg(role: 'user', text: 'Existing history'));
    state.touch();
    await tester.pump();
    await tester.tap(find.byKey(const ValueKey('conversation-history')));
    await tester.pump(const Duration(milliseconds: 300));
    expect(find.byType(AlertDialog), findsOneWidget);
    final button = tester.widget<TextButton>(
      find.byKey(const ValueKey('new-conversation')),
    );
    expect(button.onPressed, isNull);
    expect(api.creates, 0);
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
  });
}
