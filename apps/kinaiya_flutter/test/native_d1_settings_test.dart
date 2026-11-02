import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/decisions_model_settings.dart';
import 'package:kinaiya_flutter/api/provider_settings.dart';

Map<String, Object?> nativeConfig() => {
  'kind': 'native_d1',
  'base_url': 'http://127.0.0.1:8816',
  'model': 'LiquidAI/d1-Omni-2B',
  'min_confidence': 0.67,
};

Map<String, Object?> nativeProfile(Map<String, Object?> config) => {
  'id': 'native-d1',
  'label': 'Local d1',
  'generation': 1,
  'configuration_sha256': providerConfigurationDigest(config),
  'removed': false,
  'credential_available': true,
  'config': config,
  'capability': {
    'qualification': 'not_checked',
    'readiness': 'unknown',
    'declared_roles': ['decisions'],
    'supported_inputs': ['text'],
    'measured_complete_interface': false,
    'measured_stream_interface': false,
  },
};

Map<String, Object?> decisionsProvider(String kind) => {
  'schema_version': '1.0',
  'provider': kind,
  'model': 'local-model',
  'local': true,
  'streaming': false,
};

Map<String, Object?> decisionsProfile(String kind, String protocol) => {
  'id': kind,
  'label': kind == 'native_d1' ? 'Local d1' : 'Laya',
  'catalog_binding': {
    'id': kind,
    'generation': 1,
    'configuration_sha256': 'd' * 64,
  },
  'selectable': true,
  'provider': decisionsProvider(kind),
  'reason': 'Declaration only',
  'protocol': protocol,
  'qualification': 'not_checked',
  'readiness': 'unknown',
};

Map<String, Object?> decisionsSnapshot({String host = 'compatible'}) => {
  'schema_version': '1.0',
  'backend_id': 'a' * 32,
  'instance_id': 'b' * 32,
  'revision': 0,
  'state_token': 'c' * 64,
  'selection': {'source': 'host', 'catalog_binding': null},
  'selected_info': decisionsProvider(host),
  'selected_error': null,
  'host': {
    'provider': decisionsProvider(host),
    'selectable': true,
    'protocol': host == 'native_d1'
        ? 'native_d1_systemone'
        : host == 'laya'
        ? 'laya_systemone'
        : 'host_configured',
  },
  'catalog_profiles': [
    decisionsProfile('native_d1', 'native_d1_systemone'),
    decisionsProfile('laya', 'laya_systemone'),
  ],
  'catalog_error': null,
  'busy': false,
  'operation_state': {
    'preparing': 0,
    'finalized': 0,
    'windows': 0,
    'health': 0,
    'leases': 0,
    'cleanup_pending': 0,
    'cleanup_failed': 0,
    'closed': false,
  },
  'change_boundary': 'settled_decisions_operations',
  'qualification': 'not_checked',
  'readiness': 'unknown',
  'limits': {'leases': 64, 'receipts': 10000},
};

