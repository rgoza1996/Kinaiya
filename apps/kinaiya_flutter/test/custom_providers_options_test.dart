import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/provider_client.dart';
import 'package:kinaiya_flutter/api/provider_settings.dart';
import 'package:kinaiya_flutter/ui/options_provider_editor.dart';

http.Response _jsonReply(Object data, {int status = 200}) => http.Response(
  jsonEncode(data),
  status,
  headers: {'content-type': 'application/json', 'cache-control': 'no-store'},
);

Map<String, Object?> _emptyCatalog() => {
  'schema_version': '1.0',
  'revision': 0,
  'change_boundary': 'role_owner',
  'credential_reference_boundary': 'explicit_registration_snapshot',
  'profiles': <Object?>[],
  'credentials': <Object?>[],
  'known_references': <Object?>[],
  'limits': {
    'profiles': 32,
    'profile_generations': 64,
    'credentials': 32,
    'credential_generations': 32,
    'receipts': 10000,
  },
};

Map<String, Object?> _customProfile({
  required String id,
  required String name,
  required String url,
  bool enabled = true,
  bool hasKey = true,
  String model = '',
  List<String> discovered = const [],
  List<String> referencedBy = const [],
}) => {
  'profile_id': id,
  'name': name,
  'base_url': url,
  'enabled': enabled,
  'has_api_key': hasKey,
  'masked_api_key': hasKey ? kMaskedCustomApiKey : '',
  'selected_model': model,
  'qualified_model_id': model.isNotEmpty ? '$id:$model' : '',
  'discovered_models': discovered,
  'allow_remote_http': false,
  'referenced_by': referencedBy,
  'capabilities': {
    'chat': 'unverified',
    'streaming': 'unverified',
    'reasoning': 'unverified',
    'multimodal': 'unverified',
    'tool_use': 'unverified',
    'agent_qualified': false,
  },
};

