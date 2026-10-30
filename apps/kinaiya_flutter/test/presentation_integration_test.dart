import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/app.dart';
import 'package:kinaiya_flutter/presentation/controller.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/agent_chat.dart';
import 'package:kinaiya_flutter/ui/character_view.dart';
import 'package:kinaiya_flutter/ui/moonlit_scene.dart';
import 'package:kinaiya_flutter/ui/widgets.dart';

import 'concept_layout_test.dart'
    show
        LayoutApi,
        LayoutAudio,
        wired,
        task,
        settle,
        close,
        openConverseChat,
        closeConverseChat,
        navigateTo;
import 'mascot_sprites_test.dart' show makePng, buildV2, decoded, currentFrame;

class MemoryPresentationStorage implements PresentationStorage {
  String? saved;
  @override
  bool get persistent => true;
  @override
  Future<String?> read() async => saved;
  @override
  Future<void> write(String value) async {
    saved = value;
  }
}

String syntheticPackage({int variant = 0}) {
  final sheet = makePng(8, 8, variant: variant);
  final manifest = jsonDecode(
    buildV2(
      sheet,
      topOverrides: {
        'pack_id': 'public-synthetic-$variant',
        'name': 'Public synthetic character $variant',
        'theme': {'primary': '#12693C', 'background': '#F4FFF5'},
      },
    ),
  );
  return jsonEncode({
    'package_schema': 1,
    'manifest': manifest,
    'files': [
      {
        'path': 'assets/character/kinaiya-core-v1.png',
        'base64': base64Encode(sheet),
      },
      {
        'path': 'assets/mascot/kinaiya-mascot.png',
        'base64': base64Encode(makePng(2, 2)),
      },
      {
        'path': 'assets/character/prompt-test.md',
        'base64': base64Encode(
          utf8.encode('Public synthetic fixture. No personal data.'),
        ),
      },
    ],
  });
}

class PresentationApi extends LayoutApi {
  int commands = 0;
  final approval = ApprovalRequest(
    approvalId: 'public-approval',
    taskId: 'waiting',
    tool: 'write_file',
    actionDigest: 'public-digest',
    objectiveRevision: 2,
    sessionId: 'agent',
    threadId: 'thread-agent',
    epoch: 1,
  );
  @override
  Future<PendingApprovalPage?> pendingApprovals({String? after}) async =>
      PendingApprovalPage([approval], null);
  @override
  Future<Map<String, Object?>?> executeCommand({
    required String sessionId,
    required String threadId,
    required int epoch,
    required String input,
    required String idempotencyKey,
    ContentScope contentScope = ContentScope.localOnly,
  }) async {
    commands++;
    throw StateError('Settings cannot execute commands');
  }
}

void expectVoiceOnlyMain() {
  final main = find.byType(CharacterView);
  expect(
    find.descendant(
      of: main,
      matching: find.byWidgetPredicate((widget) => widget is ButtonStyleButton),
    ),
    findsNWidgets(2),
  );
  expect(find.byKey(const ValueKey('call-kinaiya')), findsOneWidget);
  expect(find.byKey(const ValueKey('home-setup')), findsOneWidget);
  expect(find.byKey(const ValueKey('converse-input')), findsNothing);
  expect(find.byKey(const ValueKey('voice-options')), findsNothing);
  expect(find.byKey(const ValueKey('converse-memory')), findsNothing);
}

