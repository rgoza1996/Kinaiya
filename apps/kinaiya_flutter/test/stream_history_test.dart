import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/app.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';

AppState wired(http.Client transport) {
  final state = AppState();
  state.connect(
    ConnectionProfile.defaults(),
    injected: KinaiyaClient(
      sessionBase: 'http://fixture',
      agentBase: 'http://fixture',
      converseBase: 'http://fixture',
      ownerToken: 'synthetic-token',
      httpClient: transport,
    ),
  );
  state.converseSession = SessionInfo(
    sessionId: 's',
    threadId: 't',
    channel: 'converse',
    epoch: 1,
  );
  return state;
}

void main() {
  testWidgets('empty history seeds once and streamed sentences keep spaces', (
    tester,
  ) async {
    var seeds = 0, turns = 0;
    final state = wired(
      MockClient((request) async {
        if (request.url.path == '/v1/transcript/recent') {
          seeds++;
          return http.Response('[]', 200);
        }
        if (request.url.path == '/v1/converse/turns/stream') {
          turns++;
          final events = [
            {'type': 'accepted', 'generation_epoch': turns},
            for (var i = 0; i < 2; i++)
              {
                'type': 'text',
                'entry': {
                  'schema_version': '1.0',
                  'event_id': 'e$turns-$i',
                  'sequence': turns * 3 + i,
                  'session_id': 's',
                  'thread_id': 't',
                  'epoch': 1,
                  'kind': 'conversation.converse',
                  'origin': 'converse',
                  'payload': {'text': i == 0 ? 'Sure thing.' : 'Here you go.'},
                },
              },
            {'type': 'done'},
          ];
          return http.Response(
            events.map((e) => 'data: ${jsonEncode(e)}\n\n').join(),
            200,
          );
        }
        return http.Response('{"enabled":true}', 200);
      }),
    );
    await tester.pumpWidget(KinaiyaApp(state: state));
    await tester.pump(const Duration(milliseconds: 200));
    for (var i = 0; i < 2; i++) {
      await tester.enterText(find.byType(TextField), 'turn $i');
      await tester.pump();
      await tester.ensureVisible(find.text('Send'));
      await tester.pump(const Duration(milliseconds: 200));
      expect(find.text('Send').hitTestable(), findsOneWidget);
      await tester.tap(find.text('Send'));
      await tester.pump(const Duration(milliseconds: 200));
    }
    expect(seeds, 1);
    expect(state.converseCursor, 7);
    expect(
      state.converseHistory
          .where((m) => m.role == 'kinaiya')
          .map((m) => m.text),
      ['Sure thing. Here you go.', 'Sure thing. Here you go.'],
    );
    expect(state.converseHistory.where((m) => m.role == 'user').length, 2);
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
  });

  testWidgets('an in-flight command cannot be submitted twice', (tester) async {
    var calls = 0;
    final response = Completer<http.Response>();
    final state = wired(
      MockClient((request) async {
        if (request.url.path == '/v1/commands/execute') {
          calls++;
          return response.future;
        }
        if (request.url.path == '/v1/commands') {
          return http.Response(
            '{"commands":[{"name":"status","description":"Status"}]}',
            200,
          );
        }
        return http.Response('[]', 200);
      }),
    );
    state.agentSession = SessionInfo(
      sessionId: 'agent',
      threadId: 'agent-thread',
      channel: 'agent',
      epoch: 1,
    );
    state.setView(AppView.agent);
    await tester.pumpWidget(KinaiyaApp(state: state));
    await tester.pump(const Duration(milliseconds: 200));
    await tester.enterText(find.byType(TextField), '/status');
    await tester.pump();
    await tester.tap(find.text('Send task'));
    await tester.pump();
    await tester.testTextInput.receiveAction(TextInputAction.send);
    await tester.pump();
    expect(calls, 1);
    expect(state.busyAgent, isTrue);
    response.complete(http.Response('{"status":"ok","command":"status"}', 200));
    await tester.pump(const Duration(milliseconds: 200));
    expect(state.busyAgent, isFalse);
    expect(state.agentHistory.single.text, 'Command status: ok');
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
  });
}
