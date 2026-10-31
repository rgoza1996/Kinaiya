/// Provider-consent regressions for GitHub #35.
///
/// Positive per-role provider verification plus disclosure consent:
/// session authentication is distinct from inference availability; each role
/// fails closed on missing/malformed/unavailable/unconsented metadata while
/// the other authorized role stays useful. Deterministic owner controls
/// (/status, /stop, approvals) work on authenticated sessions even when
/// inference is blocked; natural text, skill invocations and unknown classes
/// never submit. All transports are synthetic; no inference runs.
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
import 'package:kinaiya_flutter/audio/speech_audio.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/character_view.dart';

import 'live_capture_test.dart' show CaptureAudio;
import 'concept_layout_test.dart' show openConverseChat;

Map<String, Object?> localProvider() => <String, Object?>{
  'provider': 'local',
  'model': 'local-model',
  'local': true,
};

Map<String, Object?> contributorProvider() => <String, Object?>{
  'provider': 'meta',
  'model': 'muse-spark-1.3-contributor',
  'local': false,
};

ConnectionProfile profileWith(BackendProfile consent) => ConnectionProfile(
  sessionUrl: 'http://127.0.0.1:8760',
  agentUrl: 'http://127.0.0.1:8761',
  converseUrl: 'http://127.0.0.1:8762',
  ownerToken: 'synthetic-token',
  backendProfile: consent,
);

/// Synthetic verification transport. Agent and Converse health are told apart
/// by port (8761 vs 8762); [providerFor] returns the provider block per role
/// (null omits it), or throws/500s to simulate unavailability.
KinaiyaClient verifyClient({
  required Object? Function(bool isAgent) providerFor,
  bool failAgentCaps = false,
  bool failAgentHealth = false,
  bool failConverseHealth = false,
}) {
  return KinaiyaClient(
    sessionBase: 'http://127.0.0.1:8760',
    agentBase: 'http://127.0.0.1:8761',
    converseBase: 'http://127.0.0.1:8762',
    ownerToken: 'synthetic-token',
    httpClient: MockClient((http.Request req) async {
      final path = req.url.path;
      if (req.method == 'GET' && path == '/v1/sessions') {
        return http.Response('[]', 200);
      }
      if (path == '/v1/agents/capabilities') {
        if (failAgentCaps) return http.Response('cut', 500);
        return http.Response('{}', 200);
      }
      if (path == '/health') {
        final isAgent = req.url.port == 8761;
        if (isAgent && failAgentHealth) {
          return http.Response('cut', 500);
        }
        if (!isAgent && failConverseHealth) {
          return http.Response('cut', 500);
        }
        final provider = providerFor(isAgent);
        if (provider == null) {
          return http.Response(jsonEncode({'enabled': true}), 200);
        }
        return http.Response(
          jsonEncode({'enabled': true, 'provider': provider}),
          200,
        );
      }
      return http.Response('unexpected $path', 500);
    }),
  );
}

Future<AppState> verifiedState({
  required BackendProfile consent,
  required Object? Function(bool isAgent) providerFor,
  bool failAgentCaps = false,
  bool failAgentHealth = false,
  bool failConverseHealth = false,
}) async {
  final state = AppState();
  state.clientFactory = (_) => verifyClient(
    providerFor: providerFor,
    failAgentCaps: failAgentCaps,
    failAgentHealth: failAgentHealth,
    failConverseHealth: failConverseHealth,
  );
  state.connect(profileWith(consent));
  await Future<void>.delayed(const Duration(milliseconds: 400));
  return state;
}

/// Counting fake for widget denial tests: every inference submission is
/// observable, so denial means an exact zero.
class ConsentClient extends KinaiyaClient {
  ConsentClient()
    : super(
        sessionBase: 'http://127.0.0.1:8760',
        agentBase: 'http://127.0.0.1:8761',
        converseBase: 'http://127.0.0.1:8762',
        ownerToken: 'synthetic',
        artifactClientFactory: () => throw StateError(
          'UI artifacts are unavailable in this synthetic consent fixture',
        ),
        agentWorkspaceClientFactory: () => throw StateError(
          'Workspace reads are unavailable in this synthetic legacy fixture',
        ),
      );

