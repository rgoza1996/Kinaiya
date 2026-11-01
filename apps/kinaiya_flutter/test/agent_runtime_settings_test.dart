import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/agent_runtime_settings.dart';
import 'package:kinaiya_flutter/ui/agent_runtime_writes.dart';

const runtimeBackendA = 'cccccccccccccccccccccccccccccccc';
const runtimeBackendB = 'dddddddddddddddddddddddddddddddd';
const runtimeInstanceA = '11111111-1111-4111-8111-111111111111';
const runtimeInstanceB = '22222222-2222-4222-8222-222222222222';
const runtimeState =
    'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee';

Map<String, Object?> runtimeSettingsJson({String directory = '.'}) => {
  'file_writes': 'ask',
  'shell_commands': 'ask',
  'tool_network': false,
  'max_steps': 8,
  'shell_timeout_s': 15,
  'working_directory': directory,
};
Map<String, Object?> runtimeAuthorityJson({
  String backend = runtimeBackendA,
  String instance = runtimeInstanceA,
  bool writable = true,
}) => {
  'mutation_version': '1.1',
  'backend_id': backend,
  'instance_id': instance,
  'writable': writable,
};
Map<String, Object?> runtimeSnapshotJson({
  String backend = runtimeBackendA,
  String instance = runtimeInstanceA,
  String state = runtimeState,
  int revision = 0,
  bool bound = true,
  bool writable = true,
  bool busy = false,
  bool yolo = false,
  String directory = '.',
}) => {
  'schema_version': '1.0',
  'revision': revision,
  'state_token': state,
  'instance_id': instance,
  'change_boundary': 'settled_service',
  'yolo_scope': 'service_instance',
  'source': 'host_configuration',
  'settings': runtimeSettingsJson(directory: directory),
  'saved_settings': runtimeSettingsJson(directory: directory),
  'active_matches_saved': true,
  'yolo': yolo,
  'effective_write': yolo,
  'effective_shell': yolo,
  'busy': busy,
  'execution': {
    'workspace': '/synthetic/workspace',
    'shell_workspace_access': 'read_only',
    'shell_directory_valid': true,
    'shell_directory': '/synthetic/workspace',
    'readonly': {'paths': <String>[], 'total': 0, 'truncated': false},
    'protected': {'paths': <String>[], 'total': 0, 'truncated': false},
    'shell_binary_detected': false,
    'isolation_verified': false,
    'network_namespace': 'shared_host',
    'shell_backend': 'bubblewrap',
    'sandbox_optional': false,
    'workspace_mutable': false,
    'environment': {
      'clear_host_environment': true,
      'PATH': '/usr/bin:/bin',
      'HOME': '/tmp',
    },
  },
  if (bound)
    'write_authority': runtimeAuthorityJson(
      backend: backend,
      instance: instance,
      writable: writable,
    ),
};
AgentRuntimeSnapshot runtimeSnapshot({
  String backend = runtimeBackendA,
  String instance = runtimeInstanceA,
  String state = runtimeState,
  int revision = 0,
  bool writable = true,
  bool busy = false,
  bool yolo = false,
}) => AgentRuntimeSnapshot.fromJson(
  runtimeSnapshotJson(
    backend: backend,
    instance: instance,
    state: state,
    revision: revision,
    writable: writable,
    busy: busy,
    yolo: yolo,
  ),
);
Map<String, Object?> runtimeChangeJson({
  String backend = runtimeBackendA,
  String instance = runtimeInstanceA,
  String key = 'execution-synthetic-key',
  int revision = 0,
  String state = runtimeState,
  bool yolo = false,
  String directory = '.',
}) => {
  'schema_version': '1.1',
  'expected_backend': backend,
  'expected_instance': instance,
  'expected_revision': revision,
  'expected_state': state,
  'idempotency_key': key,
  'settings': runtimeSettingsJson(directory: directory),
  'yolo': yolo,
};
Map<String, Object?> runtimeReceiptJson(Map<String, Object?> request) => {
  'schema_version': '1.1',
  'backend_id': request['expected_backend'],
  'service_instance': request['expected_instance'],
  'idempotency_key': request['idempotency_key'],
  'revision': (request['expected_revision'] as int) + 1,
  'settings': {...request['settings'] as Map<String, Object?>},
  'requested_yolo': request['yolo'],
  'change_boundary': 'settled_service',
  'status': 'settings_saved',
};
Map<String, Object?> runtimeLookupJson(
  Map<String, Object?> request, {
  bool recorded = true,
  bool fenced = true,
}) => {
  'schema_version': '1.1',
  'backend_id': request['expected_backend'],
  'request': {...request},
  'status': recorded ? 'recorded' : 'unobserved',
  'receipt': recorded ? runtimeReceiptJson(request) : null,
  'mutation_fenced': fenced,
};

