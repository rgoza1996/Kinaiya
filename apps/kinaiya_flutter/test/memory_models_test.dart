import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/memory_models.dart';

Map<String, Object?> bindingJson() => {
  'schema_version': '1.0',
  'session_id': 's',
  'thread_id': 't',
  'epoch': 1,
};
Map<String, Object?> stateJson({int count = 0}) => {
  ...bindingJson(),
  'enabled': false,
  'revision': 0,
  'node_count': count,
  'status': 'ready',
};
Map<String, Object?> useJson() => {
  ...bindingJson(),
  'runtime_id': 'runtime',
  'use_generation': 0,
  'enabled': false,
  'implementation_id': null,
  'selector_ids': <String>[],
  'pending': false,
  'notice': 'disabled',
};
Map<String, Object?> summaryJson(int n) => {
  'schema_version': '1.0',
  'node_id': 'n$n',
  'ordinal': n,
  'kind': 'assertion',
  'state': 'active',
  'preview': 'Saved 🐈 note',
  'preview_truncated': false,
};
Map<String, Object?> nodeJson() => {
  ...bindingJson(),
  'node_id': 'n1',
  'ordinal': 1,
  'kind': 'assertion',
  'source_event_id': 'event',
  'source_revision': 1,
  'source_sequence': 1,
  'prerequisite_ids': <String>[],
  'body': 'A public synthetic note.',
  'fact': {'schema_version': '1.0', 'key': 'public.name', 'value': 'Cat'},
  'state': 'active',
  'content_scope': 'local_only',
  'provenance': 'owner_curated',
};