  int createTasks = 0;
  int executed = 0;
  int turns = 0;
  int interrupts = 0;
  int resolutions = 0;
  int transcriptions = 0;
  Completer<List<JournalEntry>>? recentGate;
  Completer<Map<String, Object?>?>? executionGate;
  List<CommandDef> registry = <CommandDef>[
    CommandDef(
      name: 'status',
      description: 'Show status',
      executionType: 'runtime_control',
    ),
    CommandDef(
      name: 'stop',
      description: 'Stop work',
      executionType: 'runtime_control',
    ),
    CommandDef(
      name: 'skill',
      description: 'Start skill work',
      executionType: 'skill_invocation',
    ),
    CommandDef(
      name: 'mystery',
      description: 'Unknown class',
      executionType: 'weird',
    ),
    CommandDef(
      name: 'off',
      description: 'Unavailable',
      available: false,
      executionType: 'runtime_control',
    ),
  ];
  List<AgentTask> taskList = const [];
  List<JournalEntry> journal = const [];

  @override
  Future<Map<String, Object?>> converseHealth() async => {'enabled': true};

  @override
  Future<List<CommandDef>?> fetchCommands() async => registry;

  @override
  Future<List<AgentTask>> listTasks() async => taskList;

  // This compatibility fixture represents the older journal-only host.
  @override
  Future<PendingApprovalPage?> pendingApprovals({String? after}) async => null;

  @override
  Future<AgentTask> createTask({
    required String sessionId,
    required String threadId,
    required int epoch,
    required String objective,
    required String idempotencyKey,
    ContentScope contentScope = ContentScope.localOnly,
  }) async {
    createTasks++;
    return AgentTask(
      taskId: 'task-synthetic',
      sessionId: sessionId,
      threadId: threadId,
      epoch: epoch,
      objective: objective,
      objectiveRevision: 1,
      state: 'accepted',
      idempotencyKey: idempotencyKey,
    );
  }

  @override
  Future<Map<String, Object?>?> executeCommand({
    required String sessionId,
    required String threadId,
    required int epoch,
    required String input,
    required String idempotencyKey,
    ContentScope contentScope = ContentScope.localOnly,
  }) async {
    executed++;
    if (executionGate != null) return executionGate!.future;
    final name = input.trim().split(RegExp(r'\s+')).first.substring(1);
    return <String, Object?>{
      'schema_version': '1.0',
      'status': 'ok',
      'command': name,
    };
  }

  @override
  Stream<ConverseEvent> streamTurn({
    required String sessionId,
    required String threadId,
    required int epoch,
    required String text,
    required String idempotencyKey,
    String inputMode = 'text',
    ContentScope contentScope = ContentScope.localOnly,
  }) async* {
    turns++;
    yield ConverseEvent('done', <String, Object?>{'type': 'done'});
  }

  @override
  Future<AgentTask> interruptTask({
    required String taskId,
    required int expectedObjectiveRevision,
    required String idempotencyKey,
    String reason = 'User requested cancellation',
  }) async {
    interrupts++;
    return AgentTask(
      taskId: taskId,
      sessionId: 's-agent',
      threadId: 't-agent',
      epoch: 1,
      objective: 'synthetic',
      objectiveRevision: expectedObjectiveRevision,
      state: 'cancelled',
      idempotencyKey: idempotencyKey,
    );
  }

  @override
  Future<void> resolveApproval({
    required String approvalId,
    required bool approved,
    required String actionDigest,
    required int objectiveRevision,
  }) async {
    resolutions++;
  }

  @override
  Future<List<SessionInfo>> listSessions() async => const [];

