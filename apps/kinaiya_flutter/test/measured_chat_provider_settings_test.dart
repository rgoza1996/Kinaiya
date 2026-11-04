// Independent #447 phase-two binding oracles; public synthetic declarations.
// Matching native/Dart bytes concern measured text, not native media readiness.
import 'dart:convert';

import 'package:crypto/crypto.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/provider_settings.dart';

const base = 'http://127.0.0.1:19447/v1';
const model = 'synthetic-model';
// Specified independently of either normalizer; also pinned by the Python test.
const canonical =
    '{"allow_cloud":false,"base_url":"http://127.0.0.1:19447/v1",'
    '"context_tokens":4096,"credential":null,"kind":"measured_chat",'
    '"max_tokens":2048,"model":"synthetic-model",'
    '"reasoning_effort":"none","timeout_s":90}';
const legacyCanonical =
    '{"allow_cloud":false,"base_url":"http://127.0.0.1:19447/v1",'
    '"context_tokens":null,"credential":null,"kind":"compatible",'
    '"max_tokens":2048,"model":"synthetic-model",'
    '"reasoning_effort":null,"timeout_s":90}';

Map<String, Object?> config([Map<String, Object?> changes = const {}]) => {
  'kind': 'measured_chat',
  'base_url': base,
  'model': model,
  'context_tokens': 4096,
  ...changes,
};

Map<String, Object?> profile(Map<String, Object?> declaration) => {
  'id': 'measured-local',
  'label': 'Public synthetic measured chat',
  'generation': 1,
  'configuration_sha256': providerConfigurationDigest(declaration),
  'removed': false,
  'credential_available': true,
  'config': declaration,
  'capability': {
    'declared_roles': ['converse'],
    'supported_inputs': ['text'],
    'measured_complete_interface': true,
    'measured_stream_interface': true,
    'qualification': 'not_checked',
    'readiness': 'unknown',
  },
};

void main() {
  test('measured defaults and canonical digest match native frozen bytes', () {
    final normalized = providerConfig(config());
    expect(normalized, jsonDecode(canonical));
    expect(
      providerConfigurationDigest(config()),
      sha256.convert(utf8.encode(canonical)).toString(),
    );
    expect(providerConfig(config({'base_url': '$base/'})), normalized);
    expect(() => normalized['context_tokens'] = 8192, throwsUnsupportedError);
    expect(providerKinds, contains('measured_chat'));
  });

  test('legacy compatible nulls and digest stay byte exact', () {
    final legacy = {
      'kind': 'compatible',
      'base_url': base,
      'model': model,
      'allow_cloud': false,
    };
    expect(providerConfig(legacy), jsonDecode(legacyCanonical));
    expect(
      providerConfigurationDigest(legacy),
      sha256.convert(utf8.encode(legacyCanonical)).toString(),
    );
  });

  test('profile retains independent protected credential generation', () {
    final credential = {'id': 'local-key', 'generation': 'a' * 32};
    final declaration = config({'credential': credential});
    final normalized = providerConfig(declaration);
    expect(normalized['credential'], credential);
    final raw = profile(declaration);
    final parsed = ProviderProfile.fromJson(raw);
    expect(parsed.config, normalized);
    expect(parsed.declaredRoles, ['converse']);
    expect(parsed.measuredComplete, true);
    expect(parsed.measuredStream, true);
    expect(parsed.credential?.id, 'local-key');
    expect(parsed.credential?.generation, 'a' * 32);
    expect(
      () =>
          ProviderProfile.fromJson({...raw, 'configuration_sha256': '0' * 64}),
      throwsFormatException,
    );
  });

  test('required context and explicit non-null effort match strict native', () {
    final missing = config()..remove('context_tokens');
    expect(() => providerConfig(missing), throwsFormatException);
    for (final effort in providerEfforts) {
      expect(
        providerConfig(
          config({
            'context_tokens': 64,
            'max_tokens': 16,
            'timeout_s': 1,
            'reasoning_effort': effort,
          }),
        )['reasoning_effort'],
        effort,
      );
    }
    expect(
      providerConfig(
        config({
          'context_tokens': providerMaxRevision,
          'max_tokens': 65536,
          'timeout_s': 900,
          'model': 'é' * 128,
        }),
      )['context_tokens'],
      providerMaxRevision,
    );
  });

  for (final endpoint in [
    'http://127.0.0.1',
    'https://127.0.0.1:443/v1',
    'http://[::1]:19447/v1',
    'https://[::1]/v1',
  ]) {
    test('accepts literal local endpoint $endpoint', () {
      expect(
        providerConfig(config({'base_url': endpoint}))['allow_cloud'],
        false,
      );
    });
  }

  for (final endpoint in [
    'http://localhost:19447/v1',
    'http://LOCALHOST:19447/v1',
    'http://127.1:19447/v1',
    'http://2130706433:19447/v1',
    'https://example.com/v1',
    'http://0.0.0.0/v1',
    'ws://127.0.0.1:19447/v1',
    'http://user@127.0.0.1:19447/v1',
    'http://@127.0.0.1:19447/v1',
    'http://127.0.0.1:19447/v1?key=x',
    'http://127.0.0.1:19447/v1#fragment',
    'http://127.0.0.1:19447/v1?',
    'http://127.0.0.1:19447/v1#',
    'http://127.0.0.1:19447/v1\\other',
    'http://127.0.0.1:19447/v1\n',
    'http://127.0.0.1:0/v1',
    'http://127.0.0.1:65536/v1',
  ]) {
    test('refuses ambiguous or non-literal endpoint $endpoint', () {
      expect(
        () => providerConfig(config({'base_url': endpoint})),
        throwsFormatException,
      );
    });
  }

  final invalidFields = <String, List<Object?>>{
    'allow_cloud': [true, 0, 'false', null],
    'context_tokens': [null, true, 4096.0, 63, providerMaxRevision + 1],
    'max_tokens': [null, 15, 65537, true, 32.0, 4096, 4097],
    'reasoning_effort': [null, 'max', false],
    'timeout_s': [null, 0, 901, true, 5.0],
    'model': [' synthetic-model', 'synthetic\nmodel', 'a\u0085b', 'é' * 129],
    'credential': [
      {'id': 'local-key', 'generation': 'a' * 31},
      {
        'id': 'local-key',
        'generation': 'a' * 32,
        'key': 'public-synthetic-not-a-real-key',
      },
    ],
    'unknown': ['must-not-be-ignored'],
  };
  for (final field in invalidFields.entries) {
    for (var index = 0; index < field.value.length; index++) {
      test('strict native parity refuses ${field.key} fixture $index', () {
        expect(
          () => providerConfig(config({field.key: field.value[index]})),
          throwsFormatException,
        );
      });
    }
  }
}
