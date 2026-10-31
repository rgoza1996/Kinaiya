import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/app.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/config/options_navigation.dart';
import 'package:kinaiya_flutter/ui/options_workspace.dart';
import 'package:kinaiya_flutter/presentation/controller.dart';
import 'package:kinaiya_flutter/ui/converse_memory.dart';
import 'package:kinaiya_flutter/ui/mcp_settings.dart';
import 'package:kinaiya_flutter/ui/presentation_settings.dart';
import 'package:kinaiya_flutter/ui/setup_dialog.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/character/package.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/agent_chat.dart';
import 'package:kinaiya_flutter/ui/character_view.dart';
import 'package:kinaiya_flutter/ui/settings_controls.dart';

import 'converse_stage_test.dart' show StageAudio;
import 'model_setup_test.dart' show setupFixture;

Future<CharacterSelection> selection(WidgetTester tester) async =>
    (await tester.runAsync(() => loadDefaultCharacterSelection(rootBundle)))!;

Future<void> routePump(WidgetTester tester) async {
  // Start the modal animation before advancing its clock; the mascot keeps
  // scheduling frames, so pumpAndSettle is not a valid completion condition.
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 350));
  // Agent destinations open after endOfFrame; advance their own animation too.
  await tester.pump(const Duration(milliseconds: 350));
}

class NavigationAudio extends StageAudio {
  int vadCalls = 0, disposals = 0;
  @override
  Future<void> configureVad({required int endSilenceMs}) async {
    vadCalls++;
    await super.configureVad(endSilenceMs: endSilenceMs);
  }

  @override
  Future<void> dispose() async {
    disposals++;
  }
}

class NavigationStorage implements PresentationStorage {
  int writes = 0;
  @override
  bool get persistent => false;
  @override
  Future<String?> read() async => null;
  @override
  Future<void> write(String value) async {
    writes++;
  }
}

class NavigationFixture {
  final requests = <String>[];
  late final transport = MockClient(respond);
  final state = AppState();
  final audio = NavigationAudio();
  final storage = NavigationStorage();
  late final presentation = PresentationController(storage: storage);
  late final api = KinaiyaClient(
    sessionBase: 'http://localhost:8760',
    agentBase: 'http://localhost:8761',
    converseBase: 'http://localhost:8762',
    ownerToken: 'public-synthetic-owner',
    httpClient: transport,
    memoryClientFactory: () => MockClient(respond),
    agentWorkspaceClientFactory: () => MockClient(respond),
  );
  Map<String, Object?> get binding => {
    'schema_version': '1.0',
    'session_id': 's-conv',
    'thread_id': 't-conv',
    'epoch': 1,
  };
  Map<String, Object?> get namespace => {
    ...binding,
    'enabled': false,
    'revision': 0,
    'node_count': 0,
    'status': 'ready',
  };
  Future<http.Response> respond(http.Request request) async {
    final path = request.url.path;
    requests.add('${request.method} $path');
    Object? body;
    if (path == '/health') {
      body = {
        'service': 'synthetic',
        'enabled': true,
        'provider': {
          'provider': 'fixture',
          'model': 'synthetic',
          'local': true,
          'streaming': true,
        },
      };
    } else if (path == '/v1/converse/setup') {
      body = setupFixture();
    } else if (path == '/v1/agent/options/model-selection') {
      body = {
        'schema_version': '1.0',
        'harness_version':
            request.url.queryParameters['harness_version'] ?? 'seed-v1',
        'provider': {
          'provider': 'fixture',
          'model': 'synthetic',
          'local': true,
        },
      };
    } else if (path == '/v1/agent/harnesses') {
      body = {
        'schema_version': '1.0',
        'configured': true,
        'items': [],
        'next_cursor': null,
      };
    } else if (path == '/v1/agent/options') {
      body = {
        'schema_version': '1.0',
        'provider': {
          'provider': 'fixture',
          'model': 'synthetic',
          'local': true,
        },
        'permissions': {
          'allow_write': false,
          'allow_shell': false,
          'network': false,
          'yolo': false,
        },
        'max_steps': 8,
        'tools': ['workspace.read'],
        'models': null,
      };
    } else if (path == '/v1/commands') {
      body = {
        'schema_version': '1.0',
        'commands': [
          {
            'name': 'status',
            'description': 'Synthetic task status',
            'available': true,
            'execution_type': 'runtime_control',
            'syntax': '/status',
          },
        ],
      };
    } else if (path == '/v1/agent/tasks' && request.method == 'GET') {
      body = [
        {
          'schema_version': '1.0',
          'task_id': 'kept-task',
          'session_id': 's-agent',
          'thread_id': 't-agent',
          'epoch': 1,
          'objective': 'Retained synthetic work',
          'objective_revision': 1,
          'state': 'paused',
          'idempotency_key': 'retained-task',
        },
      ];
    } else if (path == '/v1/approvals') {
      body = {'schema_version': '1.0', 'approvals': [], 'next_after': null};
    } else if (path == '/v1/mcp/management') {
      body = {
        'configured': true,
        'revision': 0,
        'blocked': false,
        'servers': [],
      };
    } else if (path == '/v1/converse-memory/state') {
      body = namespace;
    } else if (path == '/v1/converse-memory/catalog') {
      body = {'schema_version': '1.0', 'state': namespace, 'entries': []};
    } else if (path == '/v1/memory/state') {
      body = {
        ...binding,
        'runtime_id': 'runtime',
        'use_generation': 0,
        'enabled': false,
        'implementation_id': null,
        'selector_ids': [],
        'pending': false,
        'notice': 'disabled',
      };
    } else if (path == '/v1/resources') {
      body = {'configured': false};
    } else if (path == '/v1/agents/capabilities') {
      body = {'harness': 'synthetic', 'tools': [], 'cancellation': true};
    } else if (path == '/v1/sessions') {
      body = [
        {...binding, 'channel': 'converse', 'name': 'Converse'},
      ];
    } else if (path == '/v1/transcript/recent') {
      body = [];
    } else {
      return http.Response('Unexpected synthetic request', 404);
    }
    return http.Response(
      jsonEncode(body),
      200,
      headers: {'content-type': 'application/json'},
    );
  }

