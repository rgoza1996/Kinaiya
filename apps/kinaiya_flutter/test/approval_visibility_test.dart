/// Public synthetic transport fixtures; no inference or real credentials.
library;

import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/app.dart';
import 'package:kinaiya_flutter/state/app_state.dart';

import 'reconnect_test.dart' show FakeAgentClient, profileA, profileB;

AgentTask voiceTask() => AgentTask(
  taskId: 'voice-task',
  sessionId: 'voice-session',
  threadId: 'voice-thread',
  epoch: 3,
  objective: 'Public synthetic file change',
  objectiveRevision: 2,
  state: 'awaiting_approval',
  idempotencyKey: 'voice-request',
);

Map<String, Object?> requestJson({
  String approvalId = 'approval-public',
  String session = 'voice-session',
  int revision = 2,
  double? expiry,
}) => {
  'schema_version': '1.0',
  'origin': 'agent-supervisor',
  'approval_id': approvalId,
  'task_id': 'voice-task',
  'session_id': session,
  'thread_id': 'voice-thread',
  'epoch': 3,
  'tool': 'write_file',
  'arguments': {'path': 'public.txt', 'content': 'confirmed'},
  'action_digest': 'public-exact-digest',
  'objective_revision': revision,
  'expires_at':
      expiry ??
      DateTime.now().add(const Duration(minutes: 5)).millisecondsSinceEpoch /
          1000,
  'reason': 'uncommitted_speech',
};

PendingApprovalPage page(List<Map<String, Object?>> entries, [String? after]) =>
    PendingApprovalPage.fromJson({
      'schema_version': '1.0',
      'approvals': entries,
      'next_after': after,
    });

class ApprovalClient extends FakeAgentClient {
  final decisions = <Map<String, Object?>>[];
  @override
  Future<void> resolveApproval({
    required String approvalId,
    required bool approved,
    required String actionDigest,
    required int objectiveRevision,
  }) async {
    decisions.add({
      'approval_id': approvalId,
      'approved': approved,
      'action_digest': actionDigest,
      'objective_revision': objectiveRevision,
    });
  }
}

void main() {
  test(
    'pending approvals use scoped pages and only 404 permits journal fallback',
    () async {
      final seen = <Uri>[];
      var status = 200;
      final api = KinaiyaClient.mock(
        sessionBase: 'http://fixture',
        agentBase: 'http://fixture',
        converseBase: 'http://fixture',
        mockTransport: MockClient((req) async {
          seen.add(req.url);
          return http.Response(
            status == 200
                ? jsonEncode({
                    'schema_version': '1.0',
                    'approvals': [requestJson()],
                    'next_after': 'task-cursor',
                  })
                : '{}',
            status,
          );
        }),
      );
      final current = await api.pendingApprovals(after: 'task-cursor');
      expect(seen.single.path, '/v1/approvals');
      expect(seen.single.queryParameters['after'], 'task-cursor');
      expect(current!.approvals.single.sessionId, 'voice-session');
      expect(current.approvals.single.reason, 'uncommitted_speech');
      status = 404;
      expect(await api.pendingApprovals(), isNull);
      status = 403;
      await expectLater(api.pendingApprovals(), throwsA(isA<ApiException>()));
      api.close();
    },
  );

  test(
    'pending approval pages reject external origin and incompatible schemas',
    () {
      expect(
        () => page([
          {...requestJson(), 'origin': 'external'},
        ]),
        throwsFormatException,
      );
      expect(
        () => PendingApprovalPage.fromJson({
          'schema_version': '2.0',
          'approvals': [],
        }),
        throwsFormatException,
      );
    },
  );

  testWidgets(
    'voice approval is visible without an Agent session and resolves its exact action',
    (tester) async {
      // Keep both retained expiry and live request cards in this viewport.
      tester.view.devicePixelRatio = 1;
      tester.view.physicalSize = const Size(800, 1600);
      addTearDown(tester.view.resetDevicePixelRatio);
      addTearDown(tester.view.resetPhysicalSize);
      final api = ApprovalClient();
      final cursors = <String?>[];
      api.tasksHook = () async => [voiceTask()];
      api.approvalsHook = (after) async {
        cursors.add(after);
        if (api.decisions.isNotEmpty) return page([]);
        if (after == null) {
          return page([
            requestJson(session: 'other-session'),
            requestJson(revision: 1),
            // ADR0092 retains expired requests. These are separate permits,
            // rather than contradictory expiry metadata for the same ID.
            requestJson(approvalId: 'approval-expired-public', expiry: 1),
          ], 'next-public-page');
        }
        return page([requestJson()]);
      };
      final state = AppState()..connect(profileA(), injected: api);
      expect(state.agentSession, isNull);
      state.setView(AppView.agent);
      await tester.pumpWidget(KinaiyaApp(state: state));
      await tester.pump(const Duration(milliseconds: 300));
      expect(cursors, [null, 'next-public-page']);
      expect(state.approvals.length, 2);
      expect(state.approvals.map((approval) => approval.approvalId), [
        'approval-expired-public',
        'approval-public',
      ]);
      expect(
        find.text(
          'Live speech is not yet confirmed. Review this exact action.',
        ),
        findsNWidgets(2),
      );
      final approveButtons = find.widgetWithText(FilledButton, 'Approve');
      expect(approveButtons, findsNWidgets(2));
      final disabledApprove = find.byWidgetPredicate(
        (widget) =>
            widget is FilledButton &&
            widget.child is Text &&
            (widget.child! as Text).data == 'Approve' &&
            widget.onPressed == null,
      );
      final enabledApprove = find.byWidgetPredicate(
        (widget) =>
            widget is FilledButton &&
            widget.child is Text &&
            (widget.child! as Text).data == 'Approve' &&
            widget.onPressed != null,
      );
      expect(disabledApprove, findsOneWidget);
      expect(enabledApprove, findsOneWidget);
      expect(find.text('Expired approval: write_file'), findsOneWidget);
      await tester.tap(disabledApprove);
      await tester.pump();
      expect(api.decisions, isEmpty);
      await tester.tap(enabledApprove);
      await tester.pump(const Duration(milliseconds: 300));
      expect(api.decisions.single, {
        'approval_id': 'approval-public',
        'approved': true,
        'action_digest': 'public-exact-digest',
        'objective_revision': 2,
      });
      expect(state.approvals, isEmpty);
      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
    },
  );

  testWidgets(
    'an approval page completing after reconnect cannot cross hosts',
    (tester) async {
      final gate = Completer<PendingApprovalPage?>();
      final old = FakeAgentClient(
        tasksHook: () async => [voiceTask()],
        approvalsHook: (_) => gate.future,
      );
      final state = AppState()..connect(profileA(), injected: old);
      state.setView(AppView.agent);
      await tester.pumpWidget(KinaiyaApp(state: state));
      await tester.pump(const Duration(milliseconds: 200));
      final next = FakeAgentClient(
        tasksHook: () async => [voiceTask()],
        approvalsHook: (_) async => page([]),
      );
      state.connect(profileB(), injected: next);
      await tester.pump(const Duration(milliseconds: 200));
      gate.complete(page([requestJson()]));
      await tester.pump(const Duration(milliseconds: 200));
      expect(state.approvals, isEmpty);
      expect(find.text('Approve'), findsNothing);
      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
    },
  );
}
