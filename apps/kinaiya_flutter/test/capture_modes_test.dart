import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/capture_modes.dart';

Map<String, Object?> nativeCaptureFixture() =>
    jsonDecode(
          File('test/fixtures/capture_modes_native.json').readAsStringSync(),
        )
        as Map<String, Object?>;

void main() {
  test(
    'actual native snapshots, receipts and canonical lookup digests agree',
    () {
      final f = nativeCaptureFixture();
      final before = CaptureModeSnapshot.fromJson(f['snapshot']);
      expect(before.selectedMode, CaptureMode.rollingWindows);
      final original = CaptureModeChange.fromJson(f['request']);
      expect(original.matchesSnapshot(before), isTrue);
      final receipt = CaptureModeReceipt.fromJson(f['receipt'], original);
      expect(receipt.revision, before.revision + 1);
      expect(receipt.selection.mode, CaptureMode.legacySilence);
      for (final name in ['lookup_saved', 'lookup_saved_after_replacement']) {
        final lookup = CaptureModeLookup.fromJson(f[name], original);
        expect(lookup.saved, isTrue);
        expect(lookup.fenced, isFalse);
      }
      final absent = CaptureModeLookup.fromJson(f['lookup_before'], original);
      expect(absent.saved, isFalse);
      expect(absent.fenced, isFalse);
      final unobserved = CaptureModeChange.fromJson(f['unobserved_request']);
      expect(
        CaptureModeLookup.fromJson(f['lookup_fenced'], unobserved).fenced,
        isTrue,
      );
      for (final value in f['modes'] as List) {
        final row = value as Map<String, Object?>;
        final request = CaptureModeChange.fromJson(row['request']);
        expect(
          request.matchesSnapshot(
            CaptureModeSnapshot.fromJson(row['snapshot']),
          ),
          isTrue,
        );
        final receipt = CaptureModeReceipt.fromJson(row['receipt'], request);
        final after = CaptureModeSnapshot.fromJson(row['after']);
        expect(after.selectedMode, receipt.selection.mode);
      }
      expect(CaptureMode.values.length, 3);
    },
  );

  test(
    'snapshots reject unknown modes, malformed metadata and false physical capture proof',
    () {
      final valid = nativeCaptureFixture()['snapshot'] as Map<String, Object?>;
      for (final bad in [
        {...valid, 'contract': 'converse_capture.v2'},
        {...valid, 'extra': null},
        {...valid}..remove('adaptive_live_enabled'),
        {...valid, 'capture_state': 'capturing'},
        {...valid, 'change_boundary': 'immediate'},
        {...valid, 'writable': 1},
        {...valid, 'authority_generation': 1.5},
        {...valid, 'authority_generation': 9007199254740992},
        {...valid, 'revision': 1001},
        {...valid, 'state_token': 'F' * 64},
        {...valid, 'default_mode': 'converse'},
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
        expect(() => CaptureModeSnapshot.fromJson(bad), throwsFormatException);
      }
      final unknownHost = CaptureModeSnapshot.fromJson({
        ...valid,
        'default_mode': null,
        'selected_mode': null,
      });
      expect(unknownHost.selectedMode, isNull);
    },
  );

  test('requests detach exact original authority and nested selection', () {
    final raw = nativeCaptureFixture()['request'] as Map<String, Object?>;
    final original = CaptureModeChange.fromJson(raw);
    (raw['selection'] as Map<String, Object?>)['mode'] = 'core';
    raw['authority_generation'] = 999;
    expect(original.selection.mode, CaptureMode.legacySilence);
    expect(original.body['authority_generation'], isNot(999));
    expect(() => original.body['state_token'] = 'x', throwsUnsupportedError);
    expect(
      () =>
          (original.body['selection'] as Map<String, Object?>)['mode'] = 'core',
      throwsUnsupportedError,
    );
    expect(CaptureModeChange.fromJson(original.body).digest, original.digest);
  });

  test(
    'positive receipt binds every original observation and exact selection',
    () {
      final f = nativeCaptureFixture();
      final original = CaptureModeChange.fromJson(f['request']);
      final valid = f['receipt'] as Map<String, Object?>;
      for (final bad in [
        {...valid, 'backend_id': 'a' * 32},
        {...valid, 'authority_epoch': 'b' * 32},
        {...valid, 'authority_generation': original.generation + 1},
        {...valid, 'revision': original.revision + 2},
        {...valid, 'idempotency_key': 'different'},
        {...valid, 'default_mode_observed': null},
        {...valid, 'selected_mode_observed': 'core'},
        {
          ...valid,
          'selection': {'source': 'saved', 'mode': 'all'},
        },
        {...valid, 'change_boundary': 'already_running'},
      ]) {
        expect(
          () => CaptureModeReceipt.fromJson(bad, original),
          throwsFormatException,
        );
      }
    },
  );

  test(
    'lookup cannot substitute a different request or contradict saved proof',
    () {
      final f = nativeCaptureFixture();
      final original = CaptureModeChange.fromJson(f['request']);
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
          () => CaptureModeLookup.fromJson(bad, original),
          throwsFormatException,
        );
      }
    },
  );
}