void main() {
  test('native configuration hashes match Python catalog float bytes', () {
    // Fixed vectors computed with the native catalog's json.dumps settings.
    final vectors = {
      0.0: '5588220868ed189e0c619c50465e75b715b514b831aac57d1872f2b335b6e5be',
      1.0: '00242df283c764c806a84e94c77b509daabaddd3a5ffe49824a52284e50ed153',
      0.67: '7e3ebc4419b3d83d5749268dafb806cf778dee0f0bebc28d7f71878918acc91e',
      0.0001:
          'ad9af1dd57951c650d796df0bf3ef9f8b52380f38330c2e298e64371d3131adb',
      1e-5: 'd546ebf28cdca48fc15c5af34892c9bebb7594bb82eedc3ebdce9bee668716f2',
      1e-7: '4fa5fd7449af6dee3c484859ef5894ef78f82cbff9fdea3370e834e396be89ad',
      5e-324:
          'af964a28f4a2d95738a8e7b24adffb7f4b628c946eadf0be53d0da6708279b9b',
      0.00009999999999999999:
          'f372a870b175b0b1fc58c633597c04ee32103758a43ff8d8a8f48135684203e1',
    };
    for (final vector in vectors.entries) {
      expect(
        providerConfigurationDigest({
          ...nativeConfig(),
          'min_confidence': vector.key,
        }),
        vector.value,
      );
    }
    expect(
      providerConfigurationDigest({...nativeConfig(), 'min_confidence': -0.0}),
      '51b501a94a07962cacc6ae4e2832aeb39bc81fd616724e20f942d101f1e9df6e',
    );
  });
  test(
    'native config materializes only its frozen defaults and credential',
    () {
      final value = providerConfig(nativeConfig());
      expect(value, {
        ...nativeConfig(),
        'allow_cloud': false,
        'credential': null,
        'timeout_s': 5,
      });
      expect(
        providerConfig({
          ...nativeConfig(),
          'min_confidence': 0,
        })['min_confidence'],
        isA<double>(),
      );
      final credential = {'id': 'local-key', 'generation': 'a' * 32};
      expect(
        providerConfig({...value, 'credential': credential})['credential'],
        credential,
      );
      expect(() => value['min_confidence'] = 0.8, throwsUnsupportedError);
    },
  );

  for (final endpoint in [
    'http://localhost:80/',
    'http://LOCALHOST:8816',
    'https://[::1]:443',
    'http://127.0.0.1:65535',
  ]) {
    test('native accepts explicit local root $endpoint', () {
      expect(
        providerConfig({
          ...nativeConfig(),
          'base_url': endpoint,
        })['allow_cloud'],
        false,
      );
    });
  }
  for (final endpoint in [
    'http://localhost',
    'https://example.com:443',
    'http://127.0.0.1:0',
    'http://127.0.0.1:65536',
    'http://localhost:8816/v1',
    'http://localhost:8816/?x=1',
    'http://localhost:8816/#x',
    'http://user@localhost:8816',
    'ws://localhost:8816',
    'http://localhоst:8816',
    'http://localhost:8816\\',
    'http://localhost:8816/%2f',
    'http://[::1]suffix:8816',
    'http://[::1]:8816suffix',
    'http://[::1]:8816\n',
  ]) {
    test('native refuses noncanonical endpoint $endpoint', () {
      expect(
        () => providerConfig({...nativeConfig(), 'base_url': endpoint}),
        throwsFormatException,
      );
    });
  }
  for (final floor in [
    null,
    true,
    '0.8',
    -0.1,
    1.1,
    double.nan,
    double.infinity,
  ]) {
    test('native requires a finite owner confidence: $floor', () {
      expect(
        () => providerConfig({...nativeConfig(), 'min_confidence': floor}),
        throwsFormatException,
      );
    });
  }
  test('native has no guessed confidence or legacy knobs', () {
    final missing = nativeConfig()..remove('min_confidence');
    expect(() => providerConfig(missing), throwsFormatException);
    for (final extra in [
      'max_tokens',
      'context_tokens',
      'reasoning_effort',
      'max_len',
      'head_max_len',
      'expected_artifact_sha256',
    ]) {
      expect(
        () => providerConfig({...nativeConfig(), extra: null}),
        throwsFormatException,
      );
    }
    for (final cloud in [true, 0, null]) {
      expect(
        () => providerConfig({...nativeConfig(), 'allow_cloud': cloud}),
        throwsFormatException,
      );
    }
    for (final timeout in [null, true, 0, 121, 5.0]) {
      expect(
        () => providerConfig({...nativeConfig(), 'timeout_s': timeout}),
        throwsFormatException,
      );
    }
    for (final model in ['', '-alias', 'd1 Omni', 'd1\n', 'α', 'a' * 129]) {
      expect(
        () => providerConfig({...nativeConfig(), 'model': model}),
        throwsFormatException,
      );
    }
  });
  test('native catalog stays text-only Decisions and unmeasured', () {
    final profile = nativeProfile(providerConfig(nativeConfig()));
    expect(ProviderProfile.fromJson(profile).declaredRoles, ['decisions']);
    for (final update in [
      {
        'declared_roles': ['agent'],
      },
      {
        'supported_inputs': ['audio'],
      },
      {'measured_complete_interface': true},
      {'measured_stream_interface': true},
    ]) {
      expect(
        () => ProviderProfile.fromJson({
          ...profile,
          'capability': {
            ...profile['capability'] as Map<String, Object?>,
            ...update,
          },
        }),
        throwsFormatException,
      );
    }
  });
  test('wire confidence roundtrips without widening old numeric fields', () {
    final config = providerConfig(nativeConfig());
    final wire = utf8.encode(jsonEncode(nativeProfile(config)));
    final profile = ProviderProfile.fromJson(providerJsonDecode(wire));
    expect(profile.config['min_confidence'], 0.67);
    final reversed =
        providerJsonDecode(
              utf8.encode('{"min_confidence":6.7e-1,"kind":"native_d1"}'),
            )
            as Map<String, Object?>;
    expect(reversed['min_confidence'], 0.67);
    for (final raw in [
      '{"kind":"laya","min_confidence":0.67}',
      '{"kind":"native_d1","timeout_s":5.0}',
      '{"revision":0.0}',
      '[0.67]',
      '{"kind":"native_d1","min_confidence":1e999}',
      '{"kind":"native_d1","min_confidence":.67}',
      '{"kind":"native_d1","min_confidence":0.}',
      '{"kind":"native_d1","min_confidence":0.67,"min_confidence":0.8}',
    ]) {
      expect(() => providerJsonDecode(utf8.encode(raw)), throwsFormatException);
    }
  });
  test(
    'native metadata does not claim cloud, streaming or a different alias grammar',
    () {
      for (final update in [
        {'local': false},
        {'streaming': true},
        {'model': 'not an alias'},
      ]) {
        final value = decisionsSnapshot();
        value['selected_info'] = {...decisionsProvider('native_d1'), ...update};
        expect(
          () => DecisionsModelsSnapshot.fromJson(value),
          throwsFormatException,
        );
      }
    },
  );
  test(
    'mixed native and legacy declarations keep their exact protocol pairs',
    () {
      final snapshot = DecisionsModelsSnapshot.fromJson(decisionsSnapshot());
      expect(snapshot.profiles.map((p) => p.protocol), [
        'native_d1_systemone',
        'laya_systemone',
      ]);
      expect(
        snapshot.permits(
          DecisionsModelSelection.catalog(snapshot.profiles.first.binding),
        ),
        true,
      );
      for (final host in ['native_d1', 'laya', 'compatible']) {
        expect(
          DecisionsModelsSnapshot.fromJson(
            decisionsSnapshot(host: host),
          ).hostSelectable,
          true,
        );
      }
      for (final pair in [
        ('native_d1', 'laya_systemone'),
        ('laya', 'native_d1_systemone'),
        ('compatible', 'native_d1_systemone'),
      ]) {
        expect(
          () => DecisionsCatalogProfile.fromJson(
            decisionsProfile(pair.$1, pair.$2),
          ),
          throwsFormatException,
        );
        final value = decisionsSnapshot(host: pair.$1);
        (value['host'] as Map<String, Object?>)['protocol'] = pair.$2;
        expect(
          () => DecisionsModelsSnapshot.fromJson(value),
          throwsFormatException,
        );
      }
    },
  );
}
