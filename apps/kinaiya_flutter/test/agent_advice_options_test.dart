import 'dart:convert';

import 'package:crypto/crypto.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/decisions_model_client.dart';
import 'package:kinaiya_flutter/api/decisions_model_settings.dart';
import 'package:kinaiya_flutter/api/models.dart' show ContentScope;
import 'package:kinaiya_flutter/api/provider_client.dart';
import 'package:kinaiya_flutter/api/provider_settings.dart';
import 'package:kinaiya_flutter/config/options_navigation.dart';
import 'package:kinaiya_flutter/ui/options_agent_advice.dart';

import 'native_d1_options_test.dart' show catalog, reply;

const _path = '/v1/decisions/options/agent-advice-settings';
const _backend = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
const _instance = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';
const _legacyModel = {
  'id': 'legacy-advisor',
  'generation': 1,
  'configuration_sha256':
      'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
};

final _legacy = AgentAdvicePolicy(
  binding: DecisionsCatalogBinding.fromJson(_legacyModel),
  instructions: ' Legacy synthetic advice.\n',
  contentScope: ContentScope.localOnly,
);

Map<String, Object?> _observation(String source, AgentAdvicePolicy? policy) => {
  'contract_version': 1,
  'source': source,
  'policy': policy?.body,
  'policy_sha256': policy?.digest,
  'catalog_status': policy == null ? 'not_selected' : 'eligible',
};

class _Host {
  _Host(this.token);

  final String token;
  int revision = 0, writes = 0, lookups = 0;
  String backend = _backend;
  String source = 'host';
  bool loseSave = false, changeReceipt = false;
  bool refuseSelection = false, replaceWithUnboundRefusal = false;
  AgentAdvicePolicy? saved;
  Map<String, Object?>? receipt;
  List<int>? originalBytes;

  Map<String, Object?> get snapshot => {
    'contract_version': 1,
    'owner': 'decisions',
    'consumer': 'agent',
    'backend_id': backend,
    'instance_id': _instance,
    'revision': revision,
    'selection_source': source,
    'host': {
      ..._observation('host', _legacy),
      'owner': 'decisions',
      'consumer': 'agent',
      'observation': 'catalog_metadata_only',
      'change_boundary': 'restart_required',
      'request_admission': 'unavailable',
    },
    'active': _observation('host', _legacy),
    'next_launch': _observation(source, source == 'host' ? _legacy : saved),
    'change_boundary': 'restart_required',
    'writable': true,
  };

  DecisionsModelsApi connect() => DecisionsModelsApi(
    baseUrl: 'http://127.0.0.1:8763',
    ownerToken: token,
    clientFactory: () => MockClient(respond),
  );

  Future<http.Response> respond(http.Request request) async {
    expect(request.headers['authorization'], 'Bearer $token');
    expect(request.followRedirects, isFalse);
    if (request.method == 'GET' && request.url.path == _path) {
      return reply(snapshot);
    }
    expect(request.method, 'POST');
    final raw =
        jsonDecode(utf8.decode(request.bodyBytes)) as Map<String, Object?>;
    final digest = sha256.convert(request.bodyBytes).toString();
    expect(raw['expected_backend'], backend);
    if (request.url.path == '$_path/lookup') {
      lookups++;
      expect(request.bodyBytes, originalBytes);
      return reply({
        'contract_version': 1,
        'backend_id': backend,
        'instance_id': _instance,
        'current_revision': revision,
        'request_sha256': digest,
        'receipt': receipt,
        'mutation_fenced': false,
      });
    }
    expect(request.url.path, _path);
    expect(raw['expected_instance'], _instance);
    expect(raw['expected_revision'], revision);
    if (refuseSelection) {
      return http.Response(
        jsonEncode({
          'contract_version': 1,
          'status': 'refused',
          'request_sha256': digest,
          'reason': 'selection_unavailable',
        }),
        400,
        headers: {
          'content-type': 'application/json',
          'cache-control': 'no-store',
        },
      );
    }
    originalBytes = List.of(request.bodyBytes);
    final selection = raw['selection'] as Map<String, Object?>;
    source = selection['source'] as String;
    saved = source == 'saved'
        ? AgentAdvicePolicy.fromJson(selection['policy'])
        : null;
    revision++;
    writes++;
    receipt = {
      'contract_version': 1,
      'status': 'saved',
      'backend_id': backend,
      'instance_id': _instance,
      'revision': revision,
      'idempotency_key': raw['idempotency_key'],
      'request_sha256': digest,
      'selection_source': source,
      'policy_sha256': saved?.digest,
      'change_boundary': 'restart_required',
    };
    if (loseSave) throw http.ClientException('Synthetic disconnected observer');
    if (replaceWithUnboundRefusal) {
      return http.Response(
        '{"detail":"Unrelated synthetic response"}',
        400,
        headers: {'content-type': 'application/json'},
      );
    }
    return reply(
      changeReceipt ? {...receipt!, 'policy_sha256': 'corrupt'} : receipt!,
    );
  }
}

