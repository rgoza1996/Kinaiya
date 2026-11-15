import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/core_subsystems.dart';

Map<String, Object?> nativeCoreFixture() =>
    jsonDecode(
          File('test/fixtures/core_subsystems_native.json').readAsStringSync(),
        )
        as Map<String, Object?>;

void main() {
  test(
    'actual native snapshots, receipts and canonical lookup digests agree',
    () {
      final f = nativeCoreFixture();
      final before = CoreSubsystemSnapshot.fromJson(f['snapshot']);
      expect(before.selectedMode, CoreSubsystemMode.agent);
      expect(before.startupMode, CoreSubsystemMode.agent);
      final original = CoreSubsystemChange.fromJson(f['request']);
      expect(original.matchesSnapshot(before), isTrue);
      final receipt = CoreSubsystemReceipt.fromJson(f['receipt'], original);
      expect(receipt.revision, before.revision + 1);
      expect(receipt.selection.mode, CoreSubsystemMode.converseDecisions);
      for (final name in ['lookup_saved', 'lookup_saved_after_replacement']) {
        final lookup = CoreSubsystemLookup.fromJson(f[name], original);
        expect(lookup.saved, isTrue);
        expect(lookup.fenced, isFalse);
      }
      final absent = CoreSubsystemLookup.fromJson(f['lookup_before'], original);
      expect(absent.saved, isFalse);
      expect(absent.fenced, isFalse);
      final unobserved = CoreSubsystemChange.fromJson(f['unobserved_request']);
      expect(
        CoreSubsystemLookup.fromJson(f['lookup_fenced'], unobserved).fenced,
        isTrue,
      );
      for (final value in f['modes'] as List) {
        final row = value as Map<String, Object?>;
        final request = CoreSubsystemChange.fromJson(row['request']);
        expect(
          request.matchesSnapshot(
            CoreSubsystemSnapshot.fromJson(row['snapshot']),
          ),
          isTrue,
        );
        final receipt = CoreSubsystemReceipt.fromJson(row['receipt'], request);
        final after = CoreSubsystemSnapshot.fromJson(row['after']);
        expect(after.selectedMode, receipt.selection.mode);
        expect(after.startupMode, CoreSubsystemMode.agent);
      }
      expect(CoreSubsystemMode.values.length, 6);
    },
  );

  test(
    'snapshots reject invalid graphs, unknown fields and false running proof',
    () {
      final valid = nativeCoreFixture()['snapshot'] as Map<String, Object?>;
      for (final bad in [
        {...valid, 'contract': 'core_subsystems.v2'},
        {...valid, 'extra': null},
        {...valid}..remove('startup_mode'),
        {...valid, 'running_state': 'ready'},
        {...valid, 'change_boundary': 'immediate'},
        {...valid, 'writable': 1},
        {...valid, 'authority_generation': 1.5},
        {...valid, 'authority_generation': 9007199254740992},
        {...valid, 'revision': 1001},
        {...valid, 'state_token': 'F' * 64},
        {...valid, 'host_mode': 'converse'},
        {...valid, 'selected_mode': 'all'},
        {
          ...valid,
          'selection': {'source': 'saved', 'mode': null},
        },
        {
          ...valid,
          'selection': {'source': 'host', 'mode': 'agent'},
        },
        {
          ...valid,
          'selection': {'source': 'saved', 'mode': 'converse-agent'},
        },
      ]) {
        expect(
          () => CoreSubsystemSnapshot.fromJson(bad),
          throwsFormatException,
        );
      }
      final unknownHost = CoreSubsystemSnapshot.fromJson({
        ...valid,
        'host_mode': null,
        'selected_mode': null,
        'startup_mode': null,
      });
      expect(unknownHost.selectedMode, isNull);
      expect(unknownHost.startupMode, isNull);
    },
  );

  test('requests detach exact original authority and nested selection', () {
    final raw = nativeCoreFixture()['request'] as Map<String, Object?>;
    final original = CoreSubsystemChange.fromJson(raw);
    (raw['selection'] as Map<String, Object?>)['mode'] = 'core';
    raw['authority_generation'] = 999;
    expect(original.selection.mode, CoreSubsystemMode.converseDecisions);
    expect(original.body['authority_generation'], isNot(999));
    expect(() => original.body['state_token'] = 'x', throwsUnsupportedError);
    expect(
      () =>
          (original.body['selection'] as Map<String, Object?>)['mode'] = 'core',
      throwsUnsupportedError,
    );
    expect(CoreSubsystemChange.fromJson(original.body).digest, original.digest);
  });

  test(
    'positive receipt binds every original observation and exact selection',
    () {
      final f = nativeCoreFixture();
      final original = CoreSubsystemChange.fromJson(f['request']);
      final valid = f['receipt'] as Map<String, Object?>;
      for (final bad in [
        {...valid, 'backend_id': 'a' * 32},
        {...valid, 'authority_epoch': 'b' * 32},
        {...valid, 'authority_generation': original.generation + 1},
        {...valid, 'revision': original.revision + 2},
        {...valid, 'idempotency_key': 'different'},
        {...valid, 'host_mode_observed': null},
        {...valid, 'selected_mode_observed': 'core'},
        {
          ...valid,
          'selection': {'source': 'saved', 'mode': 'all'},
        },
        {...valid, 'change_boundary': 'already_running'},
      ]) {
        expect(
          () => CoreSubsystemReceipt.fromJson(bad, original),
          throwsFormatException,
        );
      }
    },
  );

  test(
    'lookup cannot substitute a different request or contradict saved proof',
    () {
      final f = nativeCoreFixture();
      final original = CoreSubsystemChange.fromJson(f['request']);
      final valid = f['lookup_saved'] as Map<String, Object?>;
      for (final bad in [
        {...valid, 'request_sha256': 'a' * 64},
        {...valid, 'backend_id': 'b' * 32},
        {...valid, 'outcome': 'saved', 'receipt': null},
        {...valid, 'mutation_fenced': true},
        {...valid, 'outcome': 'unobserved'},
        {...valid, 'extra': false},
      ]) {
        expect(
          () => CoreSubsystemLookup.fromJson(bad, original),
          throwsFormatException,
        );
      }
    },
  );
}
