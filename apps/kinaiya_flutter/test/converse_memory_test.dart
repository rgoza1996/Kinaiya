/// Owner Converse memory dialog tests (ADR 0052).
///
/// Public synthetic fixtures only: [KinaiyaClient.mock] with an injected
/// [memoryClientFactory] backed by MockClient. No real sockets, models, or
/// services. Every binding uses synthetic ids; each test owns its scope so
/// the module-level uncertainty latches cannot leak between tests.
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/memory_models.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/converse_memory.dart';

Map<String, Object?> _nsJson(
  MemoryBinding binding, {
  bool enabled = true,
  int revision = 3,
  int count = 2,
  String status = 'ready',
}) => {
  'schema_version': '1.0',
  'session_id': binding.sessionId,
  'thread_id': binding.threadId,
  'epoch': binding.epoch,
  'enabled': enabled,
  'revision': revision,
  'node_count': count,
  'status': status,
};

Map<String, Object?> _summaryJson({
  required String id,
  required int ordinal,
  String kind = 'assertion',
  String state = 'active',
  String? preview = 'synthetic preview',
  bool truncated = false,
}) => {
  'schema_version': '1.0',
  'node_id': id,
  'ordinal': ordinal,
  'kind': kind,
  'state': state,
  'preview': preview,
  'preview_truncated': truncated,
};

Map<String, Object?> _nodeJson(
  MemoryBinding binding, {
  required String id,
  required int ordinal,
  String kind = 'assertion',
  String state = 'active',
  String? body = 'full literal body',
}) => {
  'schema_version': '1.0',
  'session_id': binding.sessionId,
  'thread_id': binding.threadId,
  'epoch': binding.epoch,
  'node_id': id,
  'ordinal': ordinal,
  'kind': kind,
  'source_event_id': 'e-1',
  'source_revision': 1,
  'source_sequence': 1,
  'prerequisite_ids': const <String>[],
  'body': body,
  'fact': null,
  'state': state,
  'content_scope': 'local_only',
  'provenance': 'owner_curated',
};

Map<String, Object?> _useJson(
  MemoryBinding binding, {
  String runtime = 'rt-1',
  int generation = 5,
  bool enabled = false,
  List<String> selectors = const [],
  bool pending = false,
  String notice = 'disabled',
}) => {
  'schema_version': '1.0',
  'session_id': binding.sessionId,
  'thread_id': binding.threadId,
  'epoch': binding.epoch,
  'runtime_id': runtime,
  'use_generation': generation,
  'enabled': enabled,
  'implementation_id': enabled ? 'korina-dag-v1' : null,
  'selector_ids': selectors,
  'pending': pending,
  'notice': notice,
};

http.Response _ok(Map<String, Object?> json) => http.Response(
  jsonEncode(json),
  200,
  headers: {'content-type': 'application/json'},
);

/// Scriptable synthetic memory backend. [behavior] overrides per path;
/// unset paths serve the current canned scope and record their payload.
class MemoryScript {
  MemoryScript(this.binding);

  final MemoryBinding binding;
  final List<String> paths = [];
  final List<Map<String, Object?>> payloads = [];
  final Map<String, Future<http.Response> Function(Map<String, Object?>)>
  behavior = {};

  bool nsEnabled = true;
  int revision = 3;
  String nsStatus = 'ready';
  List<Map<String, Object?>> entries = [];
  String runtimeId = 'rt-1';
  int generation = 5;
  bool useEnabled = false;
  List<String> selectors = const [];
  bool pending = false;
  String useNotice = 'disabled';

  void seedTwoActive() {
    entries = [
      _summaryJson(id: 'n-1', ordinal: 1, preview: 'first synthetic note'),
      _summaryJson(id: 'n-2', ordinal: 2, preview: 'second synthetic note'),
    ];
  }

