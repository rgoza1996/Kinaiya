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

  test(
    'external receipts and stale native evidence never prove completion',
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
        'kind': 'external',
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
