/// #151: synthetic HTTP and actual setup admission; never uses a live host.
library;

import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/memory_models.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/setup_dialog.dart';
import 'agent_workspace_state_test.dart' show harnessJson;
import 'decisions_inspection_test.dart' as decisions;
import 'memory_models_test.dart' as memory;
import 'client_test.dart' show mockClient;

const owner = 'synthetic-owner';
const roots = [
  'http://127.0.0.1:8760',
  'https://agent.example:9443',
  'http://[::1]:8762',
];

// Optional service fields can be inserted before the credential field.
Finder get ownerTokenField => find.byWidgetPredicate(
  (widget) =>
      widget is TextField &&
      widget.decoration?.labelText == 'Owner access token',
);

ConnectionProfile profile(List<String> urls) => ConnectionProfile(
  sessionUrl: urls[0],
  agentUrl: urls[1],
  converseUrl: urls[2],
  ownerToken: owner,
  backendProfile: BackendProfile.privateCloudAuthorized,
);

http.Response json(Object? value) => http.Response(
  jsonEncode(value),
  200,
  headers: {'content-type': 'application/json'},
);

void main() {
  test(
    'accepted roots and safe proxy prefixes have one canonical identity',
    () {
      for (final prefix in ['', '/proxy/session-v1']) {
        final plain = profile(roots.map((r) => '$r$prefix').toList());
        final padded = profile(roots.map((r) => ' \t$r$prefix/\n').toList());
        expect(padded.validate(), isNull);
        expect(
          [padded.sessionUrl, padded.agentUrl, padded.converseUrl],
          [plain.sessionUrl, plain.agentUrl, plain.converseUrl],
        );
        expect(padded.ownerToken, owner);
        expect(padded.backendProfile, BackendProfile.privateCloudAuthorized);
      }
    },
  );

  test(
    'validation rejects unsafe paths without silently resolving another root',
    () {
      for (final suffix in [
        '//',
        '/a//b',
        '/a/../b',
        '/./',
        '/%2f',
        '/%2e%2e',
        '/a\\b',
        '/a?x=1',
        '/a#fragment',
      ]) {
        expect(
          ConnectionProfile.validateServiceUrl('https://host.example$suffix'),
          isNotNull,
          reason: suffix,
        );
      }
      for (final invalid in [
        'https://user:secret@host.example',
        'https://@host.example',
        'https:///',
        'http://remote.example',
        'ftp://host.example',
        'https://host.example?',
        'https://host.example#',
        'https://host.example:0/',
        'http://127.0.0.1:0/',
      ]) {
        expect(
          ConnectionProfile.validateServiceUrl(invalid),
          isNotNull,
          reason: invalid,
        );
      }
      for (final valid in [
        'https://remote.example/proxy/v1/',
        'http://localhost:8760/',
        'http://127.0.0.1:8760/proxy',
        'http://[::1]:8760/',
      ]) {
        expect(
          ConnectionProfile.validateServiceUrl(valid),
          isNull,
          reason: valid,
        );
      }
    },
  );

  test(
    'raw client normalizes all three roots for GET POST multipart and SSE',
    () async {
      final requests = <http.Request>[];
      for (final suffix in ['', '/']) {
        final api = KinaiyaClient(
          sessionBase: '${roots[0]}/session$suffix',
          agentBase: '${roots[1]}/agent$suffix',
          converseBase: '${roots[2]}/converse$suffix',
          ownerToken: owner,
          httpClient: MockClient((r) async {
            requests.add(r);
            if (r.url.path == '/session/v1/sessions' ||
                r.url.path == '/agent/v1/agent/tasks') {
              return json([]);
            }
            if (r.url.path == '/converse/v1/converse/turns/stream') {
              return http.Response('data: {"type":"done"}\n\n', 200);
            }
            if (r.url.path == '/converse/v1/converse/enabled' ||
                r.url.path == '/converse/v1/converse/transcribe') {
              return json({});
            }
            return http.Response('synthetic unexpected path', 404);
          }),
        );
        await api.listSessions();
        await api.listTasks();
        await api.setConverseEnabled(false);
        await api.transcribe([1, 2], 'synthetic.wav');
        await api
            .streamTurn(
              sessionId: 's',
              threadId: 't',
              epoch: 1,
              text: 'synthetic',
              idempotencyKey: 'same-key',
            )
            .toList();
        api.close();
      }
      expect(
        requests.take(5).map((r) => r.url.toString()),
        requests.skip(5).map((r) => r.url.toString()),
      );
      for (final r in requests) {
        expect(r.url.userInfo, isEmpty);
        expect(r.url.hasFragment, isFalse);
        expect(r.headers['Authorization'], 'Bearer $owner');
        expect(r.followRedirects, isFalse);
        expect(r.url.toString(), isNot(contains(owner)));
      }
      expect(jsonDecode(requests[2].body), {'enabled': false});
      expect(
        jsonDecode(requests[4].body),
        containsPair('idempotency_key', 'same-key'),
      );
    },
  );

  test('malformed raw roots refuse before any request or secret exposure', () {
    var sends = 0;
    for (final bad in [
      'https://user:secret@host.example',
      'https://@host.example',
      'https://host.example?secret=value',
      'https://host.example#secret',
      'https://host.example/a/../b',
      'https://host.example//',
      'https://host.example/%252f',
      'https://host.example/a%2fb',
    ]) {
      for (var i = 0; i < 3; i++) {
        final urls = List.of(roots)..[i] = bad;
        expect(
          () => KinaiyaClient(
            sessionBase: urls[0],
            agentBase: urls[1],
            converseBase: urls[2],
            ownerToken: owner,
            httpClient: MockClient((r) async {
              sends++;
              return json({});
            }),
          ),
          throwsA(
            isA<FormatException>().having(
              (e) => e.toString(),
              'safe error',
              allOf(isNot(contains('secret')), isNot(contains(bad))),
            ),
          ),
        );
      }
    }
    expect(sends, 0);
    // Canonical syntax does not expand the existing low-level trust policy.
    final api = KinaiyaClient.mock(
      sessionBase: 'http://synthetic-session/',
      agentBase: 'http://synthetic-agent/',
      converseBase: 'http://synthetic-converse/',
      mockTransport: MockClient((_) async => json({})),
    );
    expect(api.agentBase, 'http://synthetic-agent');
    api.close();
  });

  test('equivalent root spelling does not replace same-backend drafts', () {
    final state = AppState();
    addTearDown(state.dispose);
    state.connect(profile(roots), injected: mockClient());
    state.setDraft(AppView.agent, 'literal Agent draft');
    state.setDraft(AppView.character, 'literal Converse draft');
    state.connect(
      profile(roots.map((r) => ' $r/ ').toList()),
      injected: mockClient(),
    );
    expect(state.agentDraft, 'literal Agent draft');
    expect(state.converseDraft, 'literal Converse draft');
  });

  test(
    'same canonical roots reach workspace Decisions and both memory services',
    () async {
      final requests = <http.Request>[];
      Future<http.Response> handle(http.Request r) async {
        requests.add(r);
        switch (r.url.path) {
          case '/proxy/v1/agent/harnesses':
            return json({
              'schema_version': '1.0',
              'configured': false,
              'items': [harnessJson('seed-v1', active: true)],
              'next_cursor': null,
            });
          case '/proxy/v1/decisions/inspections':
            return json(decisions.page());
          case '/proxy/v1/converse-memory/state':
            return json(memory.stateJson());
          case '/proxy/v1/memory/state':
            return json(memory.useJson());
          default:
            return http.Response('unexpected synthetic path', 404);
        }
      }

      final api = KinaiyaClient(
        sessionBase: ' ${roots[0]}/proxy/ ',
        agentBase: ' ${roots[1]}/proxy/ ',
        converseBase: ' ${roots[2]}/proxy/ ',
        ownerToken: owner,
        httpClient: MockClient(handle),
        memoryClientFactory: () => MockClient(handle),
        decisionsClientFactory: () => MockClient(handle),
        agentWorkspaceClientFactory: () => MockClient(handle),
      );
      addTearDown(api.close);
      await api.agentHarnesses();
      await api.decisionInspections.list({});
      await api.memory.state(MemoryBinding('s', 't', 1));
      await api.memory.useState(MemoryBinding('s', 't', 1));
      expect(requests.map((r) => r.url.host), [
        'agent.example',
        '127.0.0.1',
        '127.0.0.1',
        '::1',
      ]);
      expect(
        requests.every(
          (r) =>
              r.headers['Authorization'] == 'Bearer $owner' &&
              !r.followRedirects,
        ),
        isTrue,
      );
    },
  );

  testWidgets(
    'real setup canonicalizes roots before native authentication checks',
    (t) async {
      final requests = <http.Request>[];
      final state = AppState()
        ..clientFactory = (p) => KinaiyaClient(
          sessionBase: p.sessionUrl,
          agentBase: p.agentUrl,
          converseBase: p.converseUrl,
          ownerToken: p.ownerToken,
          agentWorkspaceClientFactory: () => MockClient((r) async {
            requests.add(r);
            return http.Response('Legacy host has no Options API', 404);
          }),
          httpClient: MockClient((r) async {
            requests.add(r);
            if (r.url.path == '/v1/sessions') return json([]);
            if (r.url.path == '/v1/agents/capabilities') return json({});
            if (r.url.path == '/health') {
              return json({
                'enabled': true,
                'provider': {
                  'local': true,
                  'provider': 'synthetic',
                  'model': 'synthetic',
                },
              });
            }
            if (r.url.path == '/v1/resources') {
              return json({'configured': false});
            }
            return http.Response('synthetic unsupported', 404);
          }),
        );
      addTearDown(state.dispose);
      await t.pumpWidget(
        MaterialApp(
          home: Builder(
            builder: (context) => TextButton(
              onPressed: () => showDialog<void>(
                context: context,
                builder: (_) => SetupDialog(state: state),
              ),
              child: const Text('Open setup'),
            ),
          ),
        ),
      );
      await t.tap(find.text('Open setup'));
      await t.pumpAndSettle();
      for (var i = 0; i < 3; i++) {
        await t.enterText(find.byType(TextField).at(i), ' \t${roots[i]}/ ');
      }
      await t.enterText(ownerTokenField, owner);
      await t.tap(find.text('Connect'));
      await t.pumpAndSettle();
      expect(state.sessionAuthenticated, isTrue);
      expect(state.agentOnline, isTrue);
      expect(state.converseOnline, isTrue);
      expect([
        state.connection!.sessionUrl,
        state.connection!.agentUrl,
        state.connection!.converseUrl,
      ], roots);
      expect(state.connection!.ownerToken, owner);
      expect(state.connection!.decisionsUrl, isNull);
      expect(state.connection!.backendProfile, BackendProfile.clean);
      expect(requests.every((r) => !r.url.path.startsWith('//')), isTrue);
      expect(
        requests
            .where((r) => r.url.path != '/health')
            .every((r) => r.headers['Authorization'] == 'Bearer $owner'),
        isTrue,
      );
      state.disconnect();
      await t.pumpWidget(const SizedBox.shrink());
    },
  );

  testWidgets(
    'setup rejects invalid paths separately for each service before connecting',
    (t) async {
      var factories = 0;
      final state = AppState()
        ..clientFactory = (p) {
          factories++;
          throw StateError('must not connect');
        };
      addTearDown(state.dispose);
      await t.pumpWidget(MaterialApp(home: SetupDialog(state: state)));
      await t.enterText(ownerTokenField, owner);
      for (var invalidRole = 0; invalidRole < 3; invalidRole++) {
        for (var i = 0; i < 3; i++) {
          await t.enterText(
            find.byType(TextField).at(i),
            '${roots[i]}${i == invalidRole ? '/a/../b' : '/'}',
          );
        }
        await t.tap(find.text('Connect'));
        await t.pump();
        expect(find.byType(SetupDialog), findsOneWidget);
        expect(find.textContaining('invalid service path'), findsOneWidget);
        expect(factories, 0);
        expect(state.connection, isNull);
      }
      await t.pumpWidget(const SizedBox.shrink());
    },
  );
}
