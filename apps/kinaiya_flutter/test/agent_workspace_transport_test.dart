import 'dart:async';
import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/agent_workspace_client.dart';
import 'package:kinaiya_flutter/api/client.dart';

class HeldWorkspaceClient extends http.BaseClient {
  final response = Completer<http.StreamedResponse>();
  int closes = 0;
  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) =>
      response.future;
  @override
  void close() {
    closes++;
  }
}

http.StreamedResponse jsonResponse(String body) => http.StreamedResponse(
  Stream.value(utf8.encode(body)),
  200,
  headers: {'content-type': 'application/json'},
);

class CompletedWorkspaceClient extends http.BaseClient {
  CompletedWorkspaceClient(this.body);
  final String body;
  int closes = 0;
  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) async {
    expect(request.followRedirects, isFalse);
    expect(request.headers['Authorization'], 'Bearer synthetic');
    return jsonResponse(body);
  }

  @override
  void close() {
    closes++;
  }
}

class HeldWorkspaceCancellation extends http.BaseClient {
  final cancellation = Completer<void>();
  late final body = StreamController<List<int>>(
    onCancel: () => cancellation.future,
  );
  int closes = 0;
  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) async =>
      http.StreamedResponse(
        body.stream,
        200,
        headers: {'content-type': 'application/json'},
      );
  @override
  void close() {
    closes++;
  }
}

void main() {
  test(
    'stalled body cancellation retains admission until the actual cancel settles',
    () async {
      final clients = <HeldWorkspaceCancellation>[];
      var recovery = false;
      final reader = AgentWorkspaceReader(
        ownerToken: 'synthetic',
        requestTimeout: const Duration(milliseconds: 10),
        cleanupTimeout: const Duration(milliseconds: 5),
        clientFactory: () {
          if (recovery) return CompletedWorkspaceClient('{}');
          final client = HeldWorkspaceCancellation();
          clients.add(client);
          return client;
        },
      );
      addTearDown(reader.close);
      await Future.wait(
        List.generate(
          3,
          (_) => expectLater(
            reader.get('https://host.invalid', '/read'),
            throwsA(isA<WorkspaceReadFailure>()),
          ),
        ),
      );
      await expectLater(
        reader.get('https://host.invalid', '/read'),
        throwsA(isA<WorkspaceReadFailure>()),
      );
      expect(clients.length, 3);
      expect(clients.every((c) => c.closes == 1), isTrue);
      for (final client in clients) {
        client.cancellation.complete();
      }
      await Future<void>.delayed(const Duration(milliseconds: 5));
      recovery = true;
      expect(
        await reader.get('https://host.invalid', '/read'),
        <String, Object?>{},
      );
    },
  );

  test(
    'stalled header slots survive timeout and recover only after real settlement',
    () async {
      final clients = <HeldWorkspaceClient>[];
      var recovered = false;
      final reader = AgentWorkspaceReader(
        ownerToken: 'synthetic',
        requestTimeout: const Duration(milliseconds: 10),
        cleanupTimeout: const Duration(milliseconds: 5),
        clientFactory: () {
          if (recovered) return CompletedWorkspaceClient('{}');
          final c = HeldWorkspaceClient();
          clients.add(c);
          return c;
        },
      );
      addTearDown(reader.close);
      await Future.wait(
        List.generate(
          3,
          (_) => expectLater(
            reader.get('https://host.invalid', '/read'),
            throwsA(isA<WorkspaceReadFailure>()),
          ),
        ),
      );
      expect(clients.length, 3);
      expect(clients.every((x) => x.closes == 1), isTrue);
      await expectLater(
        reader.get('https://host.invalid', '/read'),
        throwsA(isA<WorkspaceReadFailure>()),
      );
      expect(clients.length, 3);
      for (final c in clients) {
        c.response.complete(jsonResponse('{}'));
      }
      await Future<void>.delayed(const Duration(milliseconds: 5));
      recovered = true;
      expect(
        await reader.get('https://host.invalid', '/read'),
        <String, Object?>{},
      );
    },
  );
  test(
    'declared and streamed oversized bodies are refused before JSON decoding',
    () async {
      var declared = true;
      final reader = AgentWorkspaceReader(
        ownerToken: 'synthetic',
        clientFactory: () => MockClient.streaming((request, body) async {
          expect(request.followRedirects, isFalse);
          if (declared) {
            return http.StreamedResponse(
              Stream.value([123]),
              200,
              contentLength: 1024 * 1024 + 1,
              headers: {'content-type': 'application/json'},
            );
          }
          return http.StreamedResponse(
            Stream.value(List.filled(1024 * 1024 + 1, 32)),
            200,
            headers: {'content-type': 'application/json'},
          );
        }),
      );
      addTearDown(reader.close);
      await expectLater(
        reader.get('https://host.invalid', '/read'),
        throwsA(isA<WorkspaceReadFailure>()),
      );
      declared = false;
      await expectLater(
        reader.get('https://host.invalid', '/read'),
        throwsA(isA<WorkspaceReadFailure>()),
      );
    },
  );
  test(
    'one request closes only its own client; sibling and task transport survive',
    () async {
      final held = HeldWorkspaceClient();
      final completed = CompletedWorkspaceClient('{}');
      var count = 0;
      final reader = AgentWorkspaceReader(
        ownerToken: 'synthetic',
        requestTimeout: const Duration(milliseconds: 30),
        clientFactory: () => count++ == 0 ? held : completed,
      );
      addTearDown(reader.close);
      final pending = expectLater(
        reader.get('https://host.invalid', '/a'),
        throwsA(isA<WorkspaceReadFailure>()),
      );
      expect(
        await reader.get('https://host.invalid', '/b'),
        <String, Object?>{},
      );
      expect(completed.closes, 1);
      expect(held.closes, 0);
      await pending;
      expect(held.closes, 1);
      held.response.complete(jsonResponse('{}'));
      await Future<void>.delayed(Duration.zero);
    },
  );
  test(
    'workspace closure never closes shared task client and identity parsing is strict',
    () async {
      final task = CompletedWorkspaceClient('[]');
      final api = KinaiyaClient(
        sessionBase: 'https://host.invalid',
        agentBase: 'https://host.invalid',
        converseBase: 'https://host.invalid',
        ownerToken: 'synthetic',
        httpClient: task,
        agentWorkspaceClientFactory: () => CompletedWorkspaceClient(
          '{"schema_version":"1.0","session_id":"s","thread_id":"t","channel":"agent","name":"fixture","epoch":1.5}',
        ),
      );
      addTearDown(api.close);
      await expectLater(api.agentWorkspaceSession('s'), throwsFormatException);
      expect(task.closes, 0);
      expect(await api.listTasks(), isEmpty);
      expect(task.closes, 0);
    },
  );
}
