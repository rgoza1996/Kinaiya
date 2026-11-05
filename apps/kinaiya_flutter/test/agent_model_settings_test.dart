import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/agent_model_settings.dart';

const backendA = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
const backendB = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';
const instanceA = '11111111111111111111111111111111';
const instanceB = '22222222222222222222222222222222';

Map<String, Object?> modelSnapshotJson({
  Map<String, Object?>? writeAuthority,
}) => {
  'schema_version': '1.0',
  'revision': 0,
  'change_boundary': 'new_tasks',
  'default': {'profile_id': 'host-default', 'reasoning': null},
  'harnesses': <String, Object?>{},
  'profiles': [
    {
      'id': 'host-default',
      'selectable': true,
      'auxiliary_selectable': true,
      'provider': {
        'provider': 'synthetic',
        'model': 'synthetic-local',
        'local': true,
      },
      'reasoning': null,
      'reasoning_options': ['high'],
    },
  ],
  if (writeAuthority != null) 'write_authority': writeAuthority,
};

Map<String, Object?> authorityJson({
  String backend = backendA,
  String instance = instanceA,
  bool writable = true,
}) => {
  'mutation_version': '1.1',
  'backend_id': backend,
  'instance_id': instance,
  'writable': writable,
};
AgentModelAuthority authority({
  String backend = backendA,
  String instance = instanceA,
  bool writable = true,
}) => AgentModelAuthority.fromJson(
  authorityJson(backend: backend, instance: instance, writable: writable),
);
Map<String, Object?> changeJson({
  String key = 'synthetic-key',
  String backend = backendA,
  String instance = instanceA,
  int revision = 0,
  String? harness,
  String? profile = 'host-default',
  String? reasoning,
  String? purpose,
}) => {
  'schema_version': '1.1',
  'expected_backend': backend,
  'expected_instance': instance,
  'expected_revision': revision,
  'idempotency_key': key,
  'harness_version': harness,
  'profile_id': profile,
  'reasoning': reasoning,
  if (purpose != null) 'auxiliary_task': purpose,
};
Map<String, Object?> receiptJson(Map<String, Object?> request) => {
  'schema_version': '1.1',
  'backend_id': request['expected_backend'],
  'service_instance': request['expected_instance'],
  'idempotency_key': request['idempotency_key'],
  'revision': (request['expected_revision'] as int) + 1,
  'harness_version': request['harness_version'],
  'binding': request['profile_id'] == null
      ? null
      : {
          'profile_id': request['profile_id'],
          'reasoning': request['reasoning'],
        },
  'change_boundary': 'new_tasks',
  if (request.containsKey('auxiliary_task'))
    'auxiliary_task': request['auxiliary_task'],
};
Map<String, Object?> lookupJson(
  Map<String, Object?> request, {
  bool recorded = true,
  bool fenced = true,
}) => {
  'schema_version': '1.1',
  'backend_id': request['expected_backend'],
  'request': {...request},
  'status': recorded ? 'recorded' : 'unobserved',
  'receipt': recorded ? receiptJson(request) : null,
  'mutation_fenced': fenced,
};

void main() {
  test(
    'legacy and unsupported metadata remain readable but cannot authorize writes',
    () {
      expect(
        AgentModelsSnapshot.fromJson(modelSnapshotJson()).writeAuthority,
        isNull,
      );
      expect(
        AgentModelsSnapshot.fromJson({
          ...modelSnapshotJson(),
          'write_authority': {...authorityJson(), 'mutation_version': 'next'},
        }).writeAuthority,
        isNull,
      );
      final bound = AgentModelsSnapshot.fromJson({
        ...modelSnapshotJson(),
        'write_authority': authorityJson(),
      });
      expect(bound.writeAuthority!.backendId, backendA);
      expect(bound.writeAuthority!.writable, isTrue);
      expect(authority(writable: false).writable, isFalse);
      for (final field in ['backend_id', 'instance_id', 'writable']) {
        expect(
          () => AgentModelAuthority.fromJson({...authorityJson(), field: null}),
          throwsFormatException,
        );
      }
      expect(
        () => AgentModelAuthority.fromJson({...authorityJson(), 'extra': true}),
        throwsFormatException,
      );
    },
  );

  test(
    'canonical primary, harness inheritance and all purpose bodies remain immutable',
    () {
      for (final purpose in <String?>[null, ...agentModelPurposes]) {
        final source = changeJson(
          purpose: purpose,
          harness: 'seed-v1',
          profile: null,
        );
        final request = AgentModelChange.fromJson(source);
        source['idempotency_key'] = 'changed';
        expect(request.key, 'synthetic-key');
        expect(
          () => request.body['reasoning'] = 'high',
          throwsUnsupportedError,
        );
        expect(request.body.containsKey('auxiliary_task'), purpose != null);
        final receipt = AgentModelReceipt.fromJson(
          receiptJson(request.body),
          request,
        );
        expect(receipt.json['binding'], isNull);
        expect(
          AgentModelLookup.fromJson(lookupJson(request.body), request).receipt,
          isNotNull,
        );
      }
    },
  );

  test(
    'strict canonical grammar refuses omissions, extras, unsafe numbers and inferred authority',
    () {
      final bad = <Map<String, Object?>>[
        {...changeJson(), 'schema_version': '1.0'},
        {...changeJson(), 'expected_revision': 0.0},
        {...changeJson(), 'expected_revision': true},
        {...changeJson(), 'expected_revision': agentModelMaxRevision + 1},
        {...changeJson(), 'expected_backend': backendA.toUpperCase()},
        {...changeJson(), 'expected_instance': ''},
        {...changeJson(), 'auxiliary_task': null},
        {...changeJson(), 'extra': null},
        {...changeJson(), 'profile_id': '\ud800'},
        {...changeJson(), 'idempotency_key': 'a/b'},
        {...changeJson()}..remove('harness_version'),
      ];
      for (final body in bad) {
        expect(() => AgentModelChange.fromJson(body), throwsFormatException);
      }
      expect(
        AgentModelChange.fromJson(changeJson(profile: '模型')).body['profile_id'],
        '模型',
      );
    },
  );

  test(
    'recorded proof binds backend, original instance, full request and exact receipt',
    () {
      final request = AgentModelChange.fromJson(
        changeJson(purpose: 'analysis', reasoning: 'high'),
      );
      final valid = receiptJson(request.body);
      for (final field in [
        'backend_id',
        'service_instance',
        'idempotency_key',
        'revision',
        'harness_version',
        'binding',
        'auxiliary_task',
        'schema_version',
        'change_boundary',
      ]) {
        expect(
          () => AgentModelReceipt.fromJson({...valid, field: 'wrong'}, request),
          throwsFormatException,
        );
      }
      for (final raw in [
        {...lookupJson(request.body), 'mutation_fenced': false},
        {...lookupJson(request.body), 'mutation_fenced': 1},
        {...lookupJson(request.body), 'backend_id': backendB},
        {
          ...lookupJson(request.body),
          'request': {...request.body, 'expected_instance': instanceB},
        },
        {
          ...lookupJson(request.body),
          'request': {...request.body, 'reasoning': null},
        },
        {...lookupJson(request.body), 'extra': true},
      ]) {
        expect(
          () => AgentModelLookup.fromJson(raw, request),
          throwsFormatException,
        );
      }
      expect(
        AgentModelLookup.fromJson(
          lookupJson(request.body, recorded: false, fenced: false),
          request,
        ).refused,
        isFalse,
      );
      expect(
        AgentModelLookup.fromJson(
          lookupJson(request.body, recorded: false),
          request,
        ).refused,
        isTrue,
      );
    },
  );
}
