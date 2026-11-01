import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/agent_control.dart';

const controlBackendA = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
const controlBackendB = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';
const controlInstanceA = '11111111-1111-1111-1111-111111111111';
const controlInstanceB = '22222222-2222-2222-2222-222222222222';

Map<String, Object?> modeChangeJson({
  String backend = controlBackendA,
  String instance = controlInstanceA,
  int revision = 0,
  String mode = 'off',
  String key = 'synthetic-mode-key',
}) => {
  'schema_version': '1.0',
  'idempotency_key': key,
  'expected_backend': backend,
  'expected_instance': instance,
  'expected_revision': revision,
  'mode': mode,
  'active_work_policy': 'finish_already_accepted',
};

Map<String, Object?> controlStateJson({
  String backend = controlBackendA,
  String instance = controlInstanceA,
  int revision = 0,
  String mode = 'on_demand',
  bool writable = true,
  bool standing = false,
}) => {
  'schema_version': '1.0',
  'backend_id': backend,
  'instance_id': instance,
  'revision': revision,
  'mode': mode,
  'active_work_policy': 'finish_already_accepted',
  'control_writable': writable,
  'available_modes': ['off', 'on_demand', if (standing) 'autonomous'],
  'standing_work_available': standing,
};

AgentControlSnapshot controlState({
  String backend = controlBackendA,
  String instance = controlInstanceA,
  int revision = 0,
  String mode = 'on_demand',
  bool writable = true,
  bool standing = false,
}) => AgentControlSnapshot.fromJson(
  controlStateJson(
    backend: backend,
    instance: instance,
    revision: revision,
    mode: mode,
    writable: writable,
    standing: standing,
  ),
);

Map<String, Object?> modeReceiptJson(Map<String, Object?> original) => {
  'schema_version': '1.0',
  'status': 'mode_saved',
  'backend_id': original['expected_backend'],
  'service_instance': original['expected_instance'],
  'revision': (original['expected_revision'] as int) + 1,
  'request': {...original},
};

Map<String, Object?> modeLookupJson(
  Map<String, Object?> original, {
  bool recorded = true,
  Map<String, Object?>? state,
}) {
  final observed =
      state ??
      controlStateJson(
        backend: original['expected_backend'] as String,
        instance: original['expected_instance'] as String,
        revision: (original['expected_revision'] as int) + (recorded ? 1 : 0),
        mode: recorded ? original['mode'] as String : 'on_demand',
      );
  return {
    'schema_version': '1.0',
    'status': recorded ? 'recorded' : 'unobserved',
    'request': {...original},
    'state': observed,
    'receipt': recorded ? modeReceiptJson(original) : null,
    'mutation_fenced':
        recorded ||
        observed['instance_id'] != original['expected_instance'] ||
        (observed['revision'] as int) > (original['expected_revision'] as int),
  };
}

