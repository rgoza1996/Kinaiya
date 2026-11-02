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
    },
  );
}
