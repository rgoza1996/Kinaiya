// Independent wire oracles frozen without consulting the Dart implementation.
import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/standing_work.dart';

late Map<String, Object?> fixture;

Map<String, Object?> object(Object? value) =>
    (value as Map).cast<String, Object?>();

Map<String, Object?> copy(Object? value) =>
    object(jsonDecode(jsonEncode(value)));

Map<String, Object?> specimen(String name) => copy(fixture[name]);

Object? ordered(Object? value) {
  if (value is Map) {
    final map = object(value);
    final keys = map.keys.toList()..sort();
    return {for (final key in keys) key: ordered(map[key])};
  }
  if (value is List) return value.map(ordered).toList();
  return value;
}

List<int> canonical(Object? value) => utf8.encode(jsonEncode(ordered(value)));

String digest(String domain, Object? value) =>
    sha256.convert([...utf8.encode(domain), 0, ...canonical(value)]).toString();

void rehashReceipt(Map<String, Object?> receipt) {
  final request = object(receipt['request']);
  receipt['authorization_digest'] =
      digest('kinaiya.standing.authorization.v1', {
        'responsibility_id': request['responsibility_id'],
        'revision': receipt['revision'],
        'request': request,
      });
}

void rehashRecord(Map<String, Object?> record) {
  record['record_digest'] = digest('kinaiya.standing.record.v1', {
    'receipt': record['receipt'],
    'activation': record['activation'],
    'authorized_from_utc': record['authorized_from_utc'],
  });
}

Map<String, Object?> recordNumber(int number) {
  final record = specimen('record');
  final receipt = object(record['receipt']);
  final request = object(receipt['request']);
  request['responsibility_id'] =
      '00000000-0000-0000-0000-${number.toRadixString(16).padLeft(12, '0')}';
  request['idempotency_key'] = 'independent-$number';
  rehashReceipt(receipt);
  rehashRecord(record);
  return record;
}

