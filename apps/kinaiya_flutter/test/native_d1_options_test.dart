import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/decisions_model_client.dart';
import 'package:kinaiya_flutter/api/provider_client.dart';
import 'package:kinaiya_flutter/api/provider_settings.dart';
import 'package:kinaiya_flutter/ui/options_decisions_model.dart';
import 'package:kinaiya_flutter/ui/options_provider_editor.dart';
import 'package:kinaiya_flutter/ui/widgets.dart';

import 'native_d1_settings_test.dart'
    show nativeConfig, nativeProfile, decisionsSnapshot;

http.Response reply(Object data) => http.Response(
  jsonEncode(data),
  200,
  headers: {'content-type': 'application/json', 'cache-control': 'no-store'},
);

Map<String, Object?> catalog(List<Object?> profiles) => {
  'schema_version': '1.0',
  'revision': 0,
  'change_boundary': 'role_owner',
  'credential_reference_boundary': 'explicit_registration_snapshot',
  'profiles': profiles,
  'credentials': [],
  'known_references': [],
  'limits': {
    'profiles': 32,
    'profile_generations': 64,
    'credentials': 32,
    'credential_generations': 32,
    'receipts': 10000,
  },
};

Future<void> showOptions(WidgetTester tester, Widget child) async {
  tester.view.physicalSize = const Size(1400, 1800);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(body: SingleChildScrollView(child: child)),
    ),
  );
  await tester.pumpAndSettle();
}

Finder field(String label) => find.byWidgetPredicate(
  (w) => w is TextField && w.decoration?.labelText == label,
);

Future<void> enter(WidgetTester tester, String label, String value) async {
  await tester.ensureVisible(field(label));
  await tester.enterText(field(label), value);
}