  Future<http.Response> call(http.Request request) async {
    final body = request.body.isEmpty
        ? <String, Object?>{}
        : (jsonDecode(request.body) as Map).cast<String, Object?>();
    paths.add(request.url.path);
    payloads.add(body);
    final override = behavior[request.url.path];
    if (override != null) return override(body);
    switch (request.url.path) {
      case '/v1/converse-memory/state':
        return _ok(
          _nsJson(
            binding,
            enabled: nsEnabled,
            revision: revision,
            count: entries.length,
            status: nsStatus,
          ),
        );
      case '/v1/converse-memory/configure':
        nsEnabled = body['enabled']! as bool;
        revision += 1;
        return _ok(
          _nsJson(
            binding,
            enabled: nsEnabled,
            revision: revision,
            count: entries.length,
            status: nsStatus,
          ),
        );
      case '/v1/converse-memory/catalog':
        return _ok({
          'schema_version': '1.0',
          'state': _nsJson(
            binding,
            enabled: nsEnabled,
            revision: revision,
            count: entries.length,
            status: nsStatus,
          ),
          'entries': entries,
        });
      case '/v1/converse-memory/inspect':
        final id = body['node_id']! as String;
        return _ok({
          'schema_version': '1.0',
          'state': _nsJson(
            binding,
            enabled: nsEnabled,
            revision: revision,
            count: entries.length,
            status: nsStatus,
          ),
          'node': _nodeJson(binding, id: id, ordinal: 1),
        });
      case '/v1/memory/state':
        return _ok(
          _useJson(
            binding,
            runtime: runtimeId,
            generation: generation,
            enabled: useEnabled,
            selectors: selectors,
            pending: pending,
            notice: useNotice,
          ),
        );
      case '/v1/memory/use':
        final enabled = body['enabled']! as bool;
        if (enabled) {
          useEnabled = true;
          selectors = List<String>.from(body['selector_ids']! as List);
          generation += 1;
          useNotice = 'ready';
        } else {
          useEnabled = false;
          selectors = const [];
          useNotice = 'disabled';
          if (generation != 0) generation += 1;
        }
        return _ok(
          _useJson(
            binding,
            runtime: runtimeId,
            generation: generation,
            enabled: useEnabled,
            selectors: selectors,
            notice: useNotice,
          ),
        );
    }
    return http.Response('unexpected ${request.url.path}', 500);
  }
}

KinaiyaClient _memoryClient(MemoryScript script) => KinaiyaClient.mock(
  sessionBase: 'http://127.0.0.1:8760',
  agentBase: 'http://127.0.0.1:8761',
  converseBase: 'http://127.0.0.1:8762',
  mockTransport: MockClient((_) async => http.Response('unexpected', 500)),
  memoryClientFactory: () => MockClient(script.call),
);

ConnectionProfile _profile() => ConnectionProfile(
  sessionUrl: 'http://127.0.0.1:8760',
  agentUrl: 'http://127.0.0.1:8761',
  converseUrl: 'http://127.0.0.1:8762',
  ownerToken: 'synthetic-owner',
);

AppState _memoryState(KinaiyaClient api, MemoryBinding binding) {
  final state = AppState()..connect(_profile(), injected: api);
  state.converseSession = SessionInfo(
    sessionId: binding.sessionId,
    threadId: binding.threadId,
    channel: 'converse',
    epoch: binding.epoch,
  );
  addTearDown(state.dispose);
  return state;
}

Future<void> _openMemory(WidgetTester tester, AppState state) async {
  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(
        body: Builder(
          builder: (context) => TextButton(
            key: const ValueKey('open-memory'),
            onPressed: () =>
                showConverseMemoryDialog(context: context, state: state),
            child: const Text('open'),
          ),
        ),
      ),
    ),
  );
  await tester.tap(find.byKey(const ValueKey('open-memory')));
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 100));
}

Future<void> _settle(WidgetTester tester) async {
  await tester.pump(const Duration(milliseconds: 100));
}

/// Scrolls the dialog content to [finder] before tapping: the dialog body
/// scrolls at small sizes and large text, so off-screen controls must be
/// brought into view first.
Future<void> _tapScroll(WidgetTester tester, Finder finder) async {
  await tester.scrollUntilVisible(
    finder,
    80,
    scrollable: find.byType(Scrollable).first,
  );
  await tester.tap(finder);
  await _settle(tester);
}

