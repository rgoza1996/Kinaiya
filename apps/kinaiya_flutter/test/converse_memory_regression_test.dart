import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/converse_memory.dart';

class Fixture {
  Fixture(this.id);
  final String id;
  int revision = 3;
  bool enabled = true;
  bool failSession = false;
  bool failUse = false;
  int generation = 5;
  final writes = <Map<String, Object?>>[];
  Future<http.Response> Function()? stateRead;
  Future<http.Response> Function(Map<String, Object?>)? namespaceWrite;

  Map<String, Object?> get scope => {
    'session_id': id,
    'thread_id': 'thread',
    'epoch': 1,
  };
  Map<String, Object?> ns({int? rev, bool? on}) => {
    'schema_version': '1.0',
    ...scope,
    'enabled': on ?? enabled,
    'revision': rev ?? revision,
    'node_count': 1,
    'status': 'ready',
  };
  Map<String, Object?> use({bool on = false}) => {
    'schema_version': '1.0',
    ...scope,
    'runtime_id': 'runtime',
    'use_generation': generation,
    'enabled': on,
    'implementation_id': on ? 'korina-dag-v1' : null,
    'selector_ids': on ? ['note'] : <String>[],
    'pending': false,
    'notice': on ? 'ready' : 'disabled',
  };
  http.Response ok(Object value) => http.Response(
    jsonEncode(value),
    200,
    headers: {'content-type': 'application/json'},
  );

  Future<http.Response> respond(http.Request request) async {
    final body = (jsonDecode(request.body) as Map).cast<String, Object?>();
    switch (request.url.path) {
      case '/v1/converse-memory/state':
        if (failSession) return http.Response('', 503);
        return stateRead?.call() ?? ok(ns());
      case '/v1/converse-memory/catalog':
        if (failSession) return http.Response('', 503);
        return ok({
          'schema_version': '1.0',
          'state': ns(),
          'entries': [
            {
              'schema_version': '1.0',
              'node_id': 'note',
              'ordinal': 1,
              'kind': 'assertion',
              'state': 'active',
              'preview': 'Public synthetic note',
              'preview_truncated': false,
            },
          ],
        });
      case '/v1/memory/state':
        if (failUse) return http.Response('', 503);
        return ok(use());
      case '/v1/converse-memory/configure':
        writes.add(body);
        return namespaceWrite?.call(body) ??
            ok(ns(rev: revision + 1, on: body['enabled'] as bool));
      case '/v1/memory/use':
        writes.add(body);
        generation++;
        return ok(use(on: body['enabled'] as bool));
    }
    throw StateError('Unexpected synthetic endpoint');
  }

  AppState app() {
    final client = KinaiyaClient.mock(
      sessionBase: 'http://127.0.0.1:19980',
      agentBase: 'http://127.0.0.1:19981',
      converseBase: 'http://127.0.0.1:19982',
      mockTransport: MockClient((_) async => http.Response('', 500)),
      memoryClientFactory: () => MockClient(respond),
    );
    final state = AppState()
      ..connect(
        ConnectionProfile(
          sessionUrl: client.sessionBase,
          agentUrl: client.agentBase,
          converseUrl: client.converseBase,
          ownerToken: 'public-synthetic-owner',
        ),
        injected: client,
      );
    state.converseSession = SessionInfo(
      sessionId: id,
      threadId: 'thread',
      channel: 'converse',
      epoch: 1,
    );
    addTearDown(state.dispose);
    return state;
  }
}

Future<void> open(WidgetTester tester, AppState state) async {
  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(
        body: Builder(
          builder: (context) => TextButton(
            key: const ValueKey('open'),
            onPressed: () =>
                showConverseMemoryDialog(context: context, state: state),
            child: const Text('Open'),
          ),
        ),
      ),
    ),
  );
  await tester.tap(find.byKey(const ValueKey('open')));
  await tester.pumpAndSettle();
}

Future<void> click(WidgetTester tester, String key) async {
  // Exercise actual enabled widget handlers without pointer visibility noise.
  // This is a lifetime/state probe; it is not a hit-testing/layout claim.
  final button = tester.widget<ButtonStyleButton>(find.byKey(ValueKey(key)));
  expect(button.onPressed, isNotNull, reason: 'Expected actionable $key');
  button.onPressed!();
  await tester.pumpAndSettle();
}