void main() {
  test(
    'legacy opaque instance snapshots remain readable and unsupported authority cannot write',
    () {
      final old = AgentRuntimeSnapshot.fromJson(
        runtimeSnapshotJson(instance: 'legacy-opaque-instance', bound: false),
      );
      expect(old.instanceId, 'legacy-opaque-instance');
      expect(old.writeAuthority, isNull);
      expect(
        AgentRuntimeSnapshot.fromJson({
          ...runtimeSnapshotJson(),
          'write_authority': {'mutation_version': 'next'},
        }).writeAuthority,
        isNull,
      );
      for (final bad in [
        {...runtimeAuthorityJson(), 'instance_id': '1' * 32},
        {
          ...runtimeAuthorityJson(),
          'backend_id': runtimeBackendA.toUpperCase(),
        },
        {...runtimeAuthorityJson(), 'writable': 1},
        {...runtimeAuthorityJson(), 'extra': null},
        {...runtimeAuthorityJson()}..remove('instance_id'),
      ]) {
        expect(
          () => AgentRuntimeSnapshot.fromJson({
            ...runtimeSnapshotJson(),
            'write_authority': bad,
          }),
          throwsFormatException,
        );
      }
      expect(
        () => AgentRuntimeSnapshot.fromJson({
          ...runtimeSnapshotJson(),
          'write_authority': runtimeAuthorityJson(instance: runtimeInstanceB),
        }),
        throwsFormatException,
      );
      expect(
        runtimeSnapshot(writable: false).writeAuthority!.writable,
        isFalse,
      );
    },
  );

  test(
    'canonical immutable native relative paths retain backslash whitespace and Unicode codepoints',
    () {
      final cases = {
        './a//./b/': 'a/b',
        './//./': '.',
        r' ./folder\name//模型 /': r' ./folder\name/模型 ',
        '😀' * 512: '😀' * 512,
      };
      for (final entry in cases.entries) {
        final source = runtimeChangeJson(directory: entry.key);
        final request = AgentRuntimeChange.fromJson(source);
        final settings = request.body['settings'] as Map<String, Object?>;
        expect(settings['working_directory'], entry.value);
        (source['settings'] as Map<String, Object?>)['working_directory'] =
            'changed';
        expect(settings['working_directory'], entry.value);
        expect(() => settings['max_steps'] = 9, throwsUnsupportedError);
        expect(() => request.body['yolo'] = true, throwsUnsupportedError);
        final receipt = AgentRuntimeReceipt.fromJson(
          runtimeReceiptJson(request.body),
          request,
        );
        expect(receipt.json['settings'], settings);
        expect(
          () =>
              (receipt.json['settings'] as Map<String, Object?>)['max_steps'] =
                  9,
          throwsUnsupportedError,
        );
      }
      final draft = AgentRuntimeDraft(
        AgentRuntimeSnapshot.fromJson(
          runtimeSnapshotJson(directory: '😀' * 512),
        ),
      );
      expect(draft.dirty, isFalse);
      expect(draft.settings!['working_directory'], '😀' * 512);
      for (final path in [
        '',
        '/x',
        '//x',
        '../x',
        'x/../y',
        'a\u0000b',
        'a\u007fb',
        '😀' * 513,
        '\ud800',
      ]) {
        expect(
          () => AgentRuntimeChange.fromJson(runtimeChangeJson(directory: path)),
          throwsFormatException,
        );
      }
    },
  );

  test(
    'bound request strictly requires every original state and setting field',
    () {
      final valid = runtimeChangeJson();
      for (final key in valid.keys) {
        expect(
          () => AgentRuntimeChange.fromJson({...valid}..remove(key)),
          throwsFormatException,
        );
      }
      for (final bad in [
        {...valid, 'schema_version': '1.0'},
        {...valid, 'extra': true},
        {...valid, 'expected_revision': true},
        {...valid, 'expected_revision': 0.0},
        {...valid, 'expected_revision': -1},
        {...valid, 'expected_revision': 9007199254740991},
        {...valid, 'expected_state': 'x' * 64},
        {...valid, 'expected_instance': 'a' * 32},
        {...valid, 'idempotency_key': 'a/b'},
        {...valid, 'yolo': 1},
        {
          ...valid,
          'settings': {...runtimeSettingsJson(), 'extra': null},
        },
        {
          ...valid,
          'settings': {...runtimeSettingsJson(), 'max_steps': 8.0},
        },
        {
          ...valid,
          'settings': {...runtimeSettingsJson(), 'shell_commands': 'yes'},
        },
      ]) {
        expect(() => AgentRuntimeChange.fromJson(bad), throwsFormatException);
      }
      for (final key in runtimeSettingsJson().keys) {
        expect(
          () => AgentRuntimeChange.fromJson({
            ...valid,
            'settings': {...runtimeSettingsJson()}..remove(key),
          }),
          throwsFormatException,
        );
      }
    },
  );

  test(
    'recorded proof binds exact canonical original state and historic requested YOLO',
    () {
      final original = AgentRuntimeChange.fromJson(
        runtimeChangeJson(yolo: true, directory: './a//b/'),
      );
      final receipt = runtimeReceiptJson(original.body);
      for (final key in receipt.keys) {
        expect(
          () => AgentRuntimeReceipt.fromJson({
            ...receipt,
            key: 'wrong',
          }, original),
          throwsFormatException,
        );
        expect(
          () =>
              AgentRuntimeReceipt.fromJson({...receipt}..remove(key), original),
          throwsFormatException,
        );
      }
      final proof = runtimeLookupJson(original.body);
      for (final bad in [
        {...proof, 'mutation_fenced': false},
        {...proof, 'mutation_fenced': 1},
        {...proof, 'backend_id': runtimeBackendB},
        {...proof, 'extra': true},
        {
          ...proof,
          'request': {...original.body, 'expected_state': 'f' * 64},
        },
        {
          ...proof,
          'request': {...original.body, 'expected_instance': runtimeInstanceB},
        },
        {
          ...proof,
          'request': {...original.body, 'expected_revision': 0.0},
        },
        {
          ...proof,
          'request': {
            ...original.body,
            'settings': runtimeSettingsJson(directory: './a//b/'),
          },
        },
        {
          ...proof,
          'receipt': {
            ...receipt,
            'settings': runtimeSettingsJson(directory: './a//b/'),
          },
        },
        {
          ...proof,
          'receipt': {...receipt, 'requested_yolo': false},
        },
      ]) {
        expect(
          () => AgentRuntimeLookup.fromJson(bad, original),
          throwsFormatException,
        );
      }
      final recorded = AgentRuntimeLookup.fromJson(proof, original);
      expect(recorded.receipt!.json['requested_yolo'], isTrue);
      expect(recorded.json['request'], original.body);
      expect(
        AgentRuntimeLookup.fromJson(
          runtimeLookupJson(original.body, recorded: false, fenced: false),
          original,
        ).refused,
        isFalse,
      );
      expect(
        AgentRuntimeLookup.fromJson(
          runtimeLookupJson(original.body, recorded: false),
          original,
        ).refused,
        isTrue,
      );
    },
  );
}
