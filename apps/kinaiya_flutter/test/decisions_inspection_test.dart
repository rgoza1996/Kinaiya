import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/decisions_client.dart';
import 'package:kinaiya_flutter/api/decisions_models.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/decisions_view.dart';

Map<String, Object?> row({
  String id = 'source-2',
  int sequence = 2,
  bool agent = false,
}) => {
  'source_event_id': id,
  'source_sequence': sequence,
  'session_id': 'session',
  'thread_id': 'thread',
  'epoch': 1,
  'source_kind': agent ? 'agent.command' : 'conversation.user',
  'source_origin': 'authenticated-user',
  'source_revision': 1,
  'created_at': '2026-10-30T12:00:00Z',
  'preview': 'Literal <script>🙂 source',
  'disposition': agent ? 'no_inference' : 'classified',
  'outcome': agent ? null : 'ignore',
  'decision_id': agent ? null : 'decision',
  'provider': agent ? null : 'laya',
  'model': agent ? null : 'historical-model',
  'confidence': null,
  'call_count': agent ? 0 : 1,
  'capture_state': agent ? 'not_applicable' : 'retained',
};
Map<String, Object?> page({List<Map<String, Object?>>? rows, int? next}) => {
  'contract_version': 1,
  'through_sequence': 2,
  'next_before_sequence': next,
  'exhausted': next == null,
  'timezone': 'UTC',
  'range_start_utc': null,
  'range_end_utc': null,
  'rows': rows ?? [row()],
};
Map<String, Object?> detail({
  String id = 'source-2',
  int sequence = 2,
  bool agent = false,
}) => {
  'contract_version': 1,
  'source': {
    'event_id': id,
    'sequence': sequence,
    'session_id': 'session',
    'thread_id': 'thread',
    'epoch': 1,
    'kind': agent ? 'agent.command' : 'conversation.user',
    'origin': 'authenticated-user',
    'created_at': '2026-10-30T12:00:00Z',
    'payload': {'text': 'Literal <script>🙂 source'},
  },
  'summary': row(id: id, sequence: sequence, agent: agent),
  'receipts': [],
  'receipts_truncated': false,
  'through_call_sequence': agent ? 0 : 7,
  'next_before_call_sequence': null,
  'exhausted': true,
  'calls': agent
      ? []
      : [
          {
            'call_sequence': 7,
            'call_id': 'call',
            'attempt_id': 'attempt',
            'attempt': 1,
            'source_event_id': id,
            'status': 'completed',
            'capture_state': 'retained',
            'provider': 'laya',
            'model': 'historical-model',
            'artifact_sha256': null,
            'created_at': '2026-10-30T12:00:01Z',
            'completed_at': '2026-10-30T12:00:02Z',
            'request': {
              'state': 'EXACT synthetic Ω <script>',
              'questions': {
                'action': {
                  'type': 'single_choice',
                  'possible_answers': ['ignore', 'respond'],
                },
              },
            },
            'raw_response': '{"action":"ignore"}',
            'validated_output': {'action': 'ignore'},
            'reused_from_call_id': null,
            'error_type': null,
          },
        ],
};
http.Response jsonResponse(Object value, {int status = 200}) => http.Response(
  jsonEncode(value),
  status,
  headers: {'content-type': 'application/json; charset=utf-8'},
);
AppState state(Future<http.Response> Function(http.Request) handler) =>
    AppState()
      ..client = KinaiyaClient.mock(
        sessionBase: 'http://session',
        agentBase: 'http://agent',
        converseBase: 'http://converse',
        mockTransport: MockClient((_) async => jsonResponse({})),
        decisionsClientFactory: () => MockClient(handler),
      );
Future<void> show(
  WidgetTester t,
  AppState s, {
  Size size = const Size(1600, 950),
  double scale = 1,
}) async {
  t.view.physicalSize = size;
  t.view.devicePixelRatio = 1;
  await t.pumpWidget(
    MaterialApp(
      home: MediaQuery(
        data: MediaQueryData(size: size, textScaler: TextScaler.linear(scale)),
        child: Scaffold(body: DecisionsView(state: s)),
      ),
    ),
  );
  await t.pumpAndSettle();
}