void select(WidgetTester tester) {
  tester
      .widget<CheckboxListTile>(
        find.byKey(const ValueKey('memory-select-note')),
      )
      .onChanged!(true);
}

void main() {
  testWidgets('known Stop survives Session auth flag loss on unchanged scope', (
    tester,
  ) async {
    final fixture = Fixture('independent-stop-auth');
    final state = fixture.app();
    await open(tester, state);
    expect(
      tester
          .widget<OutlinedButton>(
            find.byKey(const ValueKey('converse-memory-stop')),
          )
          .onPressed,
      isNotNull,
    );
    state.sessionAuthenticated = false;
    state.notifyListeners();
    await tester.pumpAndSettle();
    final stop = find.byKey(const ValueKey('converse-memory-stop'));
    expect(
      stop,
      findsOneWidget,
      reason: 'Same client/generation/binding retains last runtime Stop',
    );
    expect(tester.widget<OutlinedButton>(stop).onPressed, isNotNull);
  });

  testWidgets('failed same-scope refresh keeps last observed Stop', (
    tester,
  ) async {
    final fixture = Fixture('independent-stop-refresh');
    await open(tester, fixture.app());
    fixture.failSession = true;
    fixture.failUse = true;
    await click(tester, 'converse-memory-refresh');
    final stop = find.byKey(const ValueKey('converse-memory-stop'));
    expect(
      stop,
      findsOneWidget,
      reason: 'Read failure is not revocation of last runtime snapshot',
    );
    expect(tester.widget<OutlinedButton>(stop).onPressed, isNotNull);
  });

  testWidgets(
    'incoherent older namespace cannot enable catalog from disabled newer revision',
    (tester) async {
      final fixture = Fixture('independent-read-order')
        ..revision = 4
        ..enabled = false;
      final old = Completer<http.Response>();
      fixture.stateRead = () => old.future;
      await open(tester, fixture.app());
      select(tester);
      old.complete(fixture.ok(fixture.ns(rev: 3, on: true)));
      await tester.pumpAndSettle();
      tester
          .widget<FilledButton>(
            find.byKey(const ValueKey('converse-memory-use')),
          )
          .onPressed
          ?.call();
      await tester.pumpAndSettle();
      expect(
        fixture.writes,
        isEmpty,
        reason:
            'Only coherent namespace/catalog snapshots can authorize enable',
      );
    },
  );

  testWidgets(
    'late namespace acknowledgment cannot regress a refreshed newer revision',
    (tester) async {
      final fixture = Fixture('independent-late-action');
      final late = Completer<http.Response>();
      fixture.namespaceWrite = (_) => late.future;
      await open(tester, fixture.app());
      await click(tester, 'converse-memory-namespace-off');
      expect(fixture.writes.length, 1);
      fixture.revision = 5;
      await click(tester, 'converse-memory-refresh');
      expect(find.textContaining('Saved access: On'), findsOneWidget);
      late.complete(fixture.ok(fixture.ns(rev: 4, on: false)));
      await tester.pumpAndSettle();
      expect(
        find.textContaining('Saved access: On'),
        findsOneWidget,
        reason: 'Earlier write receipt is not newer than accepted refresh',
      );
      fixture.namespaceWrite = null;
      await click(tester, 'converse-memory-namespace-off');
      expect(fixture.writes.length, 2);
      expect(
        fixture.writes.last['expected_revision'],
        5,
        reason: 'The next explicit write uses the accepted newer revision',
      );
    },
  );

  testWidgets('matching snapshots allow one explicit selected-use request', (
    tester,
  ) async {
    final fixture = Fixture('independent-good-control');
    await open(tester, fixture.app());
    select(tester);
    await tester.pump();
    await click(tester, 'converse-memory-use');
    expect(fixture.writes.length, 1);
    expect(fixture.writes.single['expected_generation'], 5);
    expect(fixture.writes.single['selector_ids'], ['note']);
    expect(fixture.writes.single['enabled'], true);
    expect(tester.takeException(), isNull);
  });
}
