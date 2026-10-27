import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/app.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'client_test.dart' show mockClient;

void main() {
  test('backend numeric expiry and canonical command aliases parse', () {
    final entry = JournalEntry(
      eventId: 'e',
      sequence: 1,
      sessionId: 's',
      threadId: 't',
      epoch: 1,
      kind: 'approval.requested',
      origin: 'agent-supervisor',
      payload: {
        'approval_id': 'a',
        'task_id': 'task',
        'tool': 'shell',
        'action_digest': 'digest',
        'objective_revision': 2,
        'expires_at': 946684800.0,
        'arguments': <String, Object?>{},
      },
    );
    expect(ApprovalRequest.fromEntry(entry).isExpired, isTrue);
    final command = CommandDef.fromJson({
      'name': 'status',
      'aliases': ['tasks'],
      'description': 'Inspect state',
      'syntax': '/status [task-id]',
    });
    expect(command.matches('status'), isTrue);
    expect(command.matches('tasks'), isTrue);
    expect(command.matches('stop'), isFalse);
  });

  testWidgets(
    'Agent dispatches canonical registry command without an inference task',
    (tester) async {
      final state = AppState();
      state.connect(
        ConnectionProfile.defaults(),
        injected: mockClient(commandsMissing: false),
      );
      state.setView(AppView.agent);
      await tester.pumpWidget(KinaiyaApp(state: state));
      await tester.pump(const Duration(milliseconds: 200));
      await tester.enterText(find.byType(TextField), '/status');
      await tester.pump();
      await tester.tap(find.text('Send task'));
      await tester.pump(const Duration(milliseconds: 300));
      expect(state.agentNotice, isEmpty);
      expect(state.agentHistory.single.text, 'Command status: ok');
      expect(state.tasks, isEmpty);
      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
    },
  );
}
