import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/client.dart';

class RedirectProbe extends http.BaseClient {
  final requests = <http.BaseRequest>[];
  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) async {
    requests.add(request);
    return http.StreamedResponse(
      Stream.value(utf8.encode('{}')),
      302,
      headers: {'location': 'https://different-origin.invalid/'},
    );
  }
}

/// Clearly labeled mock transport: no real services, no credentials.
MockClient mockTransport({bool commandsMissing = true}) {
  return MockClient((http.Request req) async {
    final path = req.url.path;
    if (req.method == 'GET' && path == '/v1/sessions') {
      return http.Response(
        jsonEncode([
          {
            'schema_version': '1.0',
            'session_id': 's-conv',
            'thread_id': 't-conv',
            'channel': 'converse',
            'epoch': 1,
            'name': 'Kinaiya',
          },
        ]),
        200,
      );
    }
    if (req.method == 'POST' && path == '/v1/sessions') {
      final body = jsonDecode(req.body) as Map;
      return http.Response(
        jsonEncode({
          'schema_version': '1.0',
          'session_id': 's-new',
          'thread_id': 't-new',
          'channel': body['channel'],
          'epoch': 1,
          'name': body['name'],
        }),
        200,
      );
    }
    if (path == '/v1/converse/turns/stream') {
      const sse =
          'data: {"type":"accepted","source_event_id":"u1","generation_epoch":3}\n\n'
          'data: {"type":"text","entry":{"schema_version":"1.0","event_id":"e9","sequence":4,'
          '"session_id":"s","thread_id":"t","epoch":1,"kind":"conversation.converse",'
          '"payload":{"text":"hi","chunk_id":"c1","generation_epoch":3,"delivery":"generated"},'
          '"idempotency_key":"k","origin":"converse"}}\n\n'
          'data: {"type":"done"}\n\n';
      return http.Response(sse, 200);
    }
    if (path == '/v1/converse/turns') {
      return http.Response(jsonEncode({'events': [], 'text': 'hi'}), 200);
    }
    if (path == '/v1/commands' || path == '/v1/commands/execute') {
      if (commandsMissing) return http.Response('missing', 404);
      if (path == '/v1/commands') {
        return http.Response(
          jsonEncode({
            'schema_version': '1.0',
            'commands': [
              {
                'name': 'status',
                'description': 'Show status',
                'available': true,
                'execution_type': 'runtime_control',
              },
            ],
          }),
          200,
        );
      }
      return http.Response(
        jsonEncode({
          'schema_version': '1.0',
          'status': 'ok',
          'command': 'status',
        }),
        200,
      );
    }
    if (path == '/v1/agent/tasks' && req.method == 'POST') {
      final body = jsonDecode(req.body) as Map;
      return http.Response(
        jsonEncode({
          'schema_version': '1.0',
          'task_id': 'task-1',
          'session_id': body['session_id'],
          'thread_id': body['thread_id'],
          'epoch': 1,
          'objective': body['objective'],
          'objective_revision': 1,
          'state': 'accepted',
          'idempotency_key': body['idempotency_key'],
        }),
        201,
      );
    }
    if (path == '/v1/agent/tasks') {
      return http.Response(jsonEncode([]), 200);
    }
    if (path == '/v1/transcript/recent') {
      return http.Response(jsonEncode([]), 200);
    }
    if (path == '/health') {
      return http.Response(jsonEncode({'service': 'x', 'enabled': true}), 200);
    }
    return http.Response('unexpected $path', 500);
  });
}

KinaiyaClient mockClient({bool commandsMissing = true}) => KinaiyaClient.mock(
  sessionBase: 'http://127.0.0.1:8760',
  agentBase: 'http://127.0.0.1:8761',
  converseBase: 'http://127.0.0.1:8762',
  mockTransport: mockTransport(commandsMissing: commandsMissing),
);

void main() {
  test(
    'credentials never follow redirects on JSON, SSE or multipart requests',
    () async {
      final transport = RedirectProbe();
      final api = KinaiyaClient(
        sessionBase: 'http://fixture',
        agentBase: 'http://fixture',
        converseBase: 'http://fixture',
        ownerToken: 'synthetic-token',
        httpClient: transport,
      );
      await expectLater(api.listSessions(), throwsA(isA<ApiException>()));
      await expectLater(
        api.createSession('agent'),
        throwsA(isA<ApiException>()),
      );
      await expectLater(
        api.transcribe([1, 2], 'synthetic.wav'),
        throwsA(isA<ApiException>()),
      );
      await expectLater(
        api
            .streamTurn(
              sessionId: 's',
              threadId: 't',
              epoch: 1,
              text: 'synthetic',
              idempotencyKey: 'k',
            )
            .toList(),
        throwsA(isA<ApiException>()),
      );
      await expectLater(
        api.speech(eventId: 'e', generationEpoch: 1).toList(),
        throwsA(isA<ApiException>()),
      );
      expect(transport.requests.length, 5);
      expect(transport.requests.every((r) => !r.followRedirects), isTrue);
      expect(transport.requests.every((r) => r.url.host == 'fixture'), isTrue);
      api.close();
    },
  );

  test('mock api is labeled and reuses named sessions', () async {
    final api = mockClient();
    expect(isMockApi, isTrue);
    final sessions = await api.listSessions();
    expect(sessions.single.sessionId, 's-conv');
    final created = await api.createSession('agent', name: 'work');
    expect(created.channel, 'agent');
  });

  test('converse SSE parses accepted, text entry and done', () async {
    final api = mockClient();
    final events = await api
        .streamTurn(
          sessionId: 's',
          threadId: 't',
          epoch: 1,
          text: 'hello /status stays text',
          idempotencyKey: 'k-1',
        )
        .toList();
    expect(events.map((e) => e.type), ['accepted', 'text', 'done']);
    expect(events[1].entry!.text, 'hi');
    expect(events[1].entry!.payload['delivery'], 'generated');
  });

  test('missing command registry returns null honestly', () async {
    expect(await mockClient(commandsMissing: true).fetchCommands(), isNull);
    final present = await mockClient(commandsMissing: false).fetchCommands();
    expect(present!.single.name, 'status');
  });

  test('tasks round-trip with idempotency key', () async {
    final task = await mockClient().createTask(
      sessionId: 's',
      threadId: 't',
      epoch: 1,
      objective: 'write proof',
      idempotencyKey: 'idem-1',
    );
    expect(task.state, 'accepted');
    expect(task.idempotencyKey, 'idem-1');
  });
}
