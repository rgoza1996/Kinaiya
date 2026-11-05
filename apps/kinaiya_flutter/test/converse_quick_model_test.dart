import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/converse_model_client.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/model_setup.dart';
import 'package:kinaiya_flutter/ui/options_converse_model.dart';

const _token =
    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';

Map<String, Object?> provider(String name, String model) => {
  'schema_version': '1.0',
  'provider': name,
  'model': model,
  'local': true,
  'streaming': true,
};

Map<String, Object?> profile(
  String id,
  String label,
  String owner, {
  bool selectable = true,
}) => {
  'id': id,
  'label': label,
  'catalog_binding': {
    'id': id,
    'generation': 1,
    'configuration_sha256': _token,
  },
  'selectable': selectable,
  'provider': provider(owner, '$id-model'),
  'reasoning': 'medium',
  'reasoning_options': ['low', 'medium', 'high'],
  'reason': selectable ? 'Available in the catalog' : 'Profile unavailable',
  'qualification': 'not_checked',
  'readiness': 'unknown',
};

Map<String, Object?> snapshot({
  Map<String, Object?>? selection,
  int revision = 0,
  bool busy = false,
}) => {
  'schema_version': '1.0',
  'instance_id': '01234567-89ab-4cde-8abc-0123456789ab',
  'revision': revision,
  'state_token': _token,
  'selection':
      selection ??
      {'source': 'host', 'catalog_binding': null, 'reasoning': null},
  'selected_info': provider('local', 'host-model'),
  'selected_reasoning': null,
  'selected_error': null,
  'host': {
    'provider': provider('local', 'host-model'),
    'reasoning': null,
    'selectable': true,
  },
  'catalog_profiles': [
    profile('alpha', 'Alpha', 'Ollama'),
    profile('beta', 'Beta', 'Other', selectable: false),
  ],
  'catalog_error': null,
  'busy': busy,
  'operation_state': {
    for (final name in [
      'preparing',
      'text',
      'text_admitting',
      'windows',
      'speech',
      'stt',
      'stt_cleanup',
      'native_audio',
      'playback_pending',
      'memory_pending',
      'leases',
      'cleanup_pending',
      'cleanup_failed',
    ])
      name: 0,
  },
  'change_boundary': 'settled_converse_operations',
  'qualification': 'not_checked',
  'readiness': 'unknown',
  'limits': {'leases': 64, 'receipts': 10000},
};

http.Response jsonReply(Object body) => http.Response(
  jsonEncode(body),
  200,
  headers: {'content-type': 'application/json'},
);

Map<String, Object?> receipt(Map<String, Object?> body) => {
  'schema_version': '1.0',
  'status': 'model_selection_saved',
  'role': 'converse',
  'idempotency_key': body['idempotency_key'],
  'expected_revision': body['expected_revision'],
  'expected_state': body['expected_state'],
  'revision': (body['expected_revision'] as int) + 1,
  'selection': body['selection'],
  'change_boundary': 'settled_converse_operations',
};

Widget quick(ConverseModelsApi api) => MaterialApp(
  home: Scaffold(
    body: Center(
      child: SizedBox(
        width: 500,
        height: 850,
        child: OptionsConverseModel(
          api: api,
          active: () => true,
          compact: true,
          onInvalidate: () {},
          onSettled: () async {},
        ),
      ),
    ),
  ),
);