void main() {
  testWidgets(
    'live pack and palette replacement preserves capture and worker identity',
    (tester) async {
      final storage = MemoryPresentationStorage();
      final presentation = PresentationController(storage: storage);
      await tester.runAsync(presentation.initialize);
      final selection = await tester.runAsync(
        () => presentation.inspectPackage(syntheticPackage(variant: 1)),
      );
      final api = PresentationApi()
        ..fixtures = [
          task('one', 'running'),
          task('waiting', 'awaiting_approval'),
        ];
      final audio = LayoutAudio();
      final state = wired(api);
      final converse = state.converseSession, agent = state.agentSession;
      await tester.pumpWidget(
        KinaiyaApp(state: state, audio: audio, presentation: presentation),
      );
      await settle(tester);
      await decoded(tester);
      expect(currentFrame(tester).image.width, 1254);
      final characterState = tester.state(find.byType(CharacterView));
      final agentState = tester.state(
        find.byType(AgentChat, skipOffstage: false),
      );
      await openConverseChat(tester);
      await tester.enterText(
        find.byKey(const ValueKey('converse-input')),
        'Keep Converse draft',
      );
      await closeConverseChat(tester);
      await tester.ensureVisible(
        find.byTooltip('Start Live listen (armed mic)'),
      );
      await tester.pump(const Duration(milliseconds: 200));
      expect(
        find.byTooltip('Start Live listen (armed mic)').hitTestable(),
        findsOneWidget,
      );
      await tester.tap(find.byTooltip('Start Live listen (armed mic)'));
      await settle(tester);
      expect(audio.starts, 1);
      await presentation.useCharacter(selection!);
      await tester.pump();
      final replacedRenderer = tester.widget<MascotWidget>(
        find.byType(MascotWidget),
      );
      expect(identical(replacedRenderer.pack, selection.pack), isTrue);
      expect(identical(replacedRenderer.bundle, selection.bundle), isTrue);
      await settle(tester);
      await decoded(tester);
      expect(currentFrame(tester).image.width, 8);
      expect(currentFrame(tester).src.width, 4);
      expectVoiceOnlyMain();
      final scene = tester.widget<MoonlitScene>(find.byType(MoonlitScene));
      expect(scene.primary, const Color(0xFF12693C));
      expect(scene.background, const Color(0xFFF4FFF5));
      expect(
        Theme.of(
          tester.element(find.byType(CharacterView)),
        ).colorScheme.primary,
        const Color(0xFF12693C),
      );
      await presentation.setPreferences(
        presentation.preferences.copyWith(
          reducedMotion: true,
          primaryOverride: '#7A2255',
          backgroundOverride: '#FFF4FA',
        ),
      );
      await settle(tester);
      expect(
        tester.widget<MoonlitScene>(find.byType(MoonlitScene)).primary,
        const Color(0xFF7A2255),
      );
      expect(
        MediaQuery.of(
          tester.element(find.byType(CharacterView)),
        ).disableAnimations,
        isTrue,
      );
      expectVoiceOnlyMain();
      await openConverseChat(tester);
      expect(
        tester
            .widget<TextField>(find.byKey(const ValueKey('converse-input')))
            .controller!
            .text,
        'Keep Converse draft',
      );
      expect(
        find.byKey(const ValueKey('voice-options')).hitTestable(),
        findsOneWidget,
      );
      expect(
        find.byKey(const ValueKey('converse-memory')).hitTestable(),
        findsOneWidget,
      );
      expect(
        find.descendant(
          of: find.byType(Dialog),
          matching: find.byKey(const ValueKey('call-kinaiya')),
        ),
        findsNothing,
      );
      expect(
        find.descendant(
          of: find.byType(Dialog),
          matching: find.byKey(const ValueKey('tell-kinaiya')),
        ),
        findsOneWidget,
      );
      await closeConverseChat(tester);
      expectVoiceOnlyMain();
      expect(audio.starts, 1);
      expect(audio.micStops, 0);
      expect(api.transcriptions, 0);
      await navigateTo(tester, AppView.agent);
      await tester.enterText(
        find.byKey(const ValueKey('agent-input')),
        'Keep Agent draft',
      );
      expect(find.text('Live microphone on'), findsOneWidget);
      expect(find.byTooltip('Appearance'), findsOneWidget);
      expect(state.approvals.single.approvalId, 'public-approval');
      await navigateTo(tester, AppView.character);
      expect(
        identical(tester.state(find.byType(CharacterView)), characterState),
        isTrue,
      );
      expect(
        identical(
          tester.state(find.byType(AgentChat, skipOffstage: false)),
          agentState,
        ),
        isTrue,
      );
      expect(identical(state.converseSession, converse), isTrue);
      expect(identical(state.agentSession, agent), isTrue);
      expect(state.converseDraft, 'Keep Converse draft');
      expect(state.agentDraft, 'Keep Agent draft');
      expect(state.tasks.map((t) => t.state), ['running', 'awaiting_approval']);
      expect(audio.starts, 1);
      expect(audio.micStops, 0);
      expect(audio.disposals, 0);
      expect(api.commands, 0);
      expect(api.transcriptions, 0);
      expect(api.interrupts, 0);
      expect(api.createdSessions, 0);
      expect(tester.takeException(), isNull);
      await close(tester, state);
      presentation.dispose();
    },
  );

  testWidgets(
    'non-character fallback retains transcript and settings do not persist runtime data',
    (tester) async {
      final storage = MemoryPresentationStorage();
      final presentation = PresentationController(storage: storage);
      await tester.runAsync(presentation.initialize);
      final api = LayoutApi();
      final state = wired(api)
        ..converseHistory.add(
          ChatMsg(role: 'assistant', text: 'Synthetic caption survives.'),
        );
      await tester.pumpWidget(
        KinaiyaApp(
          state: state,
          audio: LayoutAudio(),
          presentation: presentation,
        ),
      );
      await settle(tester);
      await presentation.setPreferences(
        presentation.preferences.copyWith(
          showCharacter: false,
          showCaptions: false,
        ),
      );
      await settle(tester);
      expect(find.byType(MascotWidget), findsNothing);
      expect(find.text('Synthetic caption survives.'), findsOneWidget);
      expect(
        tester.widget<MoonlitScene>(find.byType(MoonlitScene)).enabled,
        isFalse,
      );
      final persisted = jsonDecode(storage.saved!) as Map<String, dynamic>;
      expect(persisted.keys.toSet(), {
        'state_schema',
        'preferences',
        'package',
      });
      expect(storage.saved, isNot(contains('Synthetic caption survives.')));
      expect(
        storage.saved,
        isNot(contains('synthetic')),
      ); // Fixture credential never enters settings.
      expect(tester.takeException(), isNull);
      await close(tester, state);
      presentation.dispose();
    },
  );
}