void main() {
  testWidgets(
    'custom model shortcut opens a new provider profile after catalog inspection',
    (tester) async {
      var writes = 0, opened = 0;
      final api = ProviderApi(
        baseUrl: 'http://127.0.0.1:8761',
        ownerToken: 'synthetic-custom-profile',
        clientFactory: () => MockClient((request) async {
          if (request.method != 'GET') writes++;
          return reply(catalog([]));
        }),
      );
      addTearDown(api.close);
      await showOptions(
        tester,
        OptionsProviderEditor(
          api: api,
          active: () => true,
          startNewProfile: true,
          onNewProfileStarted: () => opened++,
        ),
      );
      expect(find.text('Add provider profile'), findsOneWidget);
      expect(field('Profile ID'), findsOneWidget);
      expect(opened, 1);
      expect(writes, 0);
      await tester.pumpWidget(const SizedBox.shrink());
    },
  );
  testWidgets(
    'native declaration needs an explicit confidence and saves no inference knobs',
    (tester) async {
      final requests = <http.Request>[];
      Map<String, Object?>? saved;
      final api = ProviderApi(
        baseUrl: 'http://127.0.0.1:8761',
        ownerToken: 'synthetic-native-editor',
        clientFactory: () => MockClient((request) async {
          requests.add(request);
          if (request.method == 'GET') {
            return reply(catalog(saved == null ? [] : [nativeProfile(saved!)]));
          }
          final body = jsonDecode(request.body) as Map<String, Object?>;
          saved = body['config'] as Map<String, Object?>;
          return reply({
            'schema_version': '1.0',
            'status': 'catalog_saved',
            'revision': 1,
            'operation': body['operation'],
            'idempotency_key': body['idempotency_key'],
            'change_boundary': 'role_owner',
            'result': {
              'id': body['profile_id'],
              'generation': 1,
              'removed': false,
              'configuration_sha256': providerConfigurationDigest(saved!),
            },
          });
        }),
      );
      addTearDown(api.close);
      await showOptions(
        tester,
        OptionsProviderEditor(api: api, active: () => true),
      );
      await tester.tap(find.text('Add profile'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('OpenAI-compatible endpoint'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('D1 · native local Decisions').last);
      await tester.pumpAndSettle();
      expect(
        tester
            .widget<TextField>(field('Minimum decision confidence'))
            .controller!
            .text,
        isEmpty,
      );
      expect(field('Maximum output tokens'), findsNothing);
      expect(find.text('Configured reasoning effort'), findsNothing);
      expect(
        find.text('Authorize this profile to use a cloud endpoint'),
        findsNothing,
      );
      expect(field('Token window · optional'), findsNothing);
      expect(find.text('No credential'), findsOneWidget);
      await enter(tester, 'Profile ID', 'native-d1');
      await enter(tester, 'Display name', 'Local d1');
      await enter(tester, 'Endpoint URL', 'http://127.0.0.1:8816');
      await enter(
        tester,
        'Exact model ID or native loaded alias',
        'LiquidAI/d1-Omni-2B',
      );
      await tester.ensureVisible(find.text('Save profile'));
      await tester.tap(find.text('Save profile'));
      await tester.pumpAndSettle();
      expect(find.text('Enter a number from 0 to 1.'), findsOneWidget);
      expect(requests.where((r) => r.method == 'POST'), isEmpty);
      await enter(tester, 'Minimum decision confidence', '0.67');
      await tester.ensureVisible(find.text('Save profile'));
      await tester.tap(find.text('Save profile'));
      await tester.pumpAndSettle();
      expect(saved, {
        ...nativeConfig(),
        'allow_cloud': false,
        'credential': null,
        'timeout_s': 5,
      });
      expect(requests.where((r) => r.method == 'POST').length, 1);
      expect(
        requests.every(
          (r) =>
              {'/v1/providers', '/v1/providers/changes'}.contains(r.url.path),
        ),
        true,
      );
      expect(
        find.textContaining('Active models have not changed'),
        findsOneWidget,
      );
    },
  );

  testWidgets(
    'editing native preserves the selected floor; switching to Laya keeps its controls',
    (tester) async {
      final config = providerConfig({
        ...nativeConfig(),
        'min_confidence': 0.42,
      });
      final api = ProviderApi(
        baseUrl: 'http://127.0.0.1:8761',
        ownerToken: 'synthetic-native-edit',
        clientFactory: () =>
            MockClient((_) async => reply(catalog([nativeProfile(config)]))),
      );
      addTearDown(api.close);
      await showOptions(
        tester,
        OptionsProviderEditor(api: api, active: () => true),
      );
      await tester.tap(find.text('Edit'));
      await tester.pumpAndSettle();
      expect(
        tester
            .widget<TextField>(field('Minimum decision confidence'))
            .controller!
            .text,
        '0.42',
      );
      await tester.tap(find.text('D1 · native local Decisions'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Laya · typed Decisions').last);
      await tester.pumpAndSettle();
      expect(field('Minimum decision confidence'), findsNothing);
      expect(field('Token window · optional'), findsOneWidget);
      expect(field('Expected artifact SHA-256 · optional'), findsOneWidget);
      expect(
        find.text('Authorize this profile to use a cloud endpoint'),
        findsOneWidget,
      );
    },
  );

  testWidgets(
    'native Decisions selection saves the exact generation without probing a model',
    (tester) async {
      final requests = <http.Request>[];
      Map<String, Object?>? saved;
      final api = DecisionsModelsApi(
        baseUrl: 'http://127.0.0.1:8763',
        ownerToken: 'synthetic-native-selection',
        clientFactory: () => MockClient((request) async {
          requests.add(request);
          if (request.method == 'GET') return reply(decisionsSnapshot());
          final body = saved = jsonDecode(request.body) as Map<String, Object?>;
          return reply({
            'schema_version': '1.0',
            'status': 'model_selection_saved',
            'role': 'decisions',
            'backend_id': body['expected_backend'],
            'instance_id': body['expected_instance'],
            'idempotency_key': body['idempotency_key'],
            'expected_revision': body['expected_revision'],
            'expected_state': body['expected_state'],
            'selection': body['selection'],
            'revision': 1,
            'change_boundary': 'settled_decisions_operations',
          });
        }),
      );
      addTearDown(api.close);
      await showOptions(
        tester,
        OptionsDecisionsModel(
          api: api,
          active: () => true,
          onChanged: () {},
          onInvalidate: () {},
        ),
      );
      expect(
        find.textContaining('Native d1 supports text only'),
        findsOneWidget,
      );
      await tester.tap(
        find.text('Host configuration · legacy compatibility').last,
      );
      await tester.pumpAndSettle();
      await tester.tap(
        find.text('Local d1 · generation 1 · native d1 · text Decisions').last,
      );
      await tester.pumpAndSettle();
      await tester.tap(find.text('Save classifier selection'));
      await tester.pumpAndSettle();
      expect(saved?['selection'], {
        'source': 'catalog',
        'catalog_binding': {
          'id': 'native_d1',
          'generation': 1,
          'configuration_sha256': 'd' * 64,
        },
      });
      expect(requests.where((r) => r.method == 'POST').length, 1);
      expect(
        requests.every(
          (r) => r.url.path.startsWith('/v1/decisions/options/model'),
        ),
        true,
      );
      expect(
        tester
            .widget<ActionOutcomeFeedback>(
              find.byKey(const ValueKey('decisions-save-outcome')),
            )
            .state,
        ActionOutcomeState.confirmed,
      );
      expect(find.text('Confirmed'), findsOneWidget);
    },
  );

  testWidgets(
    'Decisions save feedback distinguishes in-progress, unconfirmed receipt, retry confirmation, and guards repeated clicks',
    (tester) async {
      final postCompleters = <Completer<http.Response>>[];
      final postRequests = <http.Request>[];
      Map<String, Object?>? lastBody;
      final api = DecisionsModelsApi(
        baseUrl: 'http://127.0.0.1:8763',
        ownerToken: 'synthetic-decisions-feedback',
        clientFactory: () => MockClient((request) async {
          if (request.method == 'GET') return reply(decisionsSnapshot());
          if (request.url.path.endsWith('/lookup')) {
            final reqBody = jsonDecode(request.body) as Map<String, Object?>;
            return reply({
              'schema_version': '1.0',
              'backend_id': 'a' * 32,
              'request': reqBody,
              'status': 'unobserved',
              'receipt': null,
              'mutation_fenced': false,
            });
          }
          postRequests.add(request);
          lastBody = jsonDecode(request.body) as Map<String, Object?>;
          final completer = Completer<http.Response>();
          postCompleters.add(completer);
          return completer.future;
        }),
      );
      addTearDown(api.close);
      await showOptions(
        tester,
        OptionsDecisionsModel(
          api: api,
          active: () => true,
          onChanged: () {},
          onInvalidate: () {},
        ),
      );
      await tester.tap(
        find.text('Host configuration · legacy compatibility').last,
      );
      await tester.pumpAndSettle();
      await tester.tap(
        find.text('Local d1 · generation 1 · native d1 · text Decisions').last,
      );
      await tester.pumpAndSettle();

      // Clicking Save enters inProgress immediately and never optimistically claims Confirmed.
      await tester.tap(find.text('Save classifier selection'));
      await tester.pump();
      expect(postRequests.length, 1);
      expect(
        tester
            .widget<ActionOutcomeFeedback>(
              find.byKey(const ValueKey('decisions-save-outcome')),
            )
            .state,
        ActionOutcomeState.inProgress,
      );
      expect(find.text('In progress'), findsOneWidget);
      expect(find.text('Confirmed'), findsNothing);

      // Repeated click while in-flight is ignored.
      await tester.tap(
        find.text('Save classifier selection'),
        warnIfMissed: false,
      );
      await tester.pump();
      expect(postRequests.length, 1);

      // Transport/HTTP failure after dispatch transitions to unconfirmed receipt, never Confirmed.
      postCompleters.first.complete(http.Response('gateway timeout', 504));
      await tester.pumpAndSettle();
      expect(
        tester
            .widget<ActionOutcomeFeedback>(
              find.byKey(const ValueKey('decisions-save-outcome')),
            )
            .state,
        ActionOutcomeState.unconfirmed,
      );
      expect(find.text('Outcome unconfirmed'), findsOneWidget);
      expect(find.text('Confirmed'), findsNothing);

      // Explicit retry with native confirmation transitions to Confirmed with exact idempotency key preserved.
      final originalKey = lastBody!['idempotency_key'];
      await tester.tap(find.text('Retry exact save'));
      await tester.pump();
      expect(postRequests.length, 2);
      expect(lastBody!['idempotency_key'], originalKey);
      postCompleters.last.complete(
        reply({
          'schema_version': '1.0',
          'status': 'model_selection_saved',
          'role': 'decisions',
          'backend_id': lastBody!['expected_backend'],
          'instance_id': lastBody!['expected_instance'],
          'idempotency_key': lastBody!['idempotency_key'],
          'expected_revision': lastBody!['expected_revision'],
          'expected_state': lastBody!['expected_state'],
          'selection': lastBody!['selection'],
          'revision': 1,
          'change_boundary': 'settled_decisions_operations',
        }),
      );
      await tester.pumpAndSettle();
      expect(
        tester
            .widget<ActionOutcomeFeedback>(
              find.byKey(const ValueKey('decisions-save-outcome')),
            )
            .state,
        ActionOutcomeState.confirmed,
      );
      expect(find.text('Confirmed'), findsOneWidget);
    },
  );
}