ProviderApi _providers(String token) {
  final config = providerConfig({
    'kind': 'lmstudio',
    'base_url': 'http://127.0.0.1:1234/v1',
    'model': 'synthetic-advisor',
    'allow_cloud': false,
  });
  return ProviderApi(
    baseUrl: 'http://127.0.0.1:8760',
    ownerToken: token,
    clientFactory: () => MockClient((request) async {
      expect(request.method, 'GET');
      expect(request.url.path, '/v1/providers');
      return reply(
        catalog([
          {
            'id': 'current-advisor',
            'generation': 2,
            'configuration_sha256': providerConfigurationDigest(config),
            'label': 'Core advisor',
            'removed': false,
            'credential_available': true,
            'config': config,
            'capability': {
              'declared_roles': ['agent', 'converse', 'auxiliary'],
              'supported_inputs': ['text'],
              'measured_complete_interface': true,
              'measured_stream_interface': true,
              'qualification': 'not_checked',
              'readiness': 'unknown',
            },
          },
        ]),
      );
    }),
  );
}

Future<void> _show(
  WidgetTester tester,
  DecisionsModelsApi client,
  ProviderApi providers,
) async {
  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(
        body: SingleChildScrollView(
          child: OptionsAgentAdvice(
            key: ObjectKey(client),
            api: client.agentAdvice,
            providers: providers,
            active: () => true,
          ),
        ),
      ),
    ),
  );
  await tester.pumpAndSettle();
}

Future<void> _pick(WidgetTester tester, String label, String value) async {
  final field = find.byKey(ValueKey('agent-advice-$label'));
  await tester.ensureVisible(field);
  await tester.tap(field);
  await tester.pumpAndSettle();
  await tester.tap(find.text(value).last);
  await tester.pumpAndSettle();
}

Future<void> _tap(WidgetTester tester, String title) async {
  final button = find.text(title);
  await tester.ensureVisible(button);
  await tester.tap(button);
  await tester.pumpAndSettle();
}

String _text(WidgetTester tester, String key) =>
    tester.widget<Text>(find.byKey(ValueKey(key))).data!;