void main() {
  testWidgets('busy model owner never dispatches a selection', (tester) async {
    tester.view.physicalSize = const Size(1000, 1000);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    var writes = 0;
    final api = ConverseModelsApi(
      baseUrl: 'http://127.0.0.1:8764',
      ownerToken: 'quick-busy',
      clientFactory: () => MockClient((request) async {
        if (request.method != 'GET') writes++;
        return jsonReply(snapshot(busy: true));
      }),
    );
    addTearDown(api.close);
    await tester.pumpWidget(quick(api));
    await tester.pumpAndSettle();
    expect(
      find.textContaining('while Converse operations are active'),
      findsOneWidget,
    );
    expect(
      tester
          .widget<ListTile>(
            find.byKey(const ValueKey(('quick-model', 'alpha:1:$_token'))),
          )
          .onTap,
      isNull,
    );
    expect(writes, 0);
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('picker fits a small screen with enlarged text', (tester) async {
    tester.view.physicalSize = const Size(320, 568);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    final state = AppState();
    await tester.pumpWidget(
      MaterialApp(
        builder: (context, child) => MediaQuery(
          data: MediaQuery.of(context)
              .copyWith(textScaler: const TextScaler.linear(2)),
          child: child!,
        ),
        home: Scaffold(
          body: SingleChildScrollView(
            child: ConverseModelSummary(
              state: state,
              onOpenModelOptions: () {},
              onAddModel: () {},
            ),
          ),
        ),
      ),
    );
    await tester.ensureVisible(
      find.byKey(const ValueKey('converse-model-summary-settings')),
    );
    await tester.tap(
      find.byKey(const ValueKey('converse-model-summary-settings')),
    );
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
    await tester.ensureVisible(find.text('Edit models…'));
    await tester.pumpAndSettle();
    expect(find.text('Edit models…').hitTestable(), findsOneWidget);
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
  });

  testWidgets('catalog rows fit a narrow picker with enlarged text', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(320, 568);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    final api = ConverseModelsApi(
      baseUrl: 'http://127.0.0.1:8765',
      ownerToken: 'quick-narrow',
      clientFactory: () => MockClient((_) async => jsonReply(snapshot())),
    );
    addTearDown(api.close);
    await tester.pumpWidget(
      MaterialApp(
        builder: (context, child) => MediaQuery(
          data: MediaQuery.of(context)
              .copyWith(textScaler: const TextScaler.linear(2)),
          child: child!,
        ),
        home: Scaffold(
          body: SizedBox(
            width: 288,
            height: 500,
            child: OptionsConverseModel(
              api: api,
              active: () => true,
              compact: true,
              onInvalidate: () {},
              onSettled: () async {},
            ),
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();
    await tester.ensureVisible(find.text('Alpha'));
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
    expect(find.text('Alpha').hitTestable(), findsOneWidget);
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets(
    'searches grouped catalog, exposes details, and saves a real selectable choice',
    (tester) async {
      tester.view.physicalSize = const Size(1000, 1000);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      Map<String, Object?>? request;
      var reads = 0;
      final api = ConverseModelsApi(
        baseUrl: 'http://127.0.0.1:8762',
        ownerToken: 'quick-verified',
        clientFactory: () => MockClient((r) async {
          if (r.method == 'GET') {
            reads++;
            return jsonReply(
              snapshot(
                selection: request?['selection'] as Map<String, Object?>?,
                revision: request == null ? 0 : 1,
              ),
            );
          }
          request = jsonDecode(r.body) as Map<String, Object?>;
          return jsonReply(receipt(request!));
        }),
      );
      addTearDown(api.close);
      await tester.pumpWidget(quick(api));
      await tester.pumpAndSettle();
      expect(find.text('Ollama'), findsOneWidget);
      expect(find.text('Other'), findsOneWidget);
      await tester.ensureVisible(find.text('Refresh models'));
      await tester.tap(find.text('Refresh models'));
      await tester.pumpAndSettle();
      expect(reads, 2);
      expect(
        tester
            .widget<ListTile>(
              find.byKey(const ValueKey(('quick-model', 'beta:1:$_token'))),
            )
            .onTap,
        isNull,
      );
      await tester.ensureVisible(
        find.byKey(const ValueKey('quick-model-search')),
      );
      await tester.enterText(
        find.byKey(const ValueKey('quick-model-search')),
        'ollama',
      );
      await tester.pump();
      expect(find.text('Alpha'), findsOneWidget);
      expect(find.text('Beta'), findsNothing);
      expect(find.text('Reasoning MEDIUM'), findsOneWidget);
      await tester.tap(find.byTooltip('Show details for Alpha'));
      await tester.pump();
      expect(
        find.textContaining('Available reasoning options: low, medium, high'),
        findsOneWidget,
      );
      await tester.tap(find.byTooltip('Favorite Alpha for this session'));
      await tester.pump();
      expect(
        find.byTooltip('Remove Alpha from session favorites'),
        findsOneWidget,
      );
      expect(request, isNull);
      await tester.tap(find.byKey(const ValueKey('quick-model-search')));
      await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
      await tester.pump();
      await tester.sendKeyEvent(LogicalKeyboardKey.enter);
      await tester.pumpAndSettle();
      expect(request?['expected_revision'], 0);
      expect((request?['selection'] as Map)['catalog_binding'], {
        'id': 'alpha',
        'generation': 1,
        'configuration_sha256': _token,
      });
      expect(
        find.textContaining('Model readiness has not been checked.'),
        findsOneWidget,
      );
      await tester.pumpWidget(const SizedBox.shrink());
    },
  );

  testWidgets(
    'uncertain save is retained across closing and recovered without another write',
    (tester) async {
      tester.view.physicalSize = const Size(1000, 1000);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      Map<String, Object?>? request;
      var writes = 0;
      var recovered = false;
      final api = ConverseModelsApi(
        baseUrl: 'http://127.0.0.1:8763',
        ownerToken: 'quick-recovery',
        clientFactory: () => MockClient((r) async {
          if (r.method == 'POST') {
            writes++;
            request = jsonDecode(r.body) as Map<String, Object?>;
            return http.Response('unconfirmed', 503);
          }
          if (r.url.path.contains('model-changes')) {
            recovered = true;
            return jsonReply(receipt(request!));
          }
          return jsonReply(
            snapshot(
              selection: recovered
                  ? request!['selection'] as Map<String, Object?>
                  : null,
              revision: recovered ? 1 : 0,
            ),
          );
        }),
      );
      addTearDown(api.close);
      await tester.pumpWidget(quick(api));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Alpha'));
      await tester.pumpAndSettle();
      expect(writes, 1);
      expect(find.text('Check exact receipt'), findsOneWidget);
      await tester.pumpWidget(const SizedBox.shrink());
      await tester.pumpWidget(quick(api));
      await tester.pumpAndSettle();
      expect(find.text('Check exact receipt'), findsOneWidget);
      await tester.tap(find.text('Check exact receipt'));
      await tester.pumpAndSettle();
      expect(writes, 1);
      expect(api.hasUnconfirmedChange, isFalse);
      await tester.pumpWidget(const SizedBox.shrink());
    },
  );

  testWidgets(
    'picker dismisses with Escape and routes to existing model settings',
    (tester) async {
      tester.view.physicalSize = const Size(1000, 800);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final state = AppState();
      var edits = 0, adds = 0;
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: Center(
              child: ConverseModelSummary(
                state: state,
                onOpenModelOptions: () => edits++,
                onAddModel: () => adds++,
              ),
            ),
          ),
        ),
      );
      await tester.tap(
        find.byKey(const ValueKey('converse-model-summary-settings')),
      );
      await tester.pumpAndSettle();
      expect(
        find.text('Connect and authenticate to inspect Converse models.'),
        findsOneWidget,
      );
      await tester.sendKeyEvent(LogicalKeyboardKey.escape);
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey('quick-model-picker')), findsNothing);
      await tester.tap(
        find.byKey(const ValueKey('converse-model-summary-settings')),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.text('Add custom model…'));
      await tester.pumpAndSettle();
      expect(adds, 1);
      await tester.tap(
        find.byKey(const ValueKey('converse-model-summary-settings')),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.text('Edit models…'));
      await tester.pumpAndSettle();
      expect(edits, 1);
      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
    },
  );
}
