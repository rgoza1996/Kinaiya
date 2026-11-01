import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/audio/speech_audio.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/agent_chat.dart';
import 'package:kinaiya_flutter/ui/character_view.dart';

import 'live_capture_test.dart' show CaptureAudio;
import 'window_capture_test.dart' show WindowAudio;
import 'provider_consent_test.dart'
    show
        ConsentClient,
        profileWith,
        localProvider,
        contributorProvider,
        cardTask;

/// Actual client serialization over an in-memory transport; no service calls.
class BodyTransport extends http.BaseClient {
  final bodies = <Map<String, Object?>>[];
  final multipartScopes = <String?>[];
  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) async {
    if (request is http.MultipartRequest) {
      multipartScopes.add(request.fields['content_scope']);
      return http.StreamedResponse(
        Stream.value(utf8.encode('{"text":"fixture"}')),
        200,
      );
    }
    final body =
        jsonDecode((request as http.Request).body) as Map<String, Object?>;
    bodies.add(body);
    final response = request.url.path.contains('turns/stream')
        ? 'data: {"type":"done"}\n\n'
        : request.url.path.contains('/tasks')
        ? jsonEncode({
            'schema_version': '1.0',
            'task_id': 't',
            'session_id': 's',
            'thread_id': 't',
            'epoch': 1,
            'objective': 'fixture',
            'objective_revision': 1,
            'state': 'accepted',
            'idempotency_key': 'k',
            'content_scope': body['content_scope'],
          })
        : '{}';
    return http.StreamedResponse(Stream.value(utf8.encode(response)), 200);
  }
}

class ScopeClient extends ConsentClient {
  final _agentSessions = <String, SessionInfo>{};

  @override
  Future<SessionInfo> agentWorkspaceSession(String id) async =>
      _agentSessions[id] ?? (throw StateError('No fixture session receipt'));

  // Accepted tasks now refresh the authoritative Agent workspace transcript.
  // This scope-counting fixture has no native history to add to the next draft.
  @override
  Future<List<JournalEntry>> agentWorkspaceHistory({
    required String sessionId,
    int after = 0,
  }) async => [];

  @override
  Future<List<JournalEntry>> transcript({
    required String sessionId,
    int after = 0,
    int limit = 200,
  }) async => [];
  final turnScopes = <ContentScope>[];
  final transcriptionScopes = <ContentScope>[];
  final taskScopes = <ContentScope>[];
  final commandScopes = <ContentScope>[];
  final steerScopes = <ContentScope>[];
  final windows = <Map<String, Object?>>[];
  Completer<Map<String, Object?>>? transcriptionGate;
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
    turnScopes.add(contentScope);
    yield ConverseEvent('done', {'type': 'done'});
  }

  @override
  Future<Map<String, Object?>> transcribe(
    List<int> bytes,
    String filename, {
    ContentScope contentScope = ContentScope.localOnly,
  }) async {
    transcriptionScopes.add(contentScope);
    return transcriptionGate == null
        ? {'text': 'fixture', 'content_scope': 'public_synthetic'}
        : transcriptionGate!.future;
  }

  @override
  Future<AgentTask> createTask({
    required String sessionId,
    required String threadId,
    required int epoch,
    required String objective,
    required String idempotencyKey,
    ContentScope contentScope = ContentScope.localOnly,
  }) async {
    taskScopes.add(contentScope);
    _agentSessions[sessionId] = SessionInfo(
      sessionId: sessionId,
      threadId: threadId,
      epoch: epoch,
      channel: 'agent',
    );
    return cardTask();
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
    commandScopes.add(contentScope);
    _agentSessions[sessionId] = SessionInfo(
      sessionId: sessionId,
      threadId: threadId,
      epoch: epoch,
      channel: 'agent',
    );
    return {'status': 'ok'};
  }

  @override
  Future<AgentTask> steerTask({
    required String taskId,
    required String instruction,
    required int expectedObjectiveRevision,
    required String idempotencyKey,
    ContentScope contentScope = ContentScope.localOnly,
  }) async {
    steerScopes.add(contentScope);
    return cardTask();
  }

  @override
  Future<Map<String, Object?>> transcribeWindow(
    Map<String, Object?> window,
  ) async {
    windows.add(window);
    return {};
  }
}