  @override
  Future<SessionInfo> createSession(String channel, {String name = ''}) async =>
      SessionInfo(
        sessionId: 's-$channel',
        threadId: 't-$channel',
        channel: channel,
        epoch: 1,
        name: name,
      );

  @override
  Future<List<JournalEntry>> recent({
    required String sessionId,
    int limit = 100,
  }) async => recentGate == null ? journal : recentGate!.future;

  @override
  Future<Map<String, Object?>> transcribe(
    List<int> bytes,
    String filename, {
    ContentScope contentScope = ContentScope.localOnly,
  }) async {
    transcriptions++;
    return {'text': 'public synthetic audio'};
  }
}

class PendingRecording extends CaptureAudio {
  final finished = Completer<RecordedAudio?>();
  @override
  Future<RecordedAudio?> stopRecording() => finished.future;
}

AgentTask cardTask({String state = 'accepted', int revision = 1}) => AgentTask(
  taskId: 't1',
  sessionId: 's-agent',
  threadId: 't-agent',
  epoch: 1,
  objective: 'synthetic work',
  objectiveRevision: revision,
  state: state,
  idempotencyKey: 'k-t1',
);

JournalEntry approvalEntry() => JournalEntry(
  eventId: 'a1',
  sequence: 1,
  sessionId: 's-agent',
  threadId: 't-agent',
  epoch: 1,
  kind: 'approval.requested',
  origin: 'agent-supervisor',
  payload: {
    'approval_id': 'a1',
    'task_id': 't1',
    'tool': 'write_file',
    'action_digest': 'synthetic-digest',
    'objective_revision': 1,
    'expires_at': '2099-01-01T00:00:00Z',
    'arguments': <String, Object?>{},
  },
);

