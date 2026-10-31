import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/models.dart';

Map<String, Object?> sessionJson(String channel) => <String, Object?>{
  'schema_version': '1.0',
  'session_id': 's1',
  'thread_id': 't1',
  'channel': channel,
  'epoch': 1,
  'name': 'Kinaiya',
};

Map<String, Object?> evidenceJson(
  String kind, {
  int revision = 2,
  String? path,
  String? sha256,
}) => {
  'schema_version': '1.0',
  'kind': kind,
  'summary': 'Public synthetic receipt',
  'objective_revision': revision,
  'path': path,
  'sha256': sha256,
};

AgentTask evidencedTask(
  List<Map<String, Object?>> evidence, {
  String state = 'succeeded',
  List<String> remaining = const [],
}) => AgentTask.fromJson({
  'schema_version': '1.0',
  'task_id': 'synthetic-task',
  'session_id': 'synthetic-session',
  'thread_id': 'synthetic-thread',
  'epoch': 1,
  'objective': 'Inspect public synthetic receipts',
  'objective_revision': 2,
  'state': state,
  'idempotency_key': 'synthetic-key',
  'evidence': evidence,
  'remaining_required_work': remaining,
});

void main() {
  test('session parses snake_case and checks schema version', () {
    final s = SessionInfo.fromJson(sessionJson('converse'));
    expect(s.sessionId, 's1');
    expect(s.channel, 'converse');
    expect(
      () => SessionInfo.fromJson({
        ...sessionJson('converse'),
        'schema_version': '2.0',
      }),
      throwsFormatException,
    );
    expect(
      () => SessionInfo.fromJson(sessionJson('nope')),
      throwsFormatException,
    );
  });

  test('grounded success needs succeeded state plus evidence', () {
    Map<String, Object?> taskJson(String state, bool evidence) =>
        <String, Object?>{
          'schema_version': '1.0',
          'task_id': 'k1',
          'session_id': 's',
          'thread_id': 't',
          'epoch': 1,
          'objective': 'do work',
          'objective_revision': 1,
          'state': state,
          'idempotency_key': 'k',
          'evidence': evidence
              ? [
                  {
                    'schema_version': '1.0',
                    'kind': 'file',
                    'summary': 'wrote proof',
                    'path': 'proof.txt',
                    'sha256': 'abc',
                  },
                ]
              : [],
        };
    expect(
      AgentTask.fromJson(taskJson('succeeded', true)).isGroundedSuccess,
      isTrue,
    );
    expect(
      AgentTask.fromJson(taskJson('succeeded', false)).isGroundedSuccess,
      isFalse,
    );
    expect(
      AgentTask.fromJson(taskJson('running', true)).isGroundedSuccess,
      isFalse,
    );
    expect(AgentTask.fromJson(taskJson('failed', true)).isTerminal, isTrue);
  });

  for (final receiptKind in ['external', 'extension']) {
    test(
      '$receiptKind receipts and stale native evidence never prove completion',
      () {
        AgentTask task(List<Map<String, Object?>> evidence) =>
            AgentTask.fromJson({
              'schema_version': '1.0',
              'task_id': 'mcp-task',
              'session_id': 's',
              'thread_id': 't',
              'epoch': 1,
              'objective': 'work',
              'objective_revision': 2,
              'state': 'succeeded',
              'idempotency_key': 'k',
              'evidence': evidence,
            });
        final external = <String, Object?>{
          'schema_version': '1.0',
          'kind': receiptKind,
          'summary': 'response arrived; effects unverified',
          'objective_revision': 2,
        };
        final stale = <String, Object?>{
          'schema_version': '1.0',
          'kind': 'file',
          'summary': 'old artifact',
          'objective_revision': 1,
          'path': 'proof',
          'sha256': 'abc',
        };
        expect(task([external]).isGroundedSuccess, isFalse);
        expect(task([external, stale]).isGroundedSuccess, isFalse);
        expect(task([external, stale]).hasUnverifiedExternalOnly, isTrue);
        expect(
          task([
            external,
            {...stale, 'objective_revision': 2},
          ]).isGroundedSuccess,
          isTrue,
        );
        expect(
          task([
            external,
            {
              'schema_version': '1.0',
              'kind': 'read',
              'summary': 'MCP metadata',
              'objective_revision': 2,
            },
          ]).isGroundedSuccess,
          isFalse,
        );
      },
    );
  }

  // Keep this closed set aligned with packages/contracts/models.py Evidence.
  for (final kind in [
    'file',
    'command',
    'read',
    'search',
    'test',
    'extension',
    'external',
    'artifact',
    'inspection',
  ]) {
    test('native $kind evidence parses inside a task response', () {
      final task = evidencedTask([evidenceJson(kind)]);
      expect(task.evidence.single.kind, kind);
      expect(task.evidence.single.objectiveRevision, 2);
    });
  }

  test('unknown evidence kinds still fail the closed native contract', () {
    expect(
      () => evidencedTask([evidenceJson('unknown-native-kind')]),
      throwsFormatException,
    );
  });

  test('inspection parses without becoming current work evidence', () {
    final inspection = evidenceJson('inspection');
    expect(evidencedTask([inspection]).isGroundedSuccess, isFalse);
    expect(
      evidencedTask([
        inspection,
        evidenceJson('file', revision: 1),
      ]).isGroundedSuccess,
      isFalse,
    );
    expect(
      evidencedTask([inspection, evidenceJson('file')]).isGroundedSuccess,
      isTrue,
    );
    for (final kind in ['external', 'extension']) {
      final task = evidencedTask([inspection, evidenceJson(kind)]);
      expect(task.isGroundedSuccess, isFalse);
      expect(task.hasUnverifiedExternalOnly, isTrue);
    }
  });

  final artifact = evidenceJson(
    'artifact',
    path: 'artifact:synthetic-ui_0123',
    sha256: 'a' * 64,
  );

  test('current native artifact receipt grounds publication only', () {
    expect(evidencedTask([artifact]).isGroundedSuccess, isTrue);
    expect(
      evidencedTask([evidenceJson('inspection'), artifact]).isGroundedSuccess,
      isTrue,
    );
    expect(
      evidencedTask([
        {...artifact, 'objective_revision': 1},
        evidenceJson('inspection'),
      ]).isGroundedSuccess,
      isFalse,
    );
    for (final kind in ['external', 'extension']) {
      // This confirms persisted UI output, not the external data's truth or
      // a remote side effect, matching the native completion predicate.
      expect(
        evidencedTask([evidenceJson(kind), artifact]).isGroundedSuccess,
        isTrue,
      );
      expect(
        evidencedTask([
          evidenceJson(kind),
          {...artifact, 'sha256': null},
        ]).isGroundedSuccess,
        isFalse,
      );
    }
  });

  final malformedArtifacts = <String, Map<String, Object?>>{
    'missing path': {'path': null},
    'missing hash': {'sha256': null},
    'missing artifact prefix': {'path': 'synthetic-ui'},
    'empty artifact ID': {'path': 'artifact:'},
    'long artifact ID': {'path': 'artifact:${'x' * 129}'},
    'non-native ID character': {'path': 'artifact:synthetic/other'},
    'ID newline': {'path': 'artifact:synthetic\n'},
    'short hash': {'sha256': 'a' * 63},
    'nonhex hash': {'sha256': 'z' * 64},
    'uppercase hash': {'sha256': 'A' * 64},
    'hash newline': {'sha256': '${'a' * 64}\n'},
  };
  for (final malformed in malformedArtifacts.entries) {
    test('artifact ${malformed.key} cannot ground success', () {
      expect(
        evidencedTask([
          {...artifact, ...malformed.value},
          evidenceJson('inspection'),
        ]).isGroundedSuccess,
        isFalse,
      );
    });
  }

  test(
    'artifact ID boundary, task state and remaining work stay authoritative',
    () {
      expect(
        evidencedTask([
          {...artifact, 'path': 'artifact:${'x' * 128}'},
        ]).isGroundedSuccess,
        isTrue,
      );
      for (final state in ['accepted', 'running', 'failed', 'cancelled']) {
        expect(
          evidencedTask([artifact], state: state).isGroundedSuccess,
          isFalse,
        );
      }
      expect(
        evidencedTask(
          [artifact],
          remaining: ['Unfinished work'],
        ).isGroundedSuccess,
        isFalse,
      );
    },
  );

  test('approval expiry is honored', () {
    ApprovalRequest req(String? expires) => ApprovalRequest(
      approvalId: 'a1',
      taskId: 't1',
      tool: 'shell',
      actionDigest: 'd',
      objectiveRevision: 2,
      expiresAt: expires,
    );
    expect(req(null).isExpired, isFalse);
    expect(req('2999-01-01T00:00:00Z').isExpired, isFalse);
    expect(req('2000-01-01T00:00:00Z').isExpired, isTrue);
  });

  test('journal entry maps causes and task refs', () {
    final e = JournalEntry.fromJson(<String, Object?>{
      'schema_version': '1.0',
      'event_id': 'e1',
      'sequence': 7,
      'session_id': 's',
      'thread_id': 't',
      'epoch': 1,
      'kind': 'conversation.converse',
      'payload': {'text': 'hello'},
      'idempotency_key': 'k',
      'origin': 'converse',
      'causes': ['c1'],
      'task_refs': ['k1'],
    });
    expect(e.sequence, 7);
    expect(e.causes, ['c1']);
    expect(e.taskRefs, ['k1']);
  });
}