void main() {
  test('Agent advice belongs to the Decisions Options category', () {
    final match = searchOptionsPages(
      'agent advice',
    ).firstWhere((result) => result.page == OptionsPage.agentAdvice);
    expect(match.category.label, 'Decisions');
  });

  testWidgets(
    'save binds the selected Core generation and exact instructions',
    (tester) async {
      tester.view.physicalSize = const Size(392, 760);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final host = _Host('synthetic-advice-save'), client = host.connect();
      final providers = _providers(host.token);
      addTearDown(client.close);
      addTearDown(providers.close);
      await _show(tester, client, providers);
      expect(_text(tester, 'agent-advice-active'), contains('legacy-advisor'));
      await _tap(tester, 'Active policy details');
      expect(
        find.byWidgetPredicate(
          (widget) =>
              widget is SelectableText && widget.data == _legacy.instructions,
        ),
        findsOneWidget,
      );
      await _tap(tester, 'Active policy details');
      await _pick(tester, 'Policy source', 'Use a Core model');
      await _pick(tester, 'Core model', 'Core advisor · Generation 2');
      await _pick(tester, 'Instruction disclosure', 'Public or synthetic');
      const instructions = '   Exact synthetic instructions Ω.\n';
      final field = find.byKey(const ValueKey('agent-advice-instructions'));
      await tester.ensureVisible(field);
      await tester.enterText(field, instructions);
      await _tap(tester, 'Save for next launch');
      expect(host.writes, 1);
      expect(host.saved!.instructions, instructions);
      expect(host.saved!.binding.id, 'current-advisor');
      expect(host.saved!.binding.generation, 2);
      expect(host.saved!.contentScope, ContentScope.publicSynthetic);
      expect(_text(tester, 'agent-advice-active'), contains('legacy-advisor'));
      expect(
        _text(tester, 'agent-advice-next-launch'),
        contains('Core advisor'),
      );
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'same-owner reconnect recovers a lost save without another write',
    (tester) async {
      final host = _Host('synthetic-advice-reconnect')..loseSave = true;
      final first = host.connect(), providers = _providers(host.token);
      addTearDown(first.close);
      addTearDown(providers.close);
      await _show(tester, first, providers);
      await _pick(tester, 'Policy source', 'Disable advice');
      await _tap(tester, 'Save for next launch');
      expect(first.agentAdvice.pending, isNotNull);
      expect(host.writes, 1);
      expect(find.text('Check original save'), findsOneWidget);
      await tester.pumpWidget(const SizedBox.shrink());
      await tester.runAsync(first.close);
      final second = host.connect();
      addTearDown(second.close);
      await _show(tester, second, providers);
      expect(second.agentAdvice.pending, isNotNull);
      await _tap(tester, 'Check original save');
      expect(second.agentAdvice.pending, isNull);
      expect(host.lookups, 1);
      expect(host.writes, 1);
      expect(_text(tester, 'agent-advice-active'), contains('Host policy'));
      expect(_text(tester, 'agent-advice-next-launch'), 'Disabled');
      expect(
        _text(tester, 'agent-advice-notice'),
        'The original save was recorded.',
      );
    },
  );

  testWidgets(
    'a stale draft requires explicit adoption of the current policy',
    (tester) async {
      final host = _Host('synthetic-advice-stale'), client = host.connect();
      final providers = _providers(host.token);
      addTearDown(client.close);
      addTearDown(providers.close);
      await _show(tester, client, providers);
      await _pick(tester, 'Policy source', 'Disable advice');
      host
        ..revision = 1
        ..source = 'disabled';
      await _tap(tester, 'Save for next launch');
      expect(host.writes, 0);
      expect(find.text('Use current policy'), findsOneWidget);
      await _tap(tester, 'Use current policy');
      expect(find.text('Use current policy'), findsNothing);
      await _pick(tester, 'Policy source', 'Use host policy');
      await _tap(tester, 'Save for next launch');
      expect(host.writes, 1);
      expect(host.source, 'host');
    },
  );

  test(
    'a mismatched receipt remains uncertain and a replacement is never queried',
    () async {
      final host = _Host('synthetic-advice-proof')..changeReceipt = true;
      final client = host.connect();
      addTearDown(client.close);
      final api = client.agentAdvice;
      final base = await api.inspect();
      final original = AgentAdvicePolicyChange(
        base: base,
        key: 'synthetic-save-proof',
        source: AgentAdviceSource.disabled,
      );
      await expectLater(
        api.save(original, current: () => true),
        throwsA(isA<DecisionsModelFailure>()),
      );
      expect(api.pending, same(original));
      host.backend = 'dddddddddddddddddddddddddddddddd';
      await expectLater(
        api.lookup(current: () => true),
        throwsA(isA<DecisionsModelFailure>()),
      );
      expect(host.lookups, 0);
      expect(api.pending, same(original));
      host.backend = _backend;
      expect((await api.lookup(current: () => true)).receipt?.revision, 1);
      expect(api.pending, isNull);
    },
  );

  test('a generic HTTP 400 keeps the original eligible for lookup', () async {
    final host = _Host('synthetic-unbound-refusal')
      ..replaceWithUnboundRefusal = true;
    final client = host.connect(), api = client.agentAdvice;
    addTearDown(client.close);
    final original = AgentAdvicePolicyChange(
      base: await api.inspect(),
      key: 'original-with-unbound-http-refusal',
      source: AgentAdviceSource.disabled,
    );
    await expectLater(
      api.save(original, current: () => true),
      throwsA(isA<DecisionsModelFailure>()),
    );
    expect(host.writes, 1);
    expect(api.pending, same(original));
    expect((await api.lookup(current: () => true)).receipt?.revision, 1);
    expect(api.pending, isNull);
  });

  test(
    'an exact native selection refusal releases the unsaved candidate',
    () async {
      final host = _Host('synthetic-bound-refusal')..refuseSelection = true;
      final client = host.connect(), api = client.agentAdvice;
      addTearDown(client.close);
      final original = AgentAdvicePolicyChange(
        base: await api.inspect(),
        key: 'original-with-exact-refusal',
        source: AgentAdviceSource.saved,
        policy: _legacy,
      );
      await expectLater(
        api.save(original, current: () => true),
        throwsA(
          isA<DecisionsModelFailure>().having(
            (error) => error.status,
            'status',
            400,
          ),
        ),
      );
      expect(host.writes, 0);
      expect(api.pending, isNull);
    },
  );
}