class RecordingAudio extends CaptureAudio {
  @override
  Future<RecordedAudio?> stopRecording() async =>
      RecordedAudio('audio/webm', [1, 2, 3]);
}

class ImportAudio extends RecordingAudio {
  final chosen = Completer<RecordedAudio?>();
  @override
  Future<RecordedAudio?> chooseRecording() => chosen.future;
}

AppState withClient(ScopeClient api, BackendProfile profile) {
  final state = AppState()..connect(profileWith(profile), injected: api);
  state.converseProvider = localProvider();
  state.agentProvider = localProvider();
  state.converseSession = SessionInfo(
    sessionId: 's',
    threadId: 't',
    channel: 'converse',
    epoch: 1,
  );
  state.agentSession = SessionInfo(
    sessionId: 'sa',
    threadId: 'ta',
    channel: 'agent',
    epoch: 1,
  );
  return state;
}

Widget character(AppState state, CaptureAudio audio) => MaterialApp(
  home: Scaffold(
    body: AnimatedBuilder(
      animation: state,
      builder: (_, _) => CharacterView(state: state, audio: audio),
    ),
  ),
);

void main() {
  for (final scope in ContentScope.values) {
    test('actual JSON and multipart carry ${scope.wireValue}', () async {
      final transport = BodyTransport();
      final api = KinaiyaClient(
        sessionBase: 'http://session.invalid',
        agentBase: 'http://agent.invalid',
        converseBase: 'http://converse.invalid',
        ownerToken: 'synthetic',
        httpClient: transport,
      );
      await api
          .streamTurn(
            sessionId: 's',
            threadId: 't',
            epoch: 1,
            text: 'fixture',
            idempotencyKey: 'k',
            contentScope: scope,
          )
          .drain<void>();
      final task = await api.createTask(
        sessionId: 's',
        threadId: 't',
        epoch: 1,
        objective: 'fixture',
        idempotencyKey: 'k',
        contentScope: scope,
      );
      expect(task.contentScope, scope);
      await api.steerTask(
        taskId: 't',
        instruction: 'fixture',
        expectedObjectiveRevision: 1,
        idempotencyKey: 'k',
        contentScope: scope,
      );
      await api.executeCommand(
        sessionId: 's',
        threadId: 't',
        epoch: 1,
        input: '/skill fixture',
        idempotencyKey: 'k',
        contentScope: scope,
      );
      await api.transcribe([1], 'fixture.wav', contentScope: scope);
      expect(transport.bodies, hasLength(4));
      expect(
        transport.bodies.map((b) => b['content_scope']),
        everyElement(scope.wireValue),
      );
      expect(transport.multipartScopes, [scope.wireValue]);
      api.close();
    });
  }

  test('legacy API calls explicitly default to local_only', () async {
    final transport = BodyTransport();
    final api = KinaiyaClient(
      sessionBase: 'http://session.invalid',
      agentBase: 'http://agent.invalid',
      converseBase: 'http://converse.invalid',
      ownerToken: 'synthetic',
      httpClient: transport,
    );
    await api
        .streamTurn(
          sessionId: 's',
          threadId: 't',
          epoch: 1,
          text: 'fixture',
          idempotencyKey: 'k',
        )
        .drain<void>();
    await api.createTask(
      sessionId: 's',
      threadId: 't',
      epoch: 1,
      objective: 'fixture',
      idempotencyKey: 'k',
    );
    await api.steerTask(
      taskId: 't',
      instruction: 'fixture',
      expectedObjectiveRevision: 1,
      idempotencyKey: 'k',
    );
    await api.executeCommand(
      sessionId: 's',
      threadId: 't',
      epoch: 1,
      input: '/status',
      idempotencyKey: 'k',
    );
    await api.transcribe([1], 'fixture.wav');
    expect(
      transport.bodies.map((b) => b['content_scope']),
      everyElement('local_only'),
    );
    expect(transport.multipartScopes, ['local_only']);
    api.close();
  });

  test(
    'existing consent meanings and native target restrictions stay distinct',
    () {
      expect(
        ConnectionProfile.defaults().backendProfile.contentScope,
        ContentScope.localOnly,
      );
      expect(
        BackendProfile.metaContributor.contentScope,
        ContentScope.publicSynthetic,
      );
      expect(
        BackendProfile.cloudDevelopment.contentScope,
        ContentScope.publicSynthetic,
      );
      expect(
        BackendProfile.privateCloudAuthorized.contentScope,
        ContentScope.privateCloudAuthorized,
      );
      expect(
        AppState.providerAllows(
          contributorProvider(),
          BackendProfile.privateCloudAuthorized,
        ),
        isFalse,
      );
      expect(
        AppState.providerAllows({
          'provider': 'cloud',
          'model': 'ordinary',
          'local': false,
        }, BackendProfile.privateCloudAuthorized),
        isTrue,
      );
      for (final info in [
        contributorProvider(),
        {'provider': 'other', 'model': 'other-Contributor', 'local': false},
      ]) {
        expect(
          AppState.providerAllowsScope(info, ContentScope.localOnly),
          isFalse,
        );
        expect(
          AppState.providerAllowsScope(
            info,
            ContentScope.privateCloudAuthorized,
          ),
          isFalse,
        );
        expect(
          AppState.providerAllowsScope(info, ContentScope.publicSynthetic),
          isTrue,
        );
      }
    },
  );

  test('reconnect, edits and unknown restored drafts never upgrade scope', () {
    final state = withClient(ScopeClient(), BackendProfile.clean);
    for (final view in AppView.values) {
      state.setDraft(view, 'private fixture');
    }
    state.connect(
      profileWith(BackendProfile.cloudDevelopment),
      injected: ScopeClient(),
    );
    for (final view in AppView.values) {
      expect(state.draftScope(view), ContentScope.localOnly);
      state.setDraft(view, 'private fixture plus edit');
      expect(state.draftScope(view), ContentScope.localOnly);
      state.setDraft(view, '');
      state.setDraft(view, 'new public fixture');
      expect(state.draftScope(view), ContentScope.publicSynthetic);
    }
    state.agentDraft = 'restored unknown fixture';
    expect(state.draftScope(AppView.agent), ContentScope.localOnly);
    state.connection = profileWith(BackendProfile.privateCloudAuthorized);
    state.setDraft(AppView.character, 'private addition');
    expect(
      state.draftScope(AppView.character),
      ContentScope.privateCloudAuthorized,
    );
    state.connection = profileWith(BackendProfile.metaContributor);
    state.setDraft(AppView.character, 'another edit');
    expect(
      state.draftScope(AppView.character),
      ContentScope.privateCloudAuthorized,
    );
    state.dispose();
  });

  testWidgets('typed Converse sends pinned scope and retains slash-like text', (
    tester,
  ) async {
    final api = ScopeClient();
    final state = withClient(api, BackendProfile.privateCloudAuthorized);
    await tester.pumpWidget(character(state, RecordingAudio()));
    await tester.pump(const Duration(milliseconds: 100));
    await tester.enterText(
      find.byType(TextField),
      '/status ordinary conversation',
    );
    state.connection = profileWith(BackendProfile.metaContributor);
    await tester.testTextInput.receiveAction(TextInputAction.send);
    await tester.pump(const Duration(milliseconds: 100));
    expect(api.turnScopes, [ContentScope.privateCloudAuthorized]);
    expect(api.commandScopes, isEmpty);
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
  });

  testWidgets('private draft blocked on cloud after reconnect until cleared', (
    tester,
  ) async {
    final api = ScopeClient();
    final state = withClient(api, BackendProfile.clean);
    state.setDraft(AppView.agent, 'private fixture');
    state.connect(profileWith(BackendProfile.cloudDevelopment), injected: api);
    state.agentProvider = {
      'provider': 'cloud',
      'model': 'ordinary',
      'local': false,
    };
    state.agentSession = SessionInfo(
      sessionId: 's',
      threadId: 't',
      channel: 'agent',
      epoch: 1,
    );
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: AnimatedBuilder(
            animation: state,
            builder: (_, _) => AgentChat(state: state),
          ),
        ),
      ),
    );
    await tester.pump(const Duration(milliseconds: 100));
    final input = find.byKey(const ValueKey('agent-input'));
    await tester.showKeyboard(input);
    await tester.tap(input);
    await tester.testTextInput.receiveAction(TextInputAction.send);
    await tester.pump(const Duration(milliseconds: 100));
    expect(api.taskScopes, isEmpty);
    expect(state.agentDraft, 'private fixture');
    await tester.enterText(input, '');
    await tester.enterText(input, 'public fixture');
    await tester.testTextInput.receiveAction(TextInputAction.send);
    await tester.pump(const Duration(milliseconds: 100));
    expect(api.taskScopes, [ContentScope.publicSynthetic]);
    await tester.enterText(input, '/skill fixture');
    await tester.testTextInput.receiveAction(TextInputAction.send);
    await tester.pump(const Duration(milliseconds: 100));
    expect(api.commandScopes, [ContentScope.publicSynthetic]);
    state.agentInferenceAllowed = false;
    state.touch();
    await tester.enterText(input, '/status');
    await tester.testTextInput.receiveAction(TextInputAction.send);
    await tester.pump(const Duration(milliseconds: 100));
    expect(api.commandScopes, [
      ContentScope.publicSynthetic,
      ContentScope.publicSynthetic,
    ]);
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
  });

  testWidgets(
    'one-shot capture and STT response cannot upgrade private input',
    (tester) async {
      final api = ScopeClient()
        ..transcriptionGate = Completer<Map<String, Object?>>();
      final state = withClient(api, BackendProfile.clean);
      await tester.pumpWidget(character(state, RecordingAudio()));
      await tester.pump(const Duration(milliseconds: 100));
      await tester.tap(find.byTooltip('Record voice'));
      await tester.pump();
      state.connection = profileWith(BackendProfile.cloudDevelopment);
      await tester.tap(find.byTooltip('Stop recording'));
      await tester.pump();
      expect(api.transcriptionScopes, [ContentScope.localOnly]);
      api.transcriptionGate!.complete({
        'text': 'fixture',
        'content_scope': 'public_synthetic',
      });
      await tester.pump(const Duration(milliseconds: 100));
      expect(api.turnScopes, [ContentScope.localOnly]);
      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
    },
  );

  testWidgets('pending STT cannot submit into reconnected public profile', (
    tester,
  ) async {
    final old = ScopeClient()
      ..transcriptionGate = Completer<Map<String, Object?>>();
    final state = withClient(old, BackendProfile.clean);
    await tester.pumpWidget(character(state, RecordingAudio()));
    await tester.pump(const Duration(milliseconds: 100));
    await tester.tap(find.byTooltip('Record voice'));
    await tester.pump();
    await tester.tap(find.byTooltip('Stop recording'));
    await tester.pump();
    final replacement = ScopeClient();
    state.connect(
      profileWith(BackendProfile.metaContributor),
      injected: replacement,
    );
    await tester.pump();
    old.transcriptionGate!.complete({
      'text': 'fixture',
      'content_scope': 'public_synthetic',
    });
    await tester.pump(const Duration(milliseconds: 100));
    expect(old.turnScopes, isEmpty);
    expect(replacement.turnScopes, isEmpty);
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
  });

  testWidgets('live clip retains armed scope through later selection changes', (
    tester,
  ) async {
    final api = ScopeClient();
    final audio = RecordingAudio();
    final state = withClient(api, BackendProfile.clean);
    await tester.pumpWidget(character(state, audio));
    await tester.pump(const Duration(milliseconds: 100));
    await tester.tap(find.byTooltip('Start Live listen (armed mic)'));
    await tester.pump();
    state.connection = profileWith(BackendProfile.cloudDevelopment);
    audio.events.add({'type': 'speech-end', 'epoch': audio.epoch});
    await tester.pump(const Duration(milliseconds: 150));
    await tester.pump(const Duration(milliseconds: 100));
    expect(api.transcriptionScopes, [ContentScope.localOnly]);
    expect(api.turnScopes, [ContentScope.localOnly]);
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
  });

  testWidgets('imported recording keeps picker-time private scope', (
    tester,
  ) async {
    final api = ScopeClient();
    final audio = ImportAudio();
    final state = withClient(api, BackendProfile.privateCloudAuthorized);
    await tester.pumpWidget(character(state, audio));
    await tester.pump(const Duration(milliseconds: 100));
    await tester.tap(find.byTooltip('Use audio recording'));
    await tester.pump();
    state.connection = profileWith(BackendProfile.metaContributor);
    audio.chosen.complete(RecordedAudio('audio/wav', [1, 2, 3]));
    await tester.pump(const Duration(milliseconds: 100));
    expect(api.transcriptionScopes, [ContentScope.privateCloudAuthorized]);
    expect(api.turnScopes, [ContentScope.privateCloudAuthorized]);
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
  });

  testWidgets('protected STT scope can further restrict public capture', (
    tester,
  ) async {
    final api = ScopeClient()
      ..transcriptionGate = Completer<Map<String, Object?>>();
    final state = withClient(api, BackendProfile.cloudDevelopment);
    await tester.pumpWidget(character(state, RecordingAudio()));
    await tester.pump(const Duration(milliseconds: 100));
    await tester.tap(find.byTooltip('Record voice'));
    await tester.pump();
    await tester.tap(find.byTooltip('Stop recording'));
    await tester.pump();
    api.transcriptionGate!.complete({
      'text': 'fixture',
      'content_scope': 'local_only',
    });
    await tester.pump(const Duration(milliseconds: 100));
    expect(api.transcriptionScopes, [ContentScope.publicSynthetic]);
    expect(api.turnScopes, [ContentScope.localOnly]);
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
  });

  testWidgets('window bodies keep armed scope after a selection change', (
    tester,
  ) async {
    final api = ScopeClient();
    final state = withClient(api, BackendProfile.privateCloudAuthorized)
      ..streamingWindows = true;
    final audio = WindowAudio();
    await tester.pumpWidget(character(state, audio));
    await tester.pump(const Duration(milliseconds: 100));
    await tester.tap(find.byTooltip('Start Live listen (armed mic)'));
    await tester.pump();
    state.connection = profileWith(BackendProfile.cloudDevelopment);
    await tester.pump(const Duration(milliseconds: 1100));
    await tester.pump();
    expect(api.windows, isNotEmpty);
    expect(
      api.windows.map((w) => w['content_scope']),
      everyElement('private_cloud_authorized'),
    );
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
  });

  testWidgets('steering pins input scope until clear and re-entry', (
    tester,
  ) async {
    final api = ScopeClient()..taskList = [cardTask()];
    final state = withClient(api, BackendProfile.privateCloudAuthorized);
    // Task cards now belong to the explicitly selected native session.
    final target = api.taskList.single;
    state.agentSession = SessionInfo(
      sessionId: target.sessionId,
      threadId: target.threadId,
      channel: 'agent',
      epoch: target.epoch,
    );
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: AnimatedBuilder(
            animation: state,
            builder: (_, _) => AgentChat(state: state),
          ),
        ),
      ),
    );
    await tester.pump(const Duration(milliseconds: 100));
    final input = find.byWidgetPredicate(
      (widget) =>
          widget is TextField &&
          widget.decoration?.labelText == 'Steer this task',
    );
    await tester.ensureVisible(input);
    await tester.enterText(input, 'private steering');
    state.connection = profileWith(BackendProfile.cloudDevelopment);
    await tester.enterText(input, 'private steering plus edit');
    await tester.ensureVisible(find.text('Steer'));
    await tester.tap(find.text('Steer'));
    await tester.pump(const Duration(milliseconds: 100));
    expect(api.steerScopes, [ContentScope.privateCloudAuthorized]);
    await tester.enterText(input, 'public steering');
    await tester.tap(find.text('Steer'));
    await tester.pump(const Duration(milliseconds: 100));
    expect(api.steerScopes, [
      ContentScope.privateCloudAuthorized,
      ContentScope.publicSynthetic,
    ]);
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
  });
}
