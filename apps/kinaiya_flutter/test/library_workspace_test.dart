import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/library_models.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/library_workspace.dart';

const projectId = '12345678-1234-1234-1234-123456789abc';
Map<String, Object?> project() => {
  'schema_version': '1.0',
  'project_id': projectId,
  'name': 'Public spreadsheets',
  'created_at': '2026-10-31T12:00:00Z',
  'revision': 1,
  'session_ids': <String>[],
};

void main() {
  test('library parser requires scoped, ordered and dated native metadata', () {
    final row = {
      'event_id': 'e',
      'sequence': 4,
      'name': 'report.csv',
      'relative_path': 'report.csv',
      'file_type': 'CSV',
      'task_id': 'task',
      'session_id': 'session',
      'created_at': '2026-10-31T12:00:00Z',
      'sha256': 'a' * 64,
    };
    final raw = {
      'schema_version': '1.0',
      'kind': 'documents',
      'items': [row],
      'next_cursor': 5,
    };
    expect(
      GeneratedOutputPage.fromJson(
        raw,
        kind: 'documents',
        after: 0,
      ).items.single.type,
      'CSV',
    );
    for (final invalid in [
      {...raw, 'kind': 'media'},
      {...raw, 'next_cursor': 3},
      {
        ...raw,
        'items': [row, row],
      },
      {
        ...raw,
        'items': [
          {...row, 'created_at': 'yesterday'},
        ],
      },
    ]) {
      expect(
        () =>
            GeneratedOutputPage.fromJson(invalid, kind: 'documents', after: 0),
        throwsFormatException,
      );
    }
    expect(AgentProject.fromJson(project()).sessionIds, isEmpty);
  });

  testWidgets('Projects creates real metadata only on explicit submission', (
    tester,
  ) async {
    final requests = <http.Request>[], rows = <Map<String, Object?>>[];
    Future<http.Response> respond(http.Request request) async {
      requests.add(request);
      Object? body;
      if (request.method == 'GET' && request.url.path == '/v1/agent/projects') {
        body = {'schema_version': '1.0', 'items': rows, 'next_cursor': null};
      } else if (request.method == 'POST' &&
          request.url.path == '/v1/agent/projects') {
        final input = jsonDecode(request.body) as Map;
        expect(input['name'], 'Public spreadsheets');
        rows.add(project());
        body = {
          'schema_version': '1.0',
          'idempotency_key': input['idempotency_key'],
          'operation': 'create',
          'project': rows.single,
        };
      } else {
        return http.Response('Unexpected request', 404);
      }
      return http.Response(
        jsonEncode(body),
        200,
        headers: {'content-type': 'application/json'},
      );
    }

    final state = AppState();
    state.client = KinaiyaClient(
      sessionBase: 'http://127.0.0.1:19101',
      agentBase: 'http://127.0.0.1:19101',
      converseBase: 'http://127.0.0.1:19101',
      ownerToken: 'synthetic-owner',
      httpClient: MockClient(respond),
      agentWorkspaceClientFactory: () => MockClient(respond),
    );
    state.connected = state.sessionAuthenticated = true;
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: LibraryWorkspace(
            state: state,
            section: LibrarySection.projects,
            onOpenAgent: () {},
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();
    expect(requests.every((request) => request.method == 'GET'), isTrue);
    await tester.enterText(
      find.widgetWithText(TextField, 'Project name'),
      'Public spreadsheets',
    );
    await tester.pump();
    await tester.tap(find.text('Create project'));
    await tester.pumpAndSettle();
    expect(requests.where((request) => request.method == 'POST'), hasLength(1));
    expect(find.text('Project saved.'), findsOneWidget);
    expect(find.text('Public spreadsheets'), findsOneWidget);
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
  });

  for (final section in LibrarySection.values) {
    testWidgets('narrow library $section is top anchored without connecting', (
      tester,
    ) async {
      tester.view.physicalSize = const Size(320, 600);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final state = AppState();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: LibraryWorkspace(
              state: state,
              section: section,
              onOpenAgent: () {},
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(
        find.text('Connect to your host to read these records.'),
        findsOneWidget,
      );
      expect(state.client, isNull);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
    });
  }
}