  Future<void> mount(WidgetTester tester) async {
    state.client = api;
    state.connected = true;
    state.sessionAuthenticated = true;
    state.converseInferenceAllowed = true;
    state.converseSession = SessionInfo.fromJson({
      ...binding,
      'channel': 'converse',
      'name': 'Converse',
    });
    state.setDraft(AppView.character, 'Retained conversation draft');
    state.setDraft(AppView.agent, 'Retained Agent draft');
    await tester.runAsync(presentation.initialize);
    await tester.pumpWidget(
      KinaiyaApp(state: state, audio: audio, presentation: presentation),
    );
    await routePump(tester);
  }

  Future<void> dispose(WidgetTester tester) async {
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
    presentation.dispose();
  }

  bool get dispatchedEffect => requests.any(
    (r) =>
        r.startsWith('POST ') &&
        !{
          'POST /v1/converse-memory/state',
          'POST /v1/converse-memory/catalog',
          'POST /v1/memory/state',
        }.contains(r),
  );
}

Future<void> openSettings(WidgetTester tester) async {
  await tester.tap(find.byKey(const ValueKey('settings-open')));
  await routePump(tester);
  expect(find.byKey(const ValueKey('settings-close')), findsOneWidget);
}

Future<void> chooseSettings(WidgetTester tester, OptionsPage page) async {
  final search = find.byKey(const ValueKey('settings-search'));
  if (search.hitTestable().evaluate().isEmpty) {
    await tester.tap(find.byTooltip('Options navigation'));
    await routePump(tester);
  }
  await tester.enterText(search, page.label);
  await tester.pump();
  final target = find.byKey(ValueKey('options-search-${page.name}'));
  await tester.ensureVisible(target);
  await tester.pump();
  expect(target.hitTestable(), findsOneWidget);
  await tester.tap(target);
  await routePump(tester);
  expect(find.byKey(const ValueKey('settings-close')), findsOneWidget);
  expect(find.byType(OptionsWorkspace), findsOneWidget);
  expect(find.byType(Dialog), findsNothing);
}