class HangingClient extends http.BaseClient {
  final result = Completer<http.StreamedResponse>();
  int closes = 0;
  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) => result.future;
  @override
  void close() {
    closes++;
  }
}

void main() {
  test(
    'models preserve actual Unicode questions and absent historical confidence',
    () {
      final d = DecisionDetail.fromJson(detail());
      expect(d.summary.confidence, isNull);
      expect(d.summary.confidenceLabel, 'Unavailable');
      expect(
        decisionsLiteral(d.calls.single.data['request']),
        contains('EXACT synthetic Ω <script>'),
      );
      expect(
        decisionsLiteral(d.calls.single.data['request']),
        contains('possible_answers'),
      );
      expect(DecisionDetail.fromJson(detail(agent: true)).calls, isEmpty);
      expect(
        () => DecisionPage.fromJson({...page(), 'contract_version': 2}),
        throwsFormatException,
      );
      expect(
        () => DecisionPage.fromJson(page(rows: [row(), row()])),
        throwsFormatException,
      );
      expect(
        () => DecisionRow.fromJson({...row(), 'confidence': double.nan}),
        throwsFormatException,
      );
      final wrong = detail();
      wrong['source'] = {...(wrong['source'] as Map), 'session_id': 'other'};
      expect(() => DecisionDetail.fromJson(wrong), throwsFormatException);
    },
  );
  test(
    'owner GETs carry filters/cursors and never follow redirects or disclose in URL',
    () async {
      final requests = <http.Request>[];
      final api = DecisionsApi(
        base: 'http://session/prefix/',
        ownerToken: 'synthetic-owner-token',
        clientFactory: () => MockClient((r) async {
          requests.add(r);
          return jsonResponse(
            r.url.path.endsWith('source-2') ? detail() : page(),
          );
        }),
      );
      await api.list(
        {
          'source': 'agent',
          'search': '🙂&q',
          'timezone': 'America/Los_Angeles',
        },
        through: 2,
        before: 2,
      );
      await api.detail('source-2', through: 7, before: 7);
      expect(requests.map((r) => r.method), everyElement('GET'));
      expect(requests.first.url.path, '/prefix/v1/decisions/inspections');
      expect(requests.first.url.queryParameters['search'], '🙂&q');
      expect(requests.first.url.queryParameters['through_sequence'], '2');
      expect(requests.last.url.queryParameters['before_call_sequence'], '7');
      expect(
        requests.first.headers['Authorization'],
        'Bearer synthetic-owner-token',
      );
      expect(
        requests.every(
          (r) => !r.followRedirects && !r.url.toString().contains('token'),
        ),
        isTrue,
      );
      api.close();
    },
  );
  test(
    'size limits, malformed UTF8 and 404 fail without reflecting remote body',
    () async {
      for (final response in [
        http.Response('PRIVATE BODY', 404),
        http.Response.bytes([0xff], 200),
        http.Response('x' * (256 * 1024 + 1), 200),
      ]) {
        final api = DecisionsApi(
          base: 'http://session',
          ownerToken: 'token',
          clientFactory: () => MockClient((_) async => response),
        );
        await expectLater(
          api.list({}),
          throwsA(
            isA<DecisionsFailure>().having(
              (e) => e.message,
              'safe message',
              isNot(contains('PRIVATE BODY')),
            ),
          ),
        );
        api.close();
      }
    },
  );
  test(
    'timed out underlying reads retain bounded slots until settlement',
    () async {
      final clients = <HangingClient>[];
      final api = DecisionsApi(
        base: 'http://session',
        ownerToken: 'token',
        timeout: const Duration(milliseconds: 2),
        clientFactory: () {
          final c = HangingClient();
          clients.add(c);
          return c;
        },
      );
      for (var i = 0; i < 2; i++) {
        await expectLater(api.list({}), throwsA(isA<DecisionsFailure>()));
      }
      await expectLater(api.list({}), throwsA(isA<DecisionsFailure>()));
      expect(clients.length, 2);
      expect(clients.map((c) => c.closes), everyElement(1));
      api.close();
      for (final c in clients) {
        c.result.completeError(StateError('synthetic late error'));
      }
      await Future<void>.delayed(Duration.zero);
    },
  );
  testWidgets(
    'timeline shows literal captured evidence without executing requests',
    (t) async {
      final requests = <http.Request>[];
      final s = state((r) async {
        requests.add(r);
        return jsonResponse(
          r.url.path.endsWith('source-2') ? detail() : page(),
        );
      });
      addTearDown(s.dispose);
      addTearDown(t.view.resetPhysicalSize);
      addTearDown(t.view.resetDevicePixelRatio);
      await show(t, s);
      expect(find.text('historical-model'), findsOneWidget);
      await t.ensureVisible(find.text('ignore\nLiteral <script>🙂 source'));
      await t.tap(find.text('ignore\nLiteral <script>🙂 source'));
      await t.pumpAndSettle();
      expect(find.textContaining('Source: source-2'), findsOneWidget);
      expect(find.textContaining('EXACT synthetic Ω <script>'), findsOneWidget);
      expect(find.text('Script passed to model'), findsOneWidget);
      expect(find.textContaining('Confidence: Unavailable'), findsOneWidget);
      expect(requests.length, 2);
      expect(requests.map((r) => r.method), everyElement('GET'));
      expect(t.takeException(), isNull);
    },
  );
  testWidgets('Agent observation has explicit no-inference provenance', (
    t,
  ) async {
    final s = state(
      (r) async => jsonResponse(
        r.url.path.endsWith('source-2')
            ? detail(agent: true)
            : page(rows: [row(agent: true)]),
      ),
    );
    addTearDown(s.dispose);
    addTearDown(t.view.resetPhysicalSize);
    addTearDown(t.view.resetDevicePixelRatio);
    await show(t, s);
    await t.ensureVisible(find.text('no_inference\nLiteral <script>🙂 source'));
    await t.tap(find.text('no_inference\nLiteral <script>🙂 source'));
    await t.pumpAndSettle();
    expect(
      find.text('No Decisions inference. This is a native Agent observation.'),
      findsOneWidget,
    );
    expect(find.text('Script passed to model'), findsNothing);
  });
  testWidgets('stable timeline cursors page back and forward', (t) async {
    final requests = <http.Request>[];
    final s = state((r) async {
      requests.add(r);
      expect(
        r.url.queryParameters.containsKey('through_sequence'),
        r.url.queryParameters.containsKey('before_sequence'),
      );
      return jsonResponse(
        r.url.queryParameters['before_sequence'] != '2'
            ? page(next: 2)
            : page(rows: [row(id: 'source-1', sequence: 1)]),
      );
    });
    addTearDown(s.dispose);
    addTearDown(t.view.resetPhysicalSize);
    addTearDown(t.view.resetDevicePixelRatio);
    await show(t, s);
    await t.tap(find.byTooltip('Older sources'));
    await t.pumpAndSettle();
    expect(requests.last.url.queryParameters['before_sequence'], '2');
    expect(requests.last.url.queryParameters['through_sequence'], '2');
    await t.tap(find.byTooltip('Newer sources'));
    await t.pumpAndSettle();
    expect(requests.last.url.queryParameters['before_sequence'], '3');
  });
  testWidgets(
    'narrow screen and 2x text retain scrollable filter and evidence access',
    (t) async {
      final s = state((_) async => jsonResponse(page(rows: [])));
      addTearDown(s.dispose);
      addTearDown(t.view.resetPhysicalSize);
      addTearDown(t.view.resetDevicePixelRatio);
      await show(t, s, size: const Size(320, 640), scale: 2);
      expect(find.text('Decisions'), findsOneWidget);
      expect(
        find.text('No recorded sources match these filters.'),
        findsOneWidget,
      );
      expect(t.takeException(), isNull);
    },
  );
  testWidgets('late detail from another connection cannot display', (t) async {
    final pending = Completer<http.Response>();
    final s = state(
      (r) async => r.url.path.endsWith('source-2')
          ? pending.future
          : jsonResponse(page()),
    );
    addTearDown(s.dispose);
    addTearDown(t.view.resetPhysicalSize);
    addTearDown(t.view.resetDevicePixelRatio);
    await show(t, s);
    await t.ensureVisible(find.text('ignore\nLiteral <script>🙂 source'));
    await t.tap(find.text('ignore\nLiteral <script>🙂 source'));
    await t.pump();
    s.disconnect();
    pending.complete(jsonResponse(detail()));
    await t.pumpAndSettle();
    expect(find.textContaining('EXACT synthetic Ω <script>'), findsNothing);
    expect(find.text('Connect to inspect recorded decisions.'), findsOneWidget);
  });
  testWidgets(
    'replacing host clears search filters and full-evidence route before its first build',
    (t) async {
      final old = state(
        (r) async =>
            jsonResponse(r.url.path.endsWith('source-2') ? detail() : page()),
      );
      final reads = <http.Request>[];
      final next = state((r) async {
        reads.add(r);
        return jsonResponse(page(rows: []));
      });
      addTearDown(old.dispose);
      addTearDown(next.dispose);
      addTearDown(t.view.resetPhysicalSize);
      addTearDown(t.view.resetDevicePixelRatio);
      await show(t, old);
      await t.enterText(
        find.widgetWithText(TextField, 'Search decisions or source text'),
        'PRIVATE SEARCH',
      );
      await t.ensureVisible(find.text('Apply filters'));
      await t.tap(find.text('Apply filters'));
      await t.pumpAndSettle();
      await t.ensureVisible(find.text('ignore\nLiteral <script>🙂 source'));
      await t.tap(find.text('ignore\nLiteral <script>🙂 source'));
      await t.pumpAndSettle();
      await t.ensureVisible(find.text('View full evidence').first);
      await t.tap(find.text('View full evidence').first);
      // No pump: the dialog builder has not run when identity changes.
      await show(t, next);
      await t.pumpAndSettle();
      expect(find.text('Close evidence'), findsNothing);
      expect(
        Navigator.of(t.element(find.byType(DecisionsView))).canPop(),
        isFalse,
      );
      expect(reads, isNotEmpty);
      expect(
        reads.every(
          (r) =>
              !r.url.queryParameters.containsKey('search') &&
              !r.url.toString().contains('PRIVATE'),
        ),
        isTrue,
      );
      expect(find.text('PRIVATE SEARCH'), findsNothing);
    },
  );
  testWidgets('populated narrow evidence remains accessible at 2x text', (
    t,
  ) async {
    final s = state(
      (r) async =>
          jsonResponse(r.url.path.endsWith('source-2') ? detail() : page()),
    );
    addTearDown(s.dispose);
    addTearDown(t.view.resetPhysicalSize);
    addTearDown(t.view.resetDevicePixelRatio);
    await show(t, s, size: const Size(320, 640), scale: 2);
    await t.ensureVisible(find.text('ignore\nLiteral <script>🙂 source'));
    await t.tap(find.text('ignore\nLiteral <script>🙂 source'));
    await t.pumpAndSettle();
    await t.ensureVisible(find.text('Copy Script passed to model'));
    expect(t.takeException(), isNull);
    await t.ensureVisible(find.text('Back to sources'));
    await t.tap(find.text('Back to sources'));
    await t.pumpAndSettle();
    expect(t.takeException(), isNull);
  });
  test(
    'detail binds source provenance and rejects Agent inference contradictions',
    () {
      for (final field in ['kind', 'origin', 'created_at']) {
        final d = detail();
        d['source'] = {...(d['source'] as Map), field: 'contradiction'};
        expect(() => DecisionDetail.fromJson(d), throwsFormatException);
      }
      final d = detail(agent: true);
      d['calls'] = detail()['calls'];
      expect(() => DecisionDetail.fromJson(d), throwsFormatException);
      final revision = detail();
      revision['source'] = {
        ...(revision['source'] as Map),
        'payload': {'revision': 2},
      };
      expect(() => DecisionDetail.fromJson(revision), throwsFormatException);
    },
  );
  test(
    'source preview uses Unicode character bounds and rejects unpaired surrogates',
    () {
      expect(
        DecisionRow.fromJson({
          ...row(),
          'preview': '🙂' * 1000,
        }).preview.runes.length,
        1000,
      );
      expect(
        () => DecisionRow.fromJson({
          ...row(),
          'preview': String.fromCharCode(0xd800),
        }),
        throwsFormatException,
      );
    },
  );
  test(
    'combined date/session filters and detail cursors preserve native query fields',
    () async {
      final requests = <http.Request>[];
      final api = DecisionsApi(
        base: 'http://session',
        ownerToken: 'token',
        clientFactory: () => MockClient((r) async {
          requests.add(r);
          return jsonResponse(
            r.url.path.endsWith('source-2') ? detail() : page(),
          );
        }),
      );
      await api.list({
        'session_id': 'session',
        'source': 'conversation',
        'outcome': 'ignore',
        'search': 'literal Ω',
        'from_date': '2026-11-01',
        'to_date': '2026-11-01',
        'timezone': 'America/Los_Angeles',
      });
      expect(
        requests.single.url.queryParameters,
        containsPair('session_id', 'session'),
      );
      expect(
        requests.single.url.queryParameters,
        containsPair('from_date', '2026-11-01'),
      );
      expect(
        requests.single.url.queryParameters,
        containsPair('to_date', '2026-11-01'),
      );
      expect(
        requests.single.url.queryParameters,
        containsPair('timezone', 'America/Los_Angeles'),
      );
      await api.detail('source-2', through: 7, before: 7);
      expect(
        requests.last.url.queryParameters,
        containsPair('through_call_sequence', '7'),
      );
      expect(
        requests.last.url.queryParameters,
        containsPair('before_call_sequence', '7'),
      );
      api.close();
    },
  );
  testWidgets(
    'expired captures stay unavailable and reused inference links navigate actual source',
    (t) async {
      final reused = detail();
      final call = ((reused['calls'] as List).single as Map);
      call['status'] = 'reused';
      call['request'] = null;
      call['raw_response'] = null;
      call['validated_output'] = null;
      call['reused_from_call_id'] = 'original-call';
      call['reused_from_source_event_id'] = 'source-original';
      call['capture_state'] = 'expired';
      final seen = <String>[];
      final s = state((r) async {
        seen.add(r.url.path);
        return jsonResponse(
          r.url.path.endsWith('source-original')
              ? detail(id: 'source-original')
              : r.url.path.endsWith('source-2')
              ? reused
              : page(),
        );
      });
      addTearDown(s.dispose);
      addTearDown(t.view.resetPhysicalSize);
      addTearDown(t.view.resetDevicePixelRatio);
      await show(t, s);
      await t.ensureVisible(find.text('ignore\nLiteral <script>🙂 source'));
      await t.tap(find.text('ignore\nLiteral <script>🙂 source'));
      await t.pumpAndSettle();
      expect(find.textContaining('Capture: expired'), findsOneWidget);
      expect(find.textContaining('EXACT synthetic Ω <script>'), findsNothing);
      await t.ensureVisible(find.text('Inspect original inference'));
      await t.tap(find.text('Inspect original inference'));
      await t.pumpAndSettle();
      expect(seen.last, endsWith('/source-original'));
      expect(find.textContaining('Source: source-original'), findsOneWidget);
    },
  );
}