void main() {
  test('all seven original fields are immutable, detached and bounded', () {
    final raw = modeChangeJson();
    final original = AgentModeChange.fromJson(raw);
    raw['mode'] = 'autonomous';
    expect(original.mode, AgentMode.off);
    expect(original.body.length, 7);
    expect(original.body, modeChangeJson());
    expect(() => original.body['mode'] = 'on_demand', throwsUnsupportedError);
    expect(
      original.matches(AgentModeChange.fromJson(modeChangeJson())),
      isTrue,
    );
    expect(
      original.matches(AgentModeChange.fromJson(modeChangeJson(key: 'other'))),
      isFalse,
    );
    expect(
      AgentModeChange.fromJson(
        modeChangeJson(key: 'a' * 128, revision: 9007199254740990),
      ).revision,
      9007199254740990,
    );
  });

  test(
    'request rejects missing/extra fields, coercion, unsafe IDs and revisions',
    () {
      final invalid = <Map<String, Object?>>[
        {...modeChangeJson(), 'extra': null},
        {...modeChangeJson(), 'schema_version': '1.1'},
        {...modeChangeJson(), 'active_work_policy': 'cancel'},
        {...modeChangeJson(), 'expected_revision': true},
        {...modeChangeJson(), 'expected_revision': 0.0},
        modeChangeJson(revision: -1),
        modeChangeJson(revision: 9007199254740991),
        modeChangeJson(backend: 'a' * 31),
        modeChangeJson(backend: controlBackendA.toUpperCase()),
        modeChangeJson(instance: '1' * 32),
        modeChangeJson(instance: '$controlInstanceA\n'),
        modeChangeJson(key: 'a' * 129),
        modeChangeJson(key: 'private\n'),
        modeChangeJson(key: 'private\u0000'),
        modeChangeJson(key: ''),
        modeChangeJson(mode: 'auto'),
      ];
      for (final field in modeChangeJson().keys) {
        invalid.add({...modeChangeJson()}..remove(field));
      }
      for (final raw in invalid) {
        expect(() => AgentModeChange.fromJson(raw), throwsFormatException);
      }
    },
  );

  test(
    'state keeps truthful persisted autonomy and exact capability order',
    () {
      final historical = controlState(mode: 'autonomous');
      expect(historical.mode, AgentMode.autonomous);
      expect(historical.standingWorkAvailable, isFalse);
      expect(historical.availableModes, [AgentMode.off, AgentMode.onDemand]);
      expect(
        () => historical.availableModes.add(AgentMode.autonomous),
        throwsUnsupportedError,
      );
      expect(
        () => (historical.json['available_modes'] as List).add('autonomous'),
        throwsUnsupportedError,
      );
      expect(controlState(standing: true).availableModes, AgentMode.values);
      expect(
        controlState(revision: 9007199254740991).revision,
        9007199254740991,
      );
      for (final raw in [
        {...controlStateJson(), 'control_writable': 1},
        {...controlStateJson(), 'standing_work_available': 'false'},
        {
          ...controlStateJson(),
          'available_modes': ['on_demand', 'off'],
        },
        {
          ...controlStateJson(),
          'available_modes': ['off', 'on_demand', 'autonomous'],
        },
        {
          ...controlStateJson(standing: true),
          'available_modes': ['off', 'on_demand'],
        },
        {...controlStateJson(), 'revision': 9007199254740992},
        {...controlStateJson(), 'extra': null},
      ]) {
        expect(() => AgentControlSnapshot.fromJson(raw), throwsFormatException);
      }
      for (final field in controlStateJson().keys) {
        expect(
          () => AgentControlSnapshot.fromJson(
            {...controlStateJson()}..remove(field),
          ),
          throwsFormatException,
        );
      }
    },
  );

  test(
    'receipt binds every original field and cannot rewrite its nested proof',
    () {
      final original = AgentModeChange.fromJson(modeChangeJson());
      final raw = modeReceiptJson(original.body);
      final receipt = AgentModeReceipt.fromJson(raw, original);
      (raw['request'] as Map)['mode'] = 'on_demand';
      expect(receipt.request, same(original));
      expect(receipt.json['request'], original.body);
      expect(
        () => (receipt.json['request'] as Map)['mode'] = 'autonomous',
        throwsUnsupportedError,
      );
      for (final changed in [
        modeChangeJson(key: 'different'),
        modeChangeJson(backend: controlBackendB),
        modeChangeJson(instance: controlInstanceB),
        modeChangeJson(revision: 1),
        modeChangeJson(mode: 'on_demand'),
        {...modeChangeJson(), 'active_work_policy': 'cancel'},
        {...modeChangeJson(), 'schema_version': '1.1'},
      ]) {
        expect(
          () => AgentModeReceipt.fromJson({
            ...modeReceiptJson(original.body),
            'request': changed,
          }, original),
          throwsFormatException,
        );
      }
      for (final changed in [
        {'backend_id': controlBackendB},
        {'service_instance': controlInstanceB},
        {'revision': 2},
        {'revision': 1.0},
        {'status': 'saved'},
        {'extra': null},
      ]) {
        expect(
          () => AgentModeReceipt.fromJson({
            ...modeReceiptJson(original.body),
            ...changed,
          }, original),
          throwsFormatException,
        );
      }
    },
  );

  test(
    'lookup only accepts exact proof and consistent serialized native fencing',
    () {
      final original = AgentModeChange.fromJson(modeChangeJson());
      final positive = AgentModeLookupResult.fromJson(
        modeLookupJson(original.body),
        original,
      );
      expect(positive.receipt, isNotNull);
      expect(positive.refused, isFalse);
      expect(
        AgentModeLookupResult.fromJson(
          modeLookupJson(original.body, recorded: false),
          original,
        ).refused,
        isFalse,
      );
      for (final state in [
        controlStateJson(instance: controlInstanceB, writable: false),
        controlStateJson(revision: 1),
      ]) {
        final negative = AgentModeLookupResult.fromJson(
          modeLookupJson(original.body, recorded: false, state: state),
          original,
        );
        expect(negative.refused, isTrue);
      }
      // Read-only or capability loss alone are not permanent fences.
      expect(
        AgentModeLookupResult.fromJson(
          modeLookupJson(
            original.body,
            recorded: false,
            state: controlStateJson(writable: false),
          ),
          original,
        ).mutationFenced,
        isFalse,
      );
      for (final invalid in [
        {...modeLookupJson(original.body), 'mutation_fenced': false},
        {...modeLookupJson(original.body), 'mutation_fenced': 1},
        {...modeLookupJson(original.body), 'receipt': null},
        {
          ...modeLookupJson(original.body),
          'request': modeChangeJson(key: 'foreign'),
        },
        {
          ...modeLookupJson(original.body),
          'state': controlStateJson(backend: controlBackendB),
        },
        {
          ...modeLookupJson(original.body),
          'state': controlStateJson(revision: 0, mode: 'off'),
        },
        {
          ...modeLookupJson(original.body),
          'state': controlStateJson(revision: 1, mode: 'on_demand'),
        },
        {
          ...modeLookupJson(original.body, recorded: false),
          'mutation_fenced': true,
        },
        {
          ...modeLookupJson(original.body, recorded: false),
          'receipt': modeReceiptJson(original.body),
        },
        {
          ...modeLookupJson(
            original.body,
            recorded: false,
            state: controlStateJson(instance: controlInstanceB),
          ),
          'mutation_fenced': false,
        },
      ]) {
        expect(
          () => AgentModeLookupResult.fromJson(invalid, original),
          throwsFormatException,
        );
      }
      final later = AgentModeLookupResult.fromJson(
        modeLookupJson(
          original.body,
          state: controlStateJson(revision: 2, mode: 'on_demand'),
        ),
        original,
      );
      expect(later.receipt?.request.mode, AgentMode.off);
      expect(later.state.mode, AgentMode.onDemand);
    },
  );
}