void main() {
  testWidgets('revoked Converse consent while history loads prevents a turn', (
    tester,
  ) async {
    final api = ConsentClient()..recentGate = Completer<List<JournalEntry>>();
    final state = AppState()
      ..connect(ConnectionProfile.defaults(), injected: api);
    await tester.pumpWidget(KinaiyaApp(state: state));
    await tester.pump(const Duration(milliseconds: 100));
    await openConverseChat(tester);
    await tester.enterText(find.byType(TextField), 'public synthetic request');
    await tester.testTextInput.receiveAction(TextInputAction.send);
    await tester.pump();
    expect(state.busyConverse, isTrue);
    state.converseInferenceAllowed = false;
    state.touch();
    api.recentGate!.complete([]);
    await tester.pump(const Duration(milliseconds: 100));
    expect(api.turns, 0);
    expect(state.busyConverse, isFalse);
    expect(state.converseDraft, 'public synthetic request');
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
  });

  testWidgets(
    'a recording completed after reconnect never uploads to either backend',
    (tester) async {
      final oldApi = ConsentClient(), newApi = ConsentClient();
      final state = AppState()
        ..connect(ConnectionProfile.defaults(), injected: oldApi);
      final audio = PendingRecording();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: AnimatedBuilder(
              animation: state,
              builder: (_, _) => CharacterView(state: state, audio: audio),
            ),
          ),
        ),
      );
      await tester.pump(const Duration(milliseconds: 100));
      await tester.tap(find.byTooltip('Record voice'));
      await tester.pump();
      await tester.tap(find.byTooltip('Stop recording'));
      await tester.pump();
      state.connect(ConnectionProfile.defaults(), injected: newApi);
      await tester.pump();
      audio.finished.complete(RecordedAudio('audio/webm', [1, 2, 3]));
      await tester.pump(const Duration(milliseconds: 100));
      expect(oldApi.transcriptions, 0);
      expect(newApi.transcriptions, 0);
      expect(newApi.turns, 0);
      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
    },
  );

  testWidgets('an uncertain model switch keeps Agent input blocked', (
    tester,
  ) async {
    final api = ConsentClient();
    api.registry.add(
      CommandDef(
        name: 'model',
        description: 'Switch profile',
        executionType: 'runtime_control',
      ),
    );
    api.executionGate = Completer<Map<String, Object?>?>();
    final state = AppState()
      ..connect(profileWith(BackendProfile.metaContributor), injected: api);
    state.agentSession = SessionInfo(
      sessionId: 's-agent',
      threadId: 't-agent',
      channel: 'agent',
      epoch: 1,
    );
    state.setView(AppView.agent);
    await tester.pumpWidget(KinaiyaApp(state: state));
    await tester.pump(const Duration(milliseconds: 200));
    await tester.enterText(find.byType(TextField), '/model remote');
    await tester.pump();
    await tester.tap(find.text('Send task'));
    await tester.pump();
    expect(api.executed, 1);
    expect(state.agentCanInfer, isFalse);
    api.executionGate!.completeError(
      ApiException(503, 'Synthetic lost acknowledgment'),
    );
    await tester.pump(const Duration(milliseconds: 200));
    expect(state.agentCanInfer, isFalse);
    await tester.enterText(find.byType(TextField), 'public synthetic followup');
    await tester.pump();
    await tester.testTextInput.receiveAction(TextInputAction.send);
    await tester.pump();
    expect(api.createTasks, 0);
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
  });

  test('providerAllows is positive, exact and fails closed', () {
    // Local metadata allows local inference under every profile.
    for (final consent in BackendProfile.values) {
      expect(AppState.providerAllows(localProvider(), consent), isTrue);
    }
    // Cloud metadata needs cloudDevelopment, or exact Meta Contributor.
    expect(
      AppState.providerAllows(contributorProvider(), BackendProfile.clean),
      isFalse,
    );
    expect(
      AppState.providerAllows(
        contributorProvider(),
        BackendProfile.cloudDevelopment,
      ),
      isTrue,
    );
    expect(
      AppState.providerAllows(
        contributorProvider(),
        BackendProfile.metaContributor,
      ),
      isTrue,
    );
    // Never substring matches: extended or partial models fail closed.
    for (final model in [
      'muse-spark-1.3-contributor-staging',
      'x-muse-spark-1.3-contributor',
      'contributor',
      'MUSE-SPARK-1.3-CONTRIBUTOR',
      '',
    ]) {
      expect(
        AppState.providerAllows({
          'provider': 'meta',
          'model': model,
          'local': false,
        }, BackendProfile.metaContributor),
        isFalse,
        reason: 'model $model must not match',
      );
    }
    expect(
      AppState.providerAllows({
        'provider': 'Meta',
        'model': 'muse-spark-1.3-contributor',
        'local': false,
      }, BackendProfile.metaContributor),
      isFalse,
    );
    // Missing or malformed metadata fails closed under every profile.
    final bad = <Object?>[
      null,
      'meta',
      42,
      <String, Object?>{},
      {'provider': 'meta', 'model': 'm', 'local': 'yes'},
      {'provider': 'meta', 'model': '', 'local': false},
      {'provider': '', 'model': 'muse-spark-1.3-contributor', 'local': false},
      {'provider': 'meta', 'model': 'muse-spark-1.3-contributor'},
    ];
    for (final consent in BackendProfile.values) {
      for (final info in bad) {
        expect(
          AppState.providerAllows(info, consent),
          isFalse,
          reason: 'consent $consent info $info must fail closed',
        );
      }
    }
  });

  test('CommandDef execution classes gate inference, never bypass', () {
    CommandDef def(String name, String type) =>
        CommandDef(name: name, description: 'd', executionType: type);
    expect(def('status', 'runtime_control').isDeterministic, isTrue);
    expect(def('status', 'runtime_control').requiresInference, isFalse);
    // The `skill` entry stays inference even when mislabeled by a stale
    // backend; dynamic skills are skill_invocation.
    expect(def('skill', 'runtime_control').requiresInference, isTrue);
    expect(def('skill', 'skill_invocation').requiresInference, isTrue);
    // Unknown or missing classes fail closed to inference.
    for (final type in ['', 'weird', 'RUNTIME_CONTROL', 'runtime-control']) {
      expect(
        def('mystery', type).requiresInference,
        isTrue,
        reason: 'class "$type" must not bypass consent',
      );
    }
  });

  test('partial failure keeps the healthy consented role useful', () async {
    final state = await verifiedState(
      consent: BackendProfile.clean,
      providerFor: (_) => localProvider(),
      failConverseHealth: true,
    );
    expect(state.sessionAuthenticated, isTrue);
    expect(state.agentOnline, isTrue);
    expect(state.converseOnline, isFalse);
    expect(state.agentInferenceAllowed, isTrue);
    expect(state.converseInferenceAllowed, isFalse);
    expect(state.connected, isTrue);
    expect(state.connectionError, contains('Agent ready'));
    state.dispose();
  });

  test('missing and malformed metadata fail closed per role', () async {
    final missing = await verifiedState(
      consent: BackendProfile.metaContributor,
      providerFor: (_) => null,
    );
    expect(missing.sessionAuthenticated, isTrue);
    expect(missing.agentOnline, isTrue);
    expect(missing.converseOnline, isTrue);
    expect(missing.agentInferenceAllowed, isFalse);
    expect(missing.converseInferenceAllowed, isFalse);
    expect(missing.connected, isFalse);
    expect(missing.connectionError, contains('Cloud inference'));
    missing.dispose();

    final malformed = await verifiedState(
      consent: BackendProfile.cloudDevelopment,
      providerFor: (isAgent) =>
          isAgent ? <String, Object?>{'provider': 'meta'} : 'not-a-map',
    );
    expect(malformed.agentInferenceAllowed, isFalse);
    expect(malformed.converseInferenceAllowed, isFalse);
    expect(malformed.connected, isFalse);
    malformed.dispose();
  });

  test(
    'Contributor match is exact; clean profile never allows cloud',
    () async {
      final exact = await verifiedState(
        consent: BackendProfile.metaContributor,
        providerFor: (_) => contributorProvider(),
      );
      expect(exact.agentInferenceAllowed, isTrue);
      expect(exact.converseInferenceAllowed, isTrue);
      expect(exact.connected, isTrue);
      exact.dispose();

      final nearMiss = await verifiedState(
        consent: BackendProfile.metaContributor,
        providerFor: (_) => <String, Object?>{
          'provider': 'meta',
          'model': 'muse-spark-1.3-contributor-staging',
          'local': false,
        },
      );
      expect(nearMiss.agentInferenceAllowed, isFalse);
      expect(nearMiss.converseInferenceAllowed, isFalse);
      expect(nearMiss.connected, isFalse);
      nearMiss.dispose();

      final cleanCloud = await verifiedState(
        consent: BackendProfile.clean,
        providerFor: (_) => contributorProvider(),
      );
      expect(cleanCloud.connected, isFalse);
      cleanCloud.dispose();
    },
  );

  test('mixed roles stay independent: one blocked, one useful', () async {
    final state = await verifiedState(
      consent: BackendProfile.clean,
      providerFor: (isAgent) =>
          isAgent ? contributorProvider() : localProvider(),
    );
    expect(state.agentInferenceAllowed, isFalse);
    expect(state.converseInferenceAllowed, isTrue);
    expect(state.converseCanInfer, isTrue);
    expect(state.agentCanInfer, isFalse);
    expect(state.connected, isTrue);
    expect(state.connectionError, contains('blocked'));
    state.dispose();
  });

  test(
    'model switch revalidates Agent consent before further inference',
    () async {
      Object? agentProvider = contributorProvider();
      final state = AppState();
      state.clientFactory = (_) => verifyClient(
        providerFor: (isAgent) =>
            isAgent ? agentProvider : contributorProvider(),
      );
      state.connect(profileWith(BackendProfile.metaContributor));
      await Future<void>.delayed(const Duration(milliseconds: 400));
      expect(state.agentInferenceAllowed, isTrue);

      agentProvider = <String, Object?>{
        'provider': 'other',
        'model': 'other-model',
        'local': false,
      };
      await state.revalidateAgentInference();
      expect(state.agentProvider, isNotNull);
      expect(state.agentInferenceAllowed, isFalse);
      expect(state.agentCanInfer, isFalse);
      // The still-consented Converse role keeps the connection useful.
      expect(state.connected, isTrue);

      agentProvider = contributorProvider();
      await state.revalidateAgentInference();
      expect(state.agentInferenceAllowed, isTrue);
      expect(state.agentCanInfer, isTrue);
      state.dispose();
    },
  );

  testWidgets('blocked inference denies Enter and button: zero submissions', (
    tester,
  ) async {
    final api = ConsentClient();
    final state = AppState()
      ..connect(
        ConnectionProfile(
          sessionUrl: 'http://127.0.0.1:8760',
          agentUrl: 'http://127.0.0.1:8761',
          converseUrl: 'http://127.0.0.1:8762',
          ownerToken: 'test-token',
        ),
        injected: api,
      );
    // Simulate verified session with inference blocked for both roles.
    state.sessionAuthenticated = true;
    state.agentInferenceAllowed = false;
    state.converseInferenceAllowed = false;
    state.setView(AppView.agent);
    await tester.pumpWidget(KinaiyaApp(state: state));
    await tester.pump(const Duration(milliseconds: 300));

    // Enter key on natural text: denied with no task submission.
    await tester.enterText(find.byType(TextField), 'plain work');
    await tester.pump();
    await tester.testTextInput.receiveAction(TextInputAction.send);
    await tester.pump(const Duration(milliseconds: 300));
    expect(api.createTasks, 0);
    expect(api.executed, 0);
    expect(state.tasks, isEmpty);
    expect(state.agentHistory, isEmpty);
    expect(state.agentNotice, contains('inference is not available'));

    // Skill invocation via Enter: denied without execution.
    await tester.enterText(find.byType(TextField), '/skill do things');
    await tester.pump();
    await tester.testTextInput.receiveAction(TextInputAction.send);
    await tester.pump(const Duration(milliseconds: 300));
    expect(api.executed, 0);
    expect(api.createTasks, 0);
    expect(state.agentHistory, isEmpty);

    // Unknown execution class via Enter: never sent anywhere.
    await tester.enterText(find.byType(TextField), '/mystery x');
    await tester.pump();
    await tester.testTextInput.receiveAction(TextInputAction.send);
    await tester.pump(const Duration(milliseconds: 300));
    expect(api.executed, 0);

    // Unknown command name: not sent anywhere.
    await tester.enterText(find.byType(TextField), '/nope');
    await tester.pump();
    await tester.testTextInput.receiveAction(TextInputAction.send);
    await tester.pump(const Duration(milliseconds: 300));
    expect(api.executed, 0);
    expect(state.agentNotice, contains('Unknown command'));

    // Send button is also gated for blocked input.
    await tester.enterText(find.byType(TextField), 'more plain work');
    await tester.pump();
    await tester.tap(find.text('Send task'));
    await tester.pump(const Duration(milliseconds: 300));
    expect(api.createTasks, 0);
    expect(api.executed, 0);
    expect(state.agentHistory, isEmpty);

    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
  });

  testWidgets('deterministic /status still runs while inference is blocked', (
    tester,
  ) async {
    final api = ConsentClient();
    final state = AppState()
      ..connect(
        ConnectionProfile(
          sessionUrl: 'http://127.0.0.1:8760',
          agentUrl: 'http://127.0.0.1:8761',
          converseUrl: 'http://127.0.0.1:8762',
          ownerToken: 'test-token',
        ),
        injected: api,
      );
    state.agentSession = SessionInfo(
      sessionId: 's-agent',
      threadId: 't-agent',
      channel: 'agent',
      epoch: 1,
    );
    state.sessionAuthenticated = true;
    state.agentInferenceAllowed = false;
    state.converseInferenceAllowed = false;
    state.setView(AppView.agent);
    await tester.pumpWidget(KinaiyaApp(state: state));
    await tester.pump(const Duration(milliseconds: 300));
    expect(state.commandsAvailable, isTrue);

    await tester.enterText(find.byType(TextField), '/status');
    await tester.pump();
    await tester.tap(find.text('Send task'));
    await tester.pump(const Duration(milliseconds: 300));
    expect(api.executed, 1);
    expect(api.createTasks, 0);
    expect(state.tasks, isEmpty);
    expect(state.agentHistory.single.text, 'Command status: ok');
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
  });

  testWidgets('deterministic stop and approval work without inference', (
    tester,
  ) async {
    final api = ConsentClient()
      ..taskList = [cardTask(state: 'awaiting_approval')]
      ..journal = [approvalEntry()];
    final state = AppState()
      ..connect(
        ConnectionProfile(
          sessionUrl: 'http://127.0.0.1:8760',
          agentUrl: 'http://127.0.0.1:8761',
          converseUrl: 'http://127.0.0.1:8762',
          ownerToken: 'test-token',
        ),
        injected: api,
      );
    state.sessionAuthenticated = true;
    state.agentInferenceAllowed = false;
    state.converseInferenceAllowed = false;
    state.agentSession = SessionInfo(
      sessionId: 's-agent',
      threadId: 't-agent',
      channel: 'agent',
      epoch: 1,
      name: 'work',
    );
    state.setView(AppView.agent);
    await tester.pumpWidget(KinaiyaApp(state: state));
    await tester.pump(const Duration(milliseconds: 300));
    await tester.pump(const Duration(seconds: 4));
    expect(state.approvals.single.approvalId, 'a1');

    await tester.tap(find.text('Stop'));
    await tester.pump(const Duration(milliseconds: 300));
    expect(api.interrupts, 1);
    expect(api.createTasks, 0);

    // The backend consumed the request: no requested entries remain, so the
    // post-resolve task poll honestly clears the approval.
    api.journal = [];
    await tester.tap(find.text('Approve'));
    await tester.pump(const Duration(milliseconds: 300));
    expect(api.resolutions, 1);
    expect(state.approvals, isEmpty);
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
  });

  testWidgets('blocked Converse sends no turn and stays ordinary', (
    tester,
  ) async {
    final api = ConsentClient();
    final state = AppState()
      ..connect(
        ConnectionProfile(
          sessionUrl: 'http://127.0.0.1:8760',
          agentUrl: 'http://127.0.0.1:8761',
          converseUrl: 'http://127.0.0.1:8762',
          ownerToken: 'test-token',
        ),
        injected: api,
      );
    state.sessionAuthenticated = true;
    state.agentInferenceAllowed = false;
    state.converseInferenceAllowed = false;
    await tester.pumpWidget(KinaiyaApp(state: state));
    await tester.pump(const Duration(milliseconds: 300));

    await openConverseChat(tester);
    await tester.enterText(find.byType(TextField), 'hello kinaiya');
    await tester.pump();
    await tester.testTextInput.receiveAction(TextInputAction.send);
    await tester.pump(const Duration(milliseconds: 300));
    expect(api.turns, 0);
    expect(state.converseHistory.where((m) => m.role == 'kinaiya'), isEmpty);

    // Slash-like text stays ordinary conversation, never a command.
    await tester.enterText(find.byType(TextField), '/status do stuff');
    await tester.pump();
    await tester.testTextInput.receiveAction(TextInputAction.send);
    await tester.pump(const Duration(milliseconds: 300));
    expect(api.turns, 0);
    expect(api.executed, 0);
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
  });
}