void main() {
  setUpAll(() {
    fixture = object(
      jsonDecode(
        File(
          'test/fixtures/standing-owner-independent-v1.json',
        ).readAsStringSync(),
      ),
    );
  });

  test(
    'literal Python-stdlib vectors bind the complete original and record',
    () {
      final original = StandingChange.fromJson(specimen('request'));
      expect(
        sha256.convert(original.canonicalBytes).toString(),
        'ebf7fdc71eadae7ba189da2a584852cd661ab3522cef1165079b5361f569a4f1',
      );
      final receipt = StandingChangeReceipt.fromJson(
        specimen('receipt'),
        original: original,
      );
      expect(
        receipt.authorizationDigest,
        '8147807c8c1997c59db6cf2561625f28c46cfde8cdf29dc9d8f5ec4e874bc1ba',
      );
      expect(receipt.request.matches(original), isTrue);
      final record = StandingRecord.fromJson(specimen('record'));
      expect(
        record.recordDigest,
        '04e1e2d7c068d96e8a7fd1bcb314eb6639c7be601fb2fb5e23c1caaf9101a7d5',
      );
      expect(record.activation, 'armed');
      expect(record.authorizedFromUtc, '2024-01-01T00:00:01Z');
      expect(
        StandingRecord.fromJson(specimen('pending')).authorizedFromUtc,
        isNull,
      );
      final snapshot = StandingResponsibilitySnapshot.fromJson(
        specimen('snapshot'),
      );
      expect(snapshot.revision, 1);
      expect(snapshot.record!.receipt.request.matches(original), isTrue);
      final lookup = StandingChangeLookup.fromJson(
        specimen('lookup'),
        original: original,
      );
      expect(lookup.status, 'recorded');
      expect(lookup.mutationFenced, isFalse);
      expect(lookup.current.revision, 1);
      expect(lookup.current.json.containsKey('record'), isFalse);
      expect(
        StandingResponsibilityPage.fromJson(
          specimen('page'),
        ).items.single.receipt.request.matches(original),
        isTrue,
      );
    },
  );

  test('retained input is detached and recursively immutable', () {
    final input = specimen('request');
    final original = StandingChange.fromJson(input);
    final before = original.canonicalBytes.toList();
    object(input['authorization'])['objective'] = 'later caller edit';
    expect(original.canonicalBytes, before);
    expect(
      () => original.body['idempotency_key'] = 'replacement',
      throwsA(isA<UnsupportedError>()),
    );
    expect(
      () => object(original.body['authorization'])['objective'] = 'replacement',
      throwsA(isA<UnsupportedError>()),
    );
    final page = StandingResponsibilityPage.fromJson(specimen('page'));
    expect(() => page.items.clear(), throwsA(isA<UnsupportedError>()));
  });

  for (final path in [
    'root',
    'authorization',
    'task_session',
    'workspace_pin',
    'schedule',
    'delivery',
  ]) {
    test('request refuses unknown fields at $path without sanitizing them', () {
      final raw = specimen('request');
      var target = raw;
      if (path != 'root') {
        target = object(raw['authorization']);
        if (path != 'authorization') target = object(target[path]);
      }
      target['synthetic_unknown_key'] = 'marker';
      expect(() => StandingChange.fromJson(raw), throwsFormatException);
    });
  }

  for (final field in ['label', 'objective', 'enabled', 'content_scope']) {
    test('full-original receipt binding includes authorization $field', () {
      final raw = specimen('request');
      final auth = object(raw['authorization']);
      auth[field] = switch (field) {
        'enabled' => false,
        'content_scope' => 'local_only',
        _ => '${auth[field]} changed',
      };
      final different = StandingChange.fromJson(raw);
      expect(
        () => StandingChangeReceipt.fromJson(
          specimen('receipt'),
          original: different,
        ),
        throwsFormatException,
      );
      expect(
        () => StandingChangeLookup.fromJson(
          specimen('lookup'),
          original: different,
        ),
        throwsFormatException,
      );
    });
  }

  test('canonical identity preserves composed and decomposed Unicode', () {
    final one = specimen('request');
    final two = specimen('request');
    object(one['authorization'])['objective'] = 'é';
    object(two['authorization'])['objective'] = 'e\u0301';
    expect(
      StandingChange.fromJson(one).matches(StandingChange.fromJson(two)),
      isFalse,
    );
  });

  for (final bad in [true, 1.0, -1, 9007199254740991]) {
    test('next revision rejects ${bad.runtimeType} $bad', () {
      final raw = specimen('request')..['expected_revision'] = bad;
      expect(() => StandingChange.fromJson(raw), throwsFormatException);
    });
  }

  for (final field in [
    'status',
    'backend_id',
    'service_instance',
    'revision',
    'authorization_digest',
  ]) {
    test('receipt rejects inconsistent $field', () {
      final raw = specimen('receipt');
      raw[field] = switch (field) {
        'status' => 'active',
        'backend_id' => 'd' * 32,
        'service_instance' => '00000000-0000-0000-0000-000000000222',
        'revision' => 2,
        _ => '0' * 64,
      };
      expect(() => StandingChangeReceipt.fromJson(raw), throwsFormatException);
    });
  }

  for (final badClock in [
    null,
    '2024-02-31T00:00:00Z',
    '2024-01-01T00:00:01+00:00',
    '2023-01-01T00:00:00Z',
  ]) {
    test('armed record rejects invalid or out-of-grant clock $badClock', () {
      final raw = specimen('record')..['authorized_from_utc'] = badClock;
      rehashRecord(raw);
      expect(() => StandingRecord.fromJson(raw), throwsFormatException);
    });
  }

  test('pending and digest checks cannot promote a saved definition', () {
    final pending = specimen('pending')
      ..['authorized_from_utc'] = '2024-01-01T00:00:01Z';
    rehashRecord(pending);
    expect(() => StandingRecord.fromJson(pending), throwsFormatException);
    final forged = specimen('record')..['record_digest'] = '0' * 64;
    expect(() => StandingRecord.fromJson(forged), throwsFormatException);
  });

  test('snapshot absent revision is exactly zero with no record', () {
    final absent = specimen('current')..['revision'] = 0;
    expect(
      StandingResponsibilitySnapshot.fromJson({
        ...absent,
        'record': null,
      }).revision,
      0,
    );
    expect(
      () => StandingResponsibilitySnapshot.fromJson({
        ...absent,
        'record': specimen('record'),
      }),
      throwsFormatException,
    );
    expect(
      () => StandingResponsibilitySnapshot.fromJson({
        ...specimen('current'),
        'record': null,
      }),
      throwsFormatException,
    );
  });

  for (final currentRevision in [0, 1]) {
    for (final expected in [0, 5]) {
      test(
        'unobserved fence depends on native revision $currentRevision/$expected',
        () {
          final raw = specimen('lookup')
            ..['status'] = 'unobserved'
            ..['receipt'] = null
            ..['mutation_fenced'] = currentRevision > expected;
          object(raw['current'])['revision'] = currentRevision;
          object(raw['request'])['expected_revision'] = expected;
          final result = StandingChangeLookup.fromJson(raw);
          expect(result.mutationFenced, currentRevision > expected);
          raw['mutation_fenced'] = !(currentRevision > expected);
          expect(
            () => StandingChangeLookup.fromJson(raw),
            throwsFormatException,
          );
        },
      );
    }
  }

  test(
    'retirement differs from a current or unknown generation declaration',
    () {
      final raw = specimen('lookup')
        ..['status'] = 'unobserved'
        ..['receipt'] = null
        ..['request_instance'] = 'retired'
        ..['mutation_fenced'] = true;
      object(raw['current'])['instance_id'] =
          '00000000-0000-0000-0000-000000000222';
      expect(StandingChangeLookup.fromJson(raw).mutationFenced, isTrue);
      raw['request_instance'] = 'current';
      expect(() => StandingChangeLookup.fromJson(raw), throwsFormatException);
    },
  );

  test('lookup rejects the third full-record copy removed by native v4', () {
    final raw = specimen('lookup');
    object(raw['current'])['record'] = specimen('record');
    expect(() => StandingChangeLookup.fromJson(raw), throwsFormatException);
  });

  test(
    'large two-copy original proof stays valid above the old small bound',
    () {
      final raw = specimen('lookup');
      final request = object(raw['request']);
      object(request['authorization'])['objective'] = '\u0000' * 16000;
      final receipt = object(raw['receipt']);
      receipt['request'] = copy(request);
      rehashReceipt(receipt);
      expect(canonical(raw).length, greaterThan(131072));
      expect(canonical(raw).length, lessThanOrEqualTo(262144));
      expect(
        StandingChangeLookup.fromJson(
          raw,
          original: StandingChange.fromJson(request),
        ).receipt!.request.body,
        request,
      );
    },
  );

  test(
    'whole-record pages enforce ordering, cursor and the 2048-node bound',
    () {
      final page = specimen('page');
      page['items'] = [recordNumber(1), recordNumber(2)];
      page['next_cursor'] = '00000000-0000-0000-0000-000000000002';
      expect(StandingResponsibilityPage.fromJson(page).items.length, 2);
      page['next_cursor'] = '00000000-0000-0000-0000-000000000001';
      expect(
        () => StandingResponsibilityPage.fromJson(page),
        throwsFormatException,
      );
      page['next_cursor'] = null;
      page['items'] = [recordNumber(2), recordNumber(1)];
      expect(
        () => StandingResponsibilityPage.fromJson(page),
        throwsFormatException,
      );
      page['items'] = [recordNumber(1), recordNumber(1)];
      expect(
        () => StandingResponsibilityPage.fromJson(page),
        throwsFormatException,
      );
      page['items'] = List.generate(19, (index) => recordNumber(index + 1));
      expect(StandingResponsibilityPage.fromJson(page).items.length, 19);
      page['items'] = List.generate(20, (index) => recordNumber(index + 1));
      expect(
        () => StandingResponsibilityPage.fromJson(page),
        throwsFormatException,
      );
    },
  );
}