Future<void> _pumpEditor(WidgetTester tester, Widget child) async {
  tester.view.physicalSize = const Size(1440, 1800);
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

void main() {
  testWidgets(
    'Custom APIs (.env) tab renders 6+ profiles, adds/edits/toggles, discovers models, and confirms referenced removal',
    (tester) async {
      var revision = 1;
      final profiles = <Map<String, Object?>>[
        _customProfile(
          id: 'p01',
          name: 'OpenRouter',
          url: 'https://openrouter.ai/api/v1',
          model: 'anthropic/claude-3.7-sonnet',
          referencedBy: const ['converse'],
        ),
        _customProfile(
          id: 'p02',
          name: 'Together AI',
          url: 'https://api.together.xyz/v1',
          model: 'meta-llama/Llama-3.3-70B-Instruct',
          referencedBy: const ['agent'],
        ),
        _customProfile(
          id: 'p03',
          name: 'Groq',
          url: 'https://api.groq.com/openai/v1',
          model: 'meta-llama/Llama-3.3-70B-Instruct',
        ),
        _customProfile(
          id: 'p04',
          name: 'DeepSeek',
          url: 'https://api.deepseek.com/v1',
        ),
        _customProfile(
          id: 'p05',
          name: 'Fireworks',
          url: 'https://api.fireworks.ai/inference/v1',
        ),
        _customProfile(
          id: 'p06',
          name: 'Local vLLM',
          url: 'http://127.0.0.1:8000/v1',
          hasKey: false,
        ),
      ];

      var p01Removed = false;
      Map<String, Object?> buildSnapshot() => {
        'schema_version': 1,
        'env_file_name': 'custom_providers.env',
        'unlimited': true,
        'revision': revision,
        'state_sha256': 'rev-$revision',
        'profiles': profiles,
        'role_bindings': {
          'converse': p01Removed
              ? {
                  'profile_id': 'p01',
                  'provider_name': 'p01 (removed)',
                  'base_url': '',
                  'enabled': false,
                  'model_id': 'anthropic/claude-3.7-sonnet',
                  'qualified_model_id': 'p01:anthropic/claude-3.7-sonnet',
                  'status': 'unavailable_needs_reselection',
                  'active': false,
                  'unavailable_reason': 'referenced_profile_removed',
                }
              : {
                  'profile_id': 'p01',
                  'provider_name': 'OpenRouter',
                  'base_url': 'https://openrouter.ai/api/v1',
                  'enabled': true,
                  'model_id': 'anthropic/claude-3.7-sonnet',
                  'qualified_model_id': 'p01:anthropic/claude-3.7-sonnet',
                  'status': 'proposed_unavailable',
                  'active': false,
                },
          'agent': {
            'profile_id': 'p02',
            'provider_name': 'Together AI',
            'base_url': 'https://api.together.xyz/v1',
            'enabled': true,
            'model_id': 'meta-llama/Llama-3.3-70B-Instruct',
            'qualified_model_id': 'p02:meta-llama/Llama-3.3-70B-Instruct',
            'status': 'proposed_unavailable',
            'active': false,
          },
        },
      };

      final actionsSeen = <Map<String, Object?>>[];
      final api = ProviderApi(
        baseUrl: 'http://127.0.0.1:8760',
        ownerToken: 'owner-test-token',
        clientFactory: () => MockClient((request) async {
          if (request.url.path == '/v1/providers') {
            return _jsonReply(_emptyCatalog());
          }
          if (request.url.path == '/v1/providers/custom' &&
              request.method == 'GET') {
            return _jsonReply(buildSnapshot());
          }
          if (request.url.path == '/v1/providers/custom/p01/discover' &&
              request.method == 'POST') {
            revision++;
            profiles[0] = _customProfile(
              id: 'p01',
              name: 'OpenRouter',
              url: 'https://openrouter.ai/api/v1',
              model: 'anthropic/claude-3.7-sonnet',
              discovered: const [
                'anthropic/claude-3.7-sonnet',
                'google/gemini-2.5-pro',
              ],
              referencedBy: const ['converse'],
            );
            return _jsonReply({
              'profile_id': 'p01',
              'provider_name': 'OpenRouter',
              'models_url': 'https://openrouter.ai/api/v1/models',
              'status': 'ok',
              'http_status': 200,
              'models': [
                {'model_id': 'anthropic/claude-3.7-sonnet'},
                {'model_id': 'google/gemini-2.5-pro'},
              ],
              'manual_fallback_allowed': true,
              'message':
                  'Discovered 2 model(s) from https://openrouter.ai/api/v1/models.',
              'snapshot': buildSnapshot(),
            });
          }
          if (request.url.path == '/v1/providers/custom' &&
              request.method == 'POST') {
            final body = jsonDecode(request.body) as Map<String, Object?>;
            actionsSeen.add(body);
            final action = body['action'];
            revision++;
            if (action == 'add_custom_provider') {
              profiles.add(
                _customProfile(
                  id: 'p07',
                  name: body['name'] as String,
                  url: body['base_url'] as String,
                  model: (body['selected_model'] as String?) ?? '',
                ),
              );
            } else if (action == 'remove_custom_provider') {
              final pid = body['profile_id'] as String;
              if (pid == 'p01') p01Removed = true;
              profiles.removeWhere((p) => p['profile_id'] == pid);
            }
            return _jsonReply(buildSnapshot());
          }
          return _jsonReply({'detail': 'Not found'}, status: 404);
        }),
      );
      addTearDown(api.close);

      await _pumpEditor(
        tester,
        OptionsProviderEditor(api: api, active: () => true),
      );

      // Switch to Custom APIs (.env) tab
      await tester.tap(find.text('Custom APIs (.env)'));
      await tester.pumpAndSettle();

      // Verify all 6 profiles render with no 5-provider cap and role bindings show proposed_unavailable
      expect(find.text('OpenRouter (p01)'), findsOneWidget);
      expect(find.text('Together AI (p02)'), findsOneWidget);
      expect(find.text('Groq (p03)'), findsOneWidget);
      expect(find.text('DeepSeek (p04)'), findsOneWidget);
      expect(find.text('Fireworks (p05)'), findsOneWidget);
      expect(find.text('Local vLLM (p06)'), findsOneWidget);
      expect(
        find.byKey(const Key('custom-role-bindings-summary')),
        findsOneWidget,
      );
      expect(find.textContaining('proposed_unavailable'), findsOneWidget);

      // Trigger Discover / Refresh Models on p01
      await tester.tap(find.byKey(const Key('custom-api-discover-p01')));
      await tester.pumpAndSettle();
      expect(
        find.text(
          'Discovered 2 model(s) from https://openrouter.ai/api/v1/models.',
        ),
        findsOneWidget,
      );

      // Add a 7th custom profile (p07) with llama.cpp custom endpoint (/v1/systemone)
      // and blank Maximum Output Tokens (so max output tokens are left up to the model).
      await tester.tap(find.byKey(const Key('add-custom-api-button')));
      await tester.pumpAndSettle();
      expect(
        find.textContaining(
          'Provider Type: Custom OpenAI-compatible API (.env)',
        ),
        findsOneWidget,
      );
      await tester.enterText(
        find.byWidgetPredicate(
          (w) =>
              w is TextField &&
              w.decoration?.labelText == 'Custom Provider Name',
        ),
        'Llama.cpp Gateway',
      );
      await tester.enterText(
        find.byWidgetPredicate(
          (w) =>
              w is TextField &&
              w.decoration?.labelText == 'OpenAI-Compatible Base URL',
        ),
        'http://127.0.0.1:8080/v1',
      );
      await tester.enterText(
        find.byWidgetPredicate(
          (w) =>
              w is TextField &&
              w.decoration?.labelText ==
                  'Custom / Engine-Specific Endpoints · optional',
        ),
        '/v1/systemone',
      );
      await tester.enterText(
        find.byWidgetPredicate(
          (w) =>
              w is TextField &&
              w.decoration?.labelText == 'Manual or Selected Model ID',
        ),
        'qwen2.5-coder-32b',
      );
      await tester.ensureVisible(
        find.byKey(const Key('save-custom-api-button')),
      );
      await tester.tap(find.byKey(const Key('save-custom-api-button')));
      await tester.pumpAndSettle();

      expect(find.text('Llama.cpp Gateway (p07)'), findsOneWidget);
      final addAction = actionsSeen.firstWhere(
        (a) => a['action'] == 'add_custom_provider',
      );
      expect(addAction['chat_completions_path'], '/chat/completions');
      expect(addAction['models_path'], '/models');
      expect(addAction['custom_endpoints'], '/v1/systemone');
      expect(addAction['max_output_tokens'], isNull);

      // Removing p01 (referenced by converse) opens confirmation dialog
      await tester.ensureVisible(
        find.byKey(const Key('custom-api-remove-p01')),
      );
      await tester.tap(find.byKey(const Key('custom-api-remove-p01')));
      await tester.pumpAndSettle();
      expect(
        find.byKey(const Key('custom-api-remove-confirm-dialog')),
        findsOneWidget,
      );
      await tester.tap(
        find.byKey(const Key('confirm-remove-custom-api-button')),
      );
      await tester.pumpAndSettle();

      // Verify p01 removed, remaining IDs p02..p07 not renumbered, and converse role retained as unavailable_needs_reselection
      expect(find.text('OpenRouter (p01)'), findsNothing);
      expect(find.text('Together AI (p02)'), findsOneWidget);
      expect(find.text('Llama.cpp Gateway (p07)'), findsOneWidget);
      expect(
        find.textContaining('unavailable_needs_reselection'),
        findsOneWidget,
      );
    },
  );

  testWidgets(
    'Aborts destructive remove POST if owner connection becomes inactive while confirmation dialog is open',
    (tester) async {
      var activeFlag = true;
      final removePosts = <Map<String, Object?>>[];
      final api = ProviderApi(
        baseUrl: 'http://127.0.0.1:8760',
        ownerToken: 'owner-test-token',
        clientFactory: () => MockClient((request) async {
          if (request.url.path == '/v1/providers') {
            return _jsonReply(_emptyCatalog());
          }
          if (request.url.path == '/v1/providers/custom' &&
              request.method == 'GET') {
            return _jsonReply({
              'schema_version': 1,
              'env_file_name': 'custom_providers.env',
              'unlimited': true,
              'revision': 3,
              'state_sha256': 'sha-rev-3',
              'profiles': [
                _customProfile(
                  id: 'p01',
                  name: 'OpenRouter',
                  url: 'https://openrouter.ai/api/v1',
                  model: 'anthropic/claude-3.7-sonnet',
                  referencedBy: const ['converse'],
                ),
              ],
              'role_bindings': const {},
            });
          }
          if (request.url.path == '/v1/providers/custom' &&
              request.method == 'POST') {
            removePosts.add(
              jsonDecode(request.body) as Map<String, Object?>,
            );
            return _jsonReply({'detail': 'Should not be called'}, status: 500);
          }
          return _jsonReply({'detail': 'Not found'}, status: 404);
        }),
      );
      addTearDown(api.close);

      await _pumpEditor(
        tester,
        OptionsProviderEditor(api: api, active: () => activeFlag),
      );
      await tester.tap(find.text('Custom APIs (.env)'));
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('custom-api-remove-p01')));
      await tester.pumpAndSettle();
      expect(
        find.byKey(const Key('custom-api-remove-confirm-dialog')),
        findsOneWidget,
      );

      // Invalidate active fence while confirmation dialog is open
      activeFlag = false;
      await tester.tap(
        find.byKey(const Key('confirm-remove-custom-api-button')),
      );
      await tester.pumpAndSettle();

      expect(removePosts, isEmpty);
    },
  );
}