void main() {
  testWidgets(
    'voice handle uses its current pane without audio changes on open',
    (tester) async {
      final state = AppState();
      final audio = StageAudio();
      final pack = await selection(tester);
      final a = ConverseViewControls();
      final b = ConverseViewControls();
      Future<void> mount(ConverseViewControls controls) => tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: CharacterView(
              state: state,
              controls: controls,
              audio: audio,
              character: pack,
            ),
          ),
        ),
      );
      await mount(a);
      await tester.pump();
      final stale = a.openVoiceOptions!;
      await mount(b);
      await tester.pump();
      expect(a.openVoiceOptions, isNull);
      await stale();
      await tester.pump();
      expect(find.text('Voice options'), findsOneWidget); // composer only
      final opened = b.openVoiceOptions!();
      await routePump(tester);
      expect(
        find.text('Call for live listening · Tell for one message'),
        findsOneWidget,
      );
      expect(audio.lastVad, isNull);
      expect(audio.arms, 0);
      expect(audio.recordStarts, 0);
      await tester.tap(find.byTooltip('Close voice options'));
      await routePump(tester);
      await opened;
      expect(audio.lastVad, isNull);
      Future<void> newer() async {}
      b.openVoiceOptions = newer;
      await tester.pumpWidget(const SizedBox.shrink());
      expect(identical(b.openVoiceOptions, newer), true);
      await stale();
      expect(tester.takeException(), isNull);
      state.dispose();
    },
  );

  testWidgets('voice sheet cannot configure a replaced pane owner', (
    tester,
  ) async {
    final first = AppState();
    final second = AppState();
    final state = ValueNotifier(first);
    final controls = ConverseViewControls();
    final audio = StageAudio();
    final pack = await selection(tester);
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: ValueListenableBuilder<AppState>(
            valueListenable: state,
            builder: (_, owner, _) => CharacterView(
              state: owner,
              controls: controls,
              audio: audio,
              character: pack,
            ),
          ),
        ),
      ),
    );
    await tester.pump();
    final stale = controls.openVoiceOptions!;
    final opened = stale();
    await routePump(tester);
    state.value = second;
    await tester.pump();
    await tester.tap(find.byType(DropdownButton<int>));
    await routePump(tester);
    await tester.tap(find.text('0.8s').last);
    await routePump(tester);
    expect(audio.lastVad, isNull);
    await tester.tap(find.byTooltip('Close voice options'));
    await routePump(tester);
    await opened;
    await stale();
    await tester.pump();
    expect(
      find.text('Call for live listening · Tell for one message'),
      findsNothing,
    );
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
    first.dispose();
    second.dispose();
  });

  testWidgets(
    'Agent handle is registry current, draft only and rejects old owners',
    (tester) async {
      final first = AppState();
      final second = AppState();
      final a = AgentViewControls();
      final b = AgentViewControls();
      Future<void> mount(AppState state, AgentViewControls controls) =>
          tester.pumpWidget(
            MaterialApp(
              home: Scaffold(
                body: AgentChat(state: state, controls: controls),
              ),
            ),
          );
      await mount(first, a);
      await tester.pump();
      first.setCommands([
        CommandDef(
          name: 'status',
          description: 'Synthetic status',
          executionType: 'runtime_control',
        ),
      ]);
      expect(first.sessionAuthenticated, false);
      a.openCommands!();
      await routePump(tester);
      expect(find.text('/status'), findsOneWidget);
      await tester.tap(find.text('/status'));
      await routePump(tester);
      expect(first.agentDraft, '/status ');
      expect(first.converseDraft, isEmpty);
      expect(first.agentSession, isNull);
      expect(first.tasks, isEmpty);
      final stale = a.openCommands!;
      first.setCommands(null);
      stale();
      await tester.pump();
      expect(find.text('/status'), findsNothing);
      await mount(second, b);
      await tester.pump();
      expect(a.openCommands, isNull);
      second.setCommands([
        CommandDef(
          name: 'permissions',
          description: 'Synthetic permission inspect',
        ),
      ]);
      stale();
      await tester.pump();
      expect(find.text('/permissions'), findsNothing);
      b.openCommands!();
      await routePump(tester);
      expect(find.text('/permissions'), findsOneWidget);
      await tester.tap(find.text('/permissions'));
      await routePump(tester);
      expect(second.agentDraft, '/permissions ');
      void newer() {}
      b.openCommands = newer;
      await tester.pumpWidget(const SizedBox.shrink());
      expect(identical(b.openCommands, newer), true);
      stale();
      expect(tester.takeException(), isNull);
      first.dispose();
      second.dispose();
    },
  );

  testWidgets('command sheet from prior pane cannot fill a replacement draft', (
    tester,
  ) async {
    final first = AppState();
    final second = AppState();
    final state = ValueNotifier(first);
    final controls = AgentViewControls();
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: ValueListenableBuilder<AppState>(
            valueListenable: state,
            builder: (_, owner, _) =>
                AgentChat(state: owner, controls: controls),
          ),
        ),
      ),
    );
    first.setCommands([CommandDef(name: 'status', description: 'Synthetic')]);
    controls.openCommands!();
    await routePump(tester);
    state.value = second;
    second.setCommands([CommandDef(name: 'status', description: 'Synthetic')]);
    await tester.pump();
    await tester.tap(find.text('/status'));
    await tester.pump();
    expect(second.agentDraft, isEmpty);
    expect(first.agentDraft, isEmpty);
    Navigator.of(tester.element(find.byType(ListTile).last)).pop();
    await routePump(tester);
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
    first.dispose();
    second.dispose();
  });
  testWidgets(
    'Options dismissal keeps armed capture panes drafts and tasks intact',
    (tester) async {
      await tester.binding.setSurfaceSize(const Size(1000, 900));
      addTearDown(() => tester.binding.setSurfaceSize(null));
      final f = NavigationFixture();
      await f.mount(tester);
      final characterState = tester.state(find.byType(CharacterView));
      final agentState = tester.state(
        find.byType(AgentChat, skipOffstage: false),
      );
      await tester.ensureVisible(find.byKey(const ValueKey('home-talk')));
      await tester.pump();
      await tester.tap(find.byKey(const ValueKey('home-talk')));
      await routePump(tester);
      expect(f.audio.arms, 1);
      final vad = f.audio.vadCalls;
      final draft = f.state.converseDraft;
      final agentDraft = f.state.agentDraft;
      final before = f.requests.length;
      await openSettings(tester);
      expect(find.byTooltip('Appearance'), findsOneWidget);
      expect(find.byTooltip('Connection setup'), findsOneWidget);
      await tester.tap(find.byKey(const ValueKey('settings-close')));
      await routePump(tester);
      expect(
        f.requests.skip(before),
        everyElement(
          isIn({
            'GET /v1/converse/setup',
            'GET /v1/agent/options',
            'GET /v1/agent/harnesses',
          }),
        ),
      );
      expect(f.audio.vadCalls, vad);
      expect(f.audio.arms, 1);
      expect(f.audio.disposals, 0);
      expect(
        identical(tester.state(find.byType(CharacterView)), characterState),
        true,
      );
      expect(
        identical(
          tester.state(find.byType(AgentChat, skipOffstage: false)),
          agentState,
        ),
        true,
      );
      expect(f.state.converseDraft, draft);
      expect(f.state.agentDraft, agentDraft);
      expect(f.state.tasks.single.taskId, 'kept-task');
      expect(f.dispatchedEffect, false);
      expect(find.text('End conversation'), findsOneWidget);
      await f.dispose(tester);
    },
  );

  testWidgets(
    'Options embeds Connection Appearance Memory and Voice owners in place',
    (tester) async {
      final f = NavigationFixture();
      await f.mount(tester);
      await openSettings(tester);
      await chooseSettings(tester, OptionsPage.connection);
      expect(find.byType(SetupDialog), findsOneWidget);
      expect(
        tester.widget<SetupDialog>(find.byType(SetupDialog)).embedded,
        isTrue,
      );
      await tester.tap(find.byTooltip('Close options'));
      await routePump(tester);
      await openSettings(tester);
      await chooseSettings(tester, OptionsPage.appearance);
      expect(find.byType(PresentationSettings), findsOneWidget);
      expect(
        tester
            .widget<PresentationSettings>(find.byType(PresentationSettings))
            .embedded,
        isTrue,
      );
      await tester.tap(find.byTooltip('Close options'));
      await routePump(tester);
      await openSettings(tester);
      await chooseSettings(tester, OptionsPage.memory);
      expect(find.byType(ConverseMemoryDialog), findsOneWidget);
      expect(
        tester
            .widget<ConverseMemoryDialog>(find.byType(ConverseMemoryDialog))
            .embedded,
        isTrue,
      );
      await tester.tap(find.byTooltip('Close options'));
      await routePump(tester);
      await openSettings(tester);
      await chooseSettings(tester, OptionsPage.voice);
      expect(
        find.text('Call for live listening · Tell for one message'),
        findsOneWidget,
      );
      await tester.tap(find.byTooltip('Close options'));
      await routePump(tester);
      expect(f.storage.writes, 0);
      expect(f.audio.vadCalls, 0);
      expect(f.audio.disposals, 0);
      expect(f.dispatchedEffect, false);
      expect(f.state.view, AppView.character);
      await f.dispose(tester);
    },
  );

  testWidgets(
    'Agent and MCP Options preserve the selected workspace without inference',
    (tester) async {
      final f = NavigationFixture();
      await f.mount(tester);
      f.state.sessionAuthenticated = false;
      f.state.agentInferenceAllowed = false;
      f.state.touch();
      await tester.pump();
      expect(f.state.commandsAvailable, true);
      await openSettings(tester);
      await chooseSettings(tester, OptionsPage.agent);
      expect(f.state.view, AppView.character);
      expect(find.text('Task step limit'), findsOneWidget);
      expect(f.state.agentDraft, 'Retained Agent draft');
      expect(f.state.converseDraft, 'Retained conversation draft');
      await openSettings(tester);
      await chooseSettings(tester, OptionsPage.integrations);
      expect(f.state.view, AppView.character);
      expect(
        tester.widget<McpSettings>(find.byType(McpSettings)).embedded,
        isTrue,
      );
      await tester.tap(find.byTooltip('Close options'));
      await routePump(tester);
      expect(f.dispatchedEffect, false);
      expect(f.audio.disposals, 0);
      expect(f.state.tasks.single.taskId, 'kept-task');
      await f.dispose(tester);
    },
  );

  testWidgets(
    'Options clears metadata on disconnect and rereads after reconnect',
    (tester) async {
      final f = NavigationFixture();
      await f.mount(tester);
      await openSettings(tester);
      f.state.disconnect();
      await routePump(tester);
      await chooseSettings(tester, OptionsPage.models);
      expect(
        find.text('Connect to your host to inspect models and services.'),
        findsOneWidget,
      );
      expect(find.byKey(const ValueKey('settings-close')), findsOneWidget);
      final next = NavigationFixture();
      f.state.connect(
        ConnectionProfile(
          sessionUrl: 'http://localhost:8760',
          agentUrl: 'http://localhost:8761',
          converseUrl: 'http://localhost:8762',
          ownerToken: 'public-synthetic-owner',
        ),
        injected: next.api,
      );
      await routePump(tester);
      expect(f.state.commandsAvailable, true);
      await chooseSettings(tester, OptionsPage.agent);
      expect(find.text('synthetic'), findsOneWidget);
      expect(
        find.text('Connect to your host to inspect models and services.'),
        findsNothing,
      );
      expect(f.state.view, AppView.character);
      expect(find.text('/status'), findsNothing);
      expect(f.dispatchedEffect, false);
      expect(next.dispatchedEffect, false);
      await f.dispose(tester);
      next.state.dispose();
      next.presentation.dispose();
    },
  );

  testWidgets(
    'Options navigation and embedded Voice remain reachable at320px2x',
    (tester) async {
      await tester.binding.setSurfaceSize(const Size(320, 480));
      tester.platformDispatcher.textScaleFactorTestValue = 2;
      addTearDown(() {
        tester.platformDispatcher.clearTextScaleFactorTestValue();
        return tester.binding.setSurfaceSize(null);
      });
      final f = NavigationFixture();
      await f.mount(tester);
      await openSettings(tester);
      expect(tester.takeException(), isNull);
      await chooseSettings(tester, OptionsPage.voice);
      expect(find.byTooltip('Close options'), findsOneWidget);
      expect(tester.takeException(), isNull);
      await tester.tap(find.byTooltip('Close options'));
      await routePump(tester);
      expect(f.audio.vadCalls, 0);
      expect(f.dispatchedEffect, false);
      await f.dispose(tester);
    },
  );
}