void main() {
  testWidgets('open performs only owner reads, never session/configure/use', (
    tester,
  ) async {
    final binding = MemoryBinding('s-reads', 't-reads', 1);
    final script = MemoryScript(binding)..seedTwoActive();
    final state = _memoryState(_memoryClient(script), binding);
    await _openMemory(tester, state);
    expect(find.textContaining('Conversation s-reads'), findsOneWidget);
    expect(find.text('Saved notes'), findsOneWidget);
    expect(script.paths.toSet(), {
      '/v1/converse-memory/state',
      '/v1/converse-memory/catalog',
      '/v1/memory/state',
    });
    for (final path in script.paths) {
      expect(path, isNot(contains('configure')));
    }
    expect(script.paths.where((p) => p.endsWith('/use')), isEmpty);
    expect(tester.takeException(), isNull);
  });

  testWidgets('empty catalog is distinct from errors', (tester) async {
    final binding = MemoryBinding('s-empty', 't-empty', 1);
    final script = MemoryScript(binding);
    final state = _memoryState(_memoryClient(script), binding);
    await _openMemory(tester, state);
    expect(
      find.text('No saved notes yet for this conversation.'),
      findsOneWidget,
    );
    final use = tester.widget<FilledButton>(
      find.byKey(const ValueKey('converse-memory-use')),
    );
    expect(use.onPressed, isNull);
    expect(tester.takeException(), isNull);
  });

  testWidgets('namespace off retains notes and blocks use', (tester) async {
    final binding = MemoryBinding('s-off', 't-off', 1);
    final script = MemoryScript(binding)
      ..seedTwoActive()
      ..nsEnabled = false;
    final state = _memoryState(_memoryClient(script), binding);
    await _openMemory(tester, state);
    expect(find.textContaining('Saved access: Off'), findsOneWidget);
    expect(find.text('first synthetic note'), findsOneWidget);
    await _tapScroll(tester, find.byKey(const ValueKey('memory-select-n-1')));
    final use = tester.widget<FilledButton>(
      find.byKey(const ValueKey('converse-memory-use')),
    );
    expect(use.onPressed, isNull);
    expect(
      find.textContaining('Turning saved access off keeps your saved notes'),
      findsWidgets,
    );
    expect(tester.takeException(), isNull);
  });

  testWidgets('quarantined namespace blocks use but stays literal', (
    tester,
  ) async {
    final binding = MemoryBinding('s-quar', 't-quar', 1);
    final script = MemoryScript(binding)
      ..seedTwoActive()
      ..nsStatus = 'quarantined';
    final state = _memoryState(_memoryClient(script), binding);
    await _openMemory(tester, state);
    expect(find.textContaining('status quarantined'), findsOneWidget);
    final use = tester.widget<FilledButton>(
      find.byKey(const ValueKey('converse-memory-use')),
    );
    expect(use.onPressed, isNull);
    expect(tester.takeException(), isNull);
  });

  testWidgets('unavailable feature is distinct', (tester) async {
    final binding = MemoryBinding('s-unav', 't-unav', 1);
    final script = MemoryScript(binding);
    for (final path in [
      '/v1/converse-memory/state',
      '/v1/converse-memory/catalog',
      '/v1/memory/state',
    ]) {
      script.behavior[path] = (_) async => http.Response('absent', 404);
    }
    final state = _memoryState(_memoryClient(script), binding);
    await _openMemory(tester, state);
    expect(find.textContaining('not available on this backend'), findsWidgets);
    expect(tester.takeException(), isNull);
  });

  testWidgets('owner access failure is distinct from offline', (tester) async {
    final binding = MemoryBinding('s-denied', 't-denied', 1);
    final script = MemoryScript(binding);
    for (final path in [
      '/v1/converse-memory/state',
      '/v1/converse-memory/catalog',
      '/v1/memory/state',
    ]) {
      script.behavior[path] = (_) async => http.Response('denied', 403);
    }
    final state = _memoryState(_memoryClient(script), binding);
    await _openMemory(tester, state);
    expect(find.textContaining('Owner access was refused'), findsWidgets);
    expect(tester.takeException(), isNull);
  });

  testWidgets('signed-out owner sends no requests', (tester) async {
    final binding = MemoryBinding('s-signedout', 't-signedout', 1);
    final script = MemoryScript(binding)..seedTwoActive();
    final state = _memoryState(_memoryClient(script), binding);
    state.sessionAuthenticated = false;
    await _openMemory(tester, state);
    expect(script.paths, isEmpty);
    expect(find.textContaining('Owner access is needed'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('inference flags do not gate owner controls', (tester) async {
    final binding = MemoryBinding('s-flags', 't-flags', 1);
    final script = MemoryScript(binding)..seedTwoActive();
    final state = _memoryState(_memoryClient(script), binding);
    state.converseInferenceAllowed = false;
    state.agentInferenceAllowed = false;
    await _openMemory(tester, state);
    expect(find.text('first synthetic note'), findsOneWidget);
    expect(find.textContaining('Saved access: On'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('offline and timeout errors stay distinct', (tester) async {
    final binding = MemoryBinding('s-offline', 't-offline', 1);
    final script = MemoryScript(binding);
    script.behavior['/v1/converse-memory/state'] = (_) async {
      throw const SocketException.closed();
    };
    script.behavior['/v1/converse-memory/catalog'] = (_) async {
      throw TimeoutException('slow');
    };
    script.behavior['/v1/memory/state'] = (_) async {
      throw const SocketException.closed();
    };
    final state = _memoryState(_memoryClient(script), binding);
    await _openMemory(tester, state);
    expect(find.textContaining('timed out'), findsOneWidget);
    expect(find.textContaining('Check the connection'), findsWidgets);
    expect(tester.takeException(), isNull);
  });

  testWidgets('missing session sends no requests and explains why', (
    tester,
  ) async {
    final script = MemoryScript(MemoryBinding('s-nosess', 't-nosess', 1));
    final state = AppState()
      ..connect(_profile(), injected: _memoryClient(script));
    addTearDown(state.dispose);
    await _openMemory(tester, state);
    expect(script.paths, isEmpty);
    expect(find.textContaining('no current Converse session'), findsOneWidget);
    expect(find.text('Saved notes'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('missing owner connection sends no requests', (tester) async {
    final script = MemoryScript(MemoryBinding('s-noconn', 't-noconn', 1));
    final state = AppState()
      ..connect(_profile(), injected: _memoryClient(script));
    addTearDown(state.dispose);
    state.disconnect();
    state.converseSession = SessionInfo(
      sessionId: 's-noconn',
      threadId: 't-noconn',
      channel: 'converse',
      epoch: 1,
    );
    await _openMemory(tester, state);
    expect(script.paths, isEmpty);
    expect(find.textContaining('no owner connection'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('selection caps at eight; inactive notes stay untickable', (
    tester,
  ) async {
    final binding = MemoryBinding('s-cap', 't-cap', 1);
    final script = MemoryScript(binding);
    script.entries = [
      for (var i = 1; i <= 9; i++)
        _summaryJson(id: 'a-$i', ordinal: i, preview: 'active note $i'),
      _summaryJson(
        id: 'x-taint',
        ordinal: 10,
        state: 'tainted',
        preview: 'tainted note',
      ),
      _summaryJson(id: 'x-forgot', ordinal: 11, state: 'forgotten'),
    ];
    final state = _memoryState(_memoryClient(script), binding);
    await _openMemory(tester, state);
    for (var i = 1; i <= 8; i++) {
      await tester.scrollUntilVisible(
        find.byKey(ValueKey('memory-select-a-$i')),
        80,
        scrollable: find.byType(Scrollable).first,
      );
      await _tapScroll(tester, find.byKey(ValueKey('memory-select-a-$i')));
    }
    expect(find.text('8 of 8 notes selected.'), findsOneWidget);
    await tester.scrollUntilVisible(
      find.byKey(const ValueKey('memory-select-a-9')),
      80,
      scrollable: find.byType(Scrollable).first,
    );
    final ninth = tester.widget<CheckboxListTile>(
      find.byKey(const ValueKey('memory-select-a-9')),
    );
    expect(ninth.onChanged, isNull);
    expect(find.byKey(const ValueKey('memory-select-x-taint')), findsNothing);
    expect(find.byKey(const ValueKey('memory-select-x-forgot')), findsNothing);
    expect(
      find.text('Only active notes can be ticked for use.'),
      findsNWidgets(2),
    );
    expect(tester.takeException(), isNull);
  });

  testWidgets('inspect shows selectable detail and fences late responses', (
    tester,
  ) async {
    final binding = MemoryBinding('s-inspect', 't-inspect', 1);
    final script = MemoryScript(binding)..seedTwoActive();
    final held = Completer<http.Response>();
    var inspectCalls = 0;
    script.behavior['/v1/converse-memory/inspect'] = (body) async {
      inspectCalls += 1;
      if (inspectCalls == 1) return held.future;
      return _ok({
        'schema_version': '1.0',
        'state': _nsJson(binding, count: 2),
        'node': _nodeJson(
          binding,
          id: body['node_id']! as String,
          ordinal: 2,
          body: 'fresh literal body',
        ),
      });
    };
    final state = _memoryState(_memoryClient(script), binding);
    await _openMemory(tester, state);
    await _tapScroll(tester, find.byKey(const ValueKey('memory-inspect-n-1')));
    expect(find.text('Loading note detail…'), findsOneWidget);
    await _tapScroll(tester, find.byKey(const ValueKey('memory-inspect-n-2')));
    expect(find.textContaining('fresh literal body'), findsOneWidget);
    expect(find.byType(SelectableText), findsWidgets);
    expect(find.textContaining('Kind: assertion'), findsWidgets);
    // The superseded first inspection must not own the detail pane.
    held.complete(
      _ok({
        'schema_version': '1.0',
        'state': _nsJson(binding, count: 2),
        'node': _nodeJson(binding, id: 'n-1', ordinal: 1, body: 'stale body'),
      }),
    );
    await _settle(tester);
    expect(find.textContaining('stale body'), findsNothing);
    expect(find.textContaining('fresh literal body'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('explicit use sends observed runtime identity and selection', (
    tester,
  ) async {
    final binding = MemoryBinding('s-use', 't-use', 1);
    final script = MemoryScript(binding)..seedTwoActive();
    final state = _memoryState(_memoryClient(script), binding);
    await _openMemory(tester, state);
    await _tapScroll(tester, find.byKey(const ValueKey('memory-select-n-2')));
    await _tapScroll(tester, find.byKey(const ValueKey('memory-select-n-1')));
    await _tapScroll(tester, find.byKey(const ValueKey('converse-memory-use')));
    final useCall = script.payloads.lastWhere(
      (p) => p['expected_runtime_id'] != null,
    );
    expect(useCall['expected_runtime_id'], 'rt-1');
    expect(useCall['expected_generation'], 5);
    expect(useCall['enabled'], true);
    expect(useCall['implementation_id'], 'korina-dag-v1');
    expect(useCall['selector_ids']! as List, ['n-1', 'n-2']);
    expect(find.textContaining('Use: On'), findsOneWidget);
    expect(find.textContaining('Using 2 notes'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('stop needs no selection and survives unavailable catalog', (
    tester,
  ) async {
    final binding = MemoryBinding('s-stop', 't-stop', 1);
    final script = MemoryScript(binding)
      ..seedTwoActive()
      ..useEnabled = true
      ..selectors = ['n-1']
      ..useNotice = 'ready';
    script.behavior['/v1/converse-memory/catalog'] = (_) async =>
        http.Response('broken', 500);
    final state = _memoryState(_memoryClient(script), binding);
    await _openMemory(tester, state);
    expect(find.textContaining('Stopping use can still work'), findsOneWidget);
    final stop = tester.widget<OutlinedButton>(
      find.byKey(const ValueKey('converse-memory-stop')),
    );
    expect(stop.onPressed, isNotNull);
    await _tapScroll(
      tester,
      find.byKey(const ValueKey('converse-memory-stop')),
    );
    final stopCall = script.payloads.lastWhere(
      (p) => p.containsKey('expected_runtime_id'),
    );
    expect(stopCall['enabled'], false);
    expect(stopCall.containsKey('selector_ids'), isFalse);
    expect(stopCall['expected_runtime_id'], 'rt-1');
    expect(find.textContaining('Use: Off'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('CAS conflict clears snapshot and requires manual refresh', (
    tester,
  ) async {
    final binding = MemoryBinding('s-cas', 't-cas', 1);
    final script = MemoryScript(binding)
      ..seedTwoActive()
      ..useEnabled = true
      ..selectors = ['n-1']
      ..useNotice = 'ready';
    var refusals = 0;
    script.behavior['/v1/memory/use'] = (_) async {
      refusals += 1;
      if (refusals == 1) return http.Response('conflict', 409);
      return _ok(_useJson(binding, runtime: 'rt-1', generation: 6));
    };
    final state = _memoryState(_memoryClient(script), binding);
    await _openMemory(tester, state);
    await _tapScroll(
      tester,
      find.byKey(const ValueKey('converse-memory-stop')),
    );
    expect(find.text('Current use is unknown.'), findsOneWidget);
    expect(find.textContaining('Another change landed first'), findsWidgets);
    final stop = tester.widget<OutlinedButton>(
      find.byKey(const ValueKey('converse-memory-stop')),
    );
    expect(stop.onPressed, isNull);
    final useCalls = script.payloads
        .where((p) => p.containsKey('expected_runtime_id'))
        .length;
    expect(useCalls, 1);
    await _tapScroll(
      tester,
      find.byKey(const ValueKey('converse-memory-refresh')),
    );
    expect(find.textContaining('Use: Off'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    'uncertain write retains notice across equal refresh; advance settles',
    (tester) async {
      final binding = MemoryBinding('s-uncertain', 't-uncertain', 1);
      final script = MemoryScript(binding)..seedTwoActive();
      var attempts = 0;
      script.behavior['/v1/memory/use'] = (_) async {
        attempts += 1;
        if (attempts == 1) throw TimeoutException('slow write');
        return _ok(
          _useJson(binding, runtime: 'rt-1', generation: 6, enabled: false),
        );
      };
      final state = _memoryState(_memoryClient(script), binding);
      await _openMemory(tester, state);
      await _tapScroll(tester, find.byKey(const ValueKey('memory-select-n-1')));
      await _tapScroll(
        tester,
        find.byKey(const ValueKey('converse-memory-use')),
      );
      expect(find.textContaining('may still arrive'), findsWidgets);
      // Equal refresh never settles the latch and never resubmits.
      await _tapScroll(
        tester,
        find.byKey(const ValueKey('converse-memory-refresh')),
      );
      expect(attempts, 1);
      expect(script.paths.where((p) => p == '/v1/memory/use').length, 1);
      final use = tester.widget<FilledButton>(
        find.byKey(const ValueKey('converse-memory-use')),
      );
      expect(use.onPressed, isNull);
      // Explicit Stop may still run while runtime use is uncertain.
      final stop = tester.widget<OutlinedButton>(
        find.byKey(const ValueKey('converse-memory-stop')),
      );
      expect(stop.onPressed, isNotNull);
      // Closing and reopening on the same host cannot erase the latch.
      await tester.tap(find.byKey(const ValueKey('converse-memory-close')));
      await _settle(tester);
      await _openMemory(tester, state);
      await _tapScroll(tester, find.byKey(const ValueKey('memory-select-n-1')));
      final reopened = tester.widget<FilledButton>(
        find.byKey(const ValueKey('converse-memory-use')),
      );
      expect(reopened.onPressed, isNull);
      expect(find.textContaining('may still arrive'), findsWidgets);
      await tester.tap(find.byKey(const ValueKey('converse-memory-close')));
      await _settle(tester);
      // A strictly advanced generation settles the latch via refresh.
      script.generation = 6;
      await _openMemory(tester, state);
      await _tapScroll(tester, find.byKey(const ValueKey('memory-select-n-1')));
      final settled = tester.widget<FilledButton>(
        find.byKey(const ValueKey('converse-memory-use')),
      );
      expect(settled.onPressed, isNotNull);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('equal-generation stop does not fence an uncertain enable', (
    tester,
  ) async {
    final binding = MemoryBinding('s-nofence', 't-nofence', 1);
    final script = MemoryScript(binding)
      ..seedTwoActive()
      ..generation = 7;
    var attempts = 0;
    script.behavior['/v1/memory/use'] = (body) async {
      attempts += 1;
      if (attempts == 1) throw TimeoutException('slow enable');
      // No-op off at the same generation: must not clear the latch.
      return _ok(_useJson(binding, runtime: 'rt-1', generation: 7));
    };
    final state = _memoryState(_memoryClient(script), binding);
    await _openMemory(tester, state);
    await _tapScroll(tester, find.byKey(const ValueKey('memory-select-n-1')));
    await _tapScroll(tester, find.byKey(const ValueKey('converse-memory-use')));
    expect(find.textContaining('may still arrive'), findsWidgets);
    await _tapScroll(
      tester,
      find.byKey(const ValueKey('converse-memory-stop')),
    );
    expect(attempts, 2);
    expect(find.textContaining('may still arrive'), findsWidgets);
    final use = tester.widget<FilledButton>(
      find.byKey(const ValueKey('converse-memory-use')),
    );
    expect(use.onPressed, isNull);
    expect(tester.takeException(), isNull);
  });

  testWidgets('namespace no-op off cannot fence a delayed enable', (
    tester,
  ) async {
    final binding = MemoryBinding('s-nsnoop', 't-nsnoop', 1);
    final script = MemoryScript(binding)..seedTwoActive();
    script.behavior['/v1/converse-memory/configure'] = (_) async {
      throw TimeoutException('slow configure');
    };
    final state = _memoryState(_memoryClient(script), binding);
    await _openMemory(tester, state);
    await tester.tap(
      find.byKey(const ValueKey('converse-memory-namespace-off')),
    );
    await _settle(tester);
    expect(find.textContaining('may still arrive'), findsWidgets);
    final on = tester.widget<FilledButton>(
      find.byKey(const ValueKey('converse-memory-namespace-on')),
    );
    final off = tester.widget<OutlinedButton>(
      find.byKey(const ValueKey('converse-memory-namespace-off')),
    );
    expect(on.onPressed, isNull);
    expect(off.onPressed, isNull);
    expect(script.paths.where((p) => p.endsWith('/configure')).length, 1);
    expect(tester.takeException(), isNull);
  });

  testWidgets('namespace and runtime scopes stay independent', (tester) async {
    final binding = MemoryBinding('s-indep', 't-indep', 1);
    final script = MemoryScript(binding)
      ..seedTwoActive()
      ..useEnabled = true
      ..selectors = ['n-1']
      ..useNotice = 'ready';
    script.behavior['/v1/converse-memory/catalog'] = (_) async =>
        http.Response('broken', 500);
    script.behavior['/v1/converse-memory/configure'] = (_) async =>
        http.Response('broken', 500);
    final state = _memoryState(_memoryClient(script), binding);
    await _openMemory(tester, state);
    await tester.tap(
      find.byKey(const ValueKey('converse-memory-namespace-off')),
    );
    await _settle(tester);
    expect(find.textContaining('refused the request'), findsWidgets);
    // The confirmed namespace failure never hides the runtime snapshot.
    expect(find.textContaining('Use: On'), findsOneWidget);
    final stop = tester.widget<OutlinedButton>(
      find.byKey(const ValueKey('converse-memory-stop')),
    );
    expect(stop.onPressed, isNotNull);
    expect(tester.takeException(), isNull);
  });

  testWidgets('held reads drop after reconnect and session change', (
    tester,
  ) async {
    final binding = MemoryBinding('s-stale', 't-stale', 1);
    final script = MemoryScript(binding)..seedTwoActive();
    final stateGate = Completer<http.Response>();
    script.behavior['/v1/converse-memory/state'] = (_) => stateGate.future;
    final state = _memoryState(_memoryClient(script), binding);
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: Builder(
            builder: (context) => TextButton(
              key: const ValueKey('open-memory'),
              onPressed: () =>
                  showConverseMemoryDialog(context: context, state: state),
              child: const Text('open'),
            ),
          ),
        ),
      ),
    );
    await tester.tap(find.byKey(const ValueKey('open-memory')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 50));
    // Same-backend reconnect bumps the generation with a new client.
    state.connect(_profile(), injected: _memoryClient(script));
    await tester.pump();
    expect(find.textContaining('changed. Choose Refresh'), findsOneWidget);
    stateGate.complete(_ok(_nsJson(binding, count: 2)));
    await _settle(tester);
    expect(find.textContaining('Saved access: On'), findsNothing);
    // Manual refresh adopts the live scope explicitly.
    script.behavior.remove('/v1/converse-memory/state');
    await _tapScroll(
      tester,
      find.byKey(const ValueKey('converse-memory-refresh')),
    );
    expect(find.textContaining('Saved access: On'), findsOneWidget);
    // A held session-scoped action drops after the epoch changes too.
    final useGate = Completer<http.Response>();
    script.behavior['/v1/memory/use'] = (_) => useGate.future;
    await _tapScroll(tester, find.byKey(const ValueKey('memory-select-n-1')));
    await _tapScroll(tester, find.byKey(const ValueKey('converse-memory-use')));
    state.converseSession = SessionInfo(
      sessionId: 's-stale',
      threadId: 't-stale',
      channel: 'converse',
      epoch: 2,
    );
    state.touch();
    await tester.pump();
    useGate.complete(
      _ok(_useJson(binding, runtime: 'rt-1', generation: 9, enabled: true)),
    );
    await _settle(tester);
    expect(find.textContaining('changed. Choose Refresh'), findsOneWidget);
    expect(find.textContaining('Use: On'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('runtime restart clears selection and restarts use off', (
    tester,
  ) async {
    final binding = MemoryBinding('s-restart', 't-restart', 1);
    final script = MemoryScript(binding)..seedTwoActive();
    final state = _memoryState(_memoryClient(script), binding);
    await _openMemory(tester, state);
    await _tapScroll(tester, find.byKey(const ValueKey('memory-select-n-1')));
    expect(find.text('1 of 8 notes selected.'), findsOneWidget);
    script.runtimeId = 'rt-2';
    script.generation = 0;
    script.useEnabled = false;
    await _tapScroll(
      tester,
      find.byKey(const ValueKey('converse-memory-refresh')),
    );
    expect(find.text('0 of 8 notes selected.'), findsOneWidget);
    expect(find.textContaining('restarted'), findsWidgets);
    expect(find.textContaining('rt-2'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('busy action sends once and settles after close', (tester) async {
    final binding = MemoryBinding('s-busy', 't-busy', 1);
    final script = MemoryScript(binding)..seedTwoActive();
    final useGate = Completer<http.Response>();
    script.behavior['/v1/memory/use'] = (_) => useGate.future;
    final state = _memoryState(_memoryClient(script), binding);
    await _openMemory(tester, state);
    await _tapScroll(tester, find.byKey(const ValueKey('memory-select-n-1')));
    await _tapScroll(tester, find.byKey(const ValueKey('converse-memory-use')));
    expect(find.text('Saving…'), findsWidgets);
    await tester.tap(find.byKey(const ValueKey('converse-memory-close')));
    await _settle(tester);
    useGate.complete(
      _ok(
        _useJson(
          binding,
          runtime: 'rt-1',
          generation: 6,
          enabled: true,
          selectors: const ['n-1'],
          notice: 'ready',
        ),
      ),
    );
    await _settle(tester);
    expect(script.paths.where((p) => p == '/v1/memory/use').length, 1);
    expect(tester.takeException(), isNull);
  });

  testWidgets('dialog fits 320x480 at 2x text with keyboard close', (
    tester,
  ) async {
    tester.view.devicePixelRatio = 1;
    tester.view.physicalSize = const Size(320, 480);
    tester.platformDispatcher.textScaleFactorTestValue = 2;
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
    final binding = MemoryBinding('s-small', 't-small', 1);
    final script = MemoryScript(binding)..seedTwoActive();
    final state = _memoryState(_memoryClient(script), binding);
    await _openMemory(tester, state);
    expect(find.text('Converse memory'), findsOneWidget);
    for (final key in [
      'converse-memory-refresh',
      'converse-memory-close',
      'converse-memory-use',
      'converse-memory-stop',
      'converse-memory-namespace-on',
      'converse-memory-namespace-off',
    ]) {
      await tester.scrollUntilVisible(
        find.byKey(ValueKey(key)),
        60,
        scrollable: find.byType(Scrollable).first,
      );
    }
    expect(tester.takeException(), isNull);
    await tester.sendKeyEvent(LogicalKeyboardKey.escape);
    await tester.pump();
    expect(find.text('Converse memory'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('pending service state is shown truthfully', (tester) async {
    final binding = MemoryBinding('s-pending', 't-pending', 1);
    final script = MemoryScript(binding)
      ..seedTwoActive()
      ..pending = true
      ..useNotice = 'ready';
    final state = _memoryState(_memoryClient(script), binding);
    await _openMemory(tester, state);
    expect(find.textContaining('Still applying'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });
}