void main() {
  test(
    'binding validates, detaches wire output and compares complete scope',
    () {
      final b = MemoryBinding('s', 't', 1);
      expect(b, MemoryBinding.fromJson(bindingJson()));
      expect(b.hashCode, MemoryBinding('s', 't', 1).hashCode);
      expect(b, isNot(MemoryBinding('s', 't', 2)));
      b.toJson()['session_id'] = 'other';
      expect(b.sessionId, 's');
      for (final id in ['', 's x', 's\n', 'é', 'x' * 129]) {
        expect(() => MemoryBinding(id, 't', 1), throwsFormatException);
      }
      for (final n in [0, -1, memoryMaxSafeInteger + 1]) {
        expect(() => MemoryBinding('s', 't', n), throwsFormatException);
      }
    },
  );
  test(
    'strict namespace fields and integer types, with honest diagnostic count',
    () {
      expect(
        MemoryNamespaceState.fromJson(stateJson(count: 257)).nodeCount,
        257,
      );
      expect(
        MemoryNamespaceState.fromJson({
          ...stateJson(),
          'revision': memoryMaxSafeInteger,
        }).revision,
        memoryMaxSafeInteger,
      );
      for (final bad in [
        null,
        [],
        {...stateJson()}..remove('schema_version'),
        {...stateJson(), 'schema_version': '2.0'},
        {...stateJson(), 'extra': 1},
        {...stateJson(), 'enabled': 1},
        {...stateJson(), 'revision': 1.0},
        {...stateJson(), 'revision': memoryMaxSafeInteger + 1},
        {...stateJson(), 'node_count': -1},
        {...stateJson(), 'status': 'good'},
      ]) {
        expect(() => MemoryNamespaceState.fromJson(bad), throwsFormatException);
      }
    },
  );
  test('complete 256 catalog is ordered, distinct and immutable', () {
    final rows = List.generate(256, (i) => summaryJson(i + 1));
    final c = MemoryCatalog.fromJson({
      'schema_version': '1.0',
      'state': stateJson(count: 256),
      'entries': rows,
    });
    rows.clear();
    expect(c.entries.length, 256);
    expect(c.binding, MemoryBinding('s', 't', 1));
    expect(() => c.entries.clear(), throwsUnsupportedError);
    for (final bad in [
      {'schema_version': '1.0', 'state': stateJson(count: 1), 'entries': []},
      {
        'schema_version': '1.0',
        'state': stateJson(count: 2),
        'entries': [summaryJson(1), summaryJson(1)],
      },
      {
        'schema_version': '1.0',
        'state': stateJson(count: 1),
        'entries': [summaryJson(2)],
      },
      {
        'schema_version': '1.0',
        'state': stateJson(count: 257),
        'entries': List.generate(257, (i) => summaryJson(i + 1)),
      },
    ]) {
      expect(() => MemoryCatalog.fromJson(bad), throwsFormatException);
    }
  });
  test('Unicode and redaction bounds preserve exact note text', () {
    final good = '🦉' * 32;
    expect(utf8.encode(good).length, 128);
    expect(
      MemorySummary.fromJson({...summaryJson(1), 'preview': good}).preview,
      good,
    );
    expect(
      MemorySummary.fromJson({
        ...summaryJson(1),
        'state': 'forgotten',
        'preview': null,
      }).preview,
      null,
    );
    for (final text in [
      '🦉' * 33,
      '',
      '\u0000',
      '\u007f',
      '\u0085',
      String.fromCharCode(0xd800),
    ]) {
      expect(
        () => MemorySummary.fromJson({...summaryJson(1), 'preview': text}),
        throwsFormatException,
      );
    }
    expect(
      () => MemorySummary.fromJson({...summaryJson(1), 'state': 'forgotten'}),
      throwsFormatException,
    );
    expect(
      () => MemorySummary.fromJson({
        ...summaryJson(1),
        'state': 'forgotten',
        'preview': null,
        'preview_truncated': true,
      }),
      throwsFormatException,
    );
  });
  test(
    'all native node fields and typed facts retained, detached and strict',
    () {
      final raw = nodeJson();
      final parents = <String>['p'];
      raw['prerequisite_ids'] = parents;
      final node = MemoryNode.fromJson(raw);
      parents.clear();
      expect(node.prerequisiteIds, ['p']);
      expect(() => node.prerequisiteIds.add('x'), throwsUnsupportedError);
      expect(node.sourceSequence, 1);
      expect(node.sourceRevision, 1);
      expect(node.sourceEventId, 'event');
      expect(node.provenance, 'owner_curated');
      expect(node.fact!.value, 'Cat');
      for (final v in [
        null,
        true,
        false,
        -memoryMaxSafeInteger,
        memoryMaxSafeInteger,
        '',
      ]) {
        expect(
          MemoryFact.fromJson({
            'schema_version': '1.0',
            'key': 'a/b:c-d',
            'value': v,
          }).value,
          v,
        );
      }
      for (final v in [
        1.0,
        double.nan,
        double.infinity,
        <String>[],
        <String, String>{},
        memoryMaxSafeInteger + 1,
        'x' * 513,
      ]) {
        expect(
          () => MemoryFact.fromJson({
            'schema_version': '1.0',
            'key': 'a',
            'value': v,
          }),
          throwsFormatException,
        );
      }
      for (final key in ['a\n', 'é', '']) {
        expect(
          () => MemoryFact.fromJson({
            'schema_version': '1.0',
            'key': key,
            'value': null,
          }),
          throwsFormatException,
        );
      }
    },
  );
  test('node consistency, source authority and body bounds', () {
    expect(
      MemoryNode.fromJson({
        ...nodeJson(),
        'body': '\t${'a' * 2047}',
      }).body!.length,
      2048,
    );
    expect(
      MemoryNode.fromJson({
        ...nodeJson(),
        'state': 'forgotten',
        'body': null,
        'fact': null,
      }).body,
      null,
    );
    expect(
      MemoryNode.fromJson({
        ...nodeJson(),
        'kind': 'inference',
        'fact': null,
        'prerequisite_ids': ['p'],
      }).kind,
      'inference',
    );
    for (final change in [
      {'body': 'x' * 2049},
      {'body': '  '},
      {'body': '\u0000'},
      {'body': null},
      {'fact': null},
      {'kind': 'inference', 'fact': null},
      {'content_scope': 'public_synthetic'},
      {'provenance': 'model'},
      {'source_revision': 0},
      {'source_sequence': 0},
      {
        'prerequisite_ids': ['p', 'p'],
      },
      {'prerequisite_ids': List.generate(9, (i) => 'p$i')},
      {'state': 'forgotten'},
      {'unknown': false},
    ]) {
      expect(
        () => MemoryNode.fromJson({...nodeJson(), ...change}),
        throwsFormatException,
      );
    }
    expect(
      () => MemoryInspection.fromJson({
        'schema_version': '1.0',
        'state': stateJson(),
        'node': {...nodeJson(), 'epoch': 2},
      }),
      throwsFormatException,
    );
  });
  test(
    'use state allows retained disabled selection but never malformed pins',
    () {
      final ids = ['n1'];
      final v = MemoryUseState.fromJson({
        ...useJson(),
        'selector_ids': ids,
        'implementation_id': 'korina-dag-v1',
      });
      ids.clear();
      expect(v.selectorIds, ['n1']);
      expect(() => v.selectorIds.clear(), throwsUnsupportedError);
      for (final change in [
        {'enabled': true},
        {'runtime_id': ''},
        {'use_generation': -1},
        {'pending': 'yes'},
        {'notice': 'active'},
        {
          'selector_ids': ['n', 'n'],
        },
        {'extra': true},
      ]) {
        expect(
          () => MemoryUseState.fromJson({...useJson(), ...change}),
          throwsFormatException,
        );
      }
    },
  );
}
