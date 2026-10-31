import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/app.dart';
import 'package:kinaiya_flutter/audio/speech_audio.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/character_view.dart';
import 'package:kinaiya_flutter/ui/anchored_moonlit_scene.dart';
import 'package:kinaiya_flutter/ui/visual_theme.dart';

import 'client_test.dart' show mockTransport;
import 'live_capture_test.dart' show CaptureAudio;

// Public synthetic transport and audio adapter. No service or model calls.
class LayoutApi extends KinaiyaClient {
  LayoutApi()
    : super(
        sessionBase: 'http://fixture',
        agentBase: 'http://fixture',
        converseBase: 'http://fixture',
        ownerToken: 'synthetic',
        httpClient: mockTransport(commandsMissing: false),
      );
  int createdSessions = 0, transcriptions = 0, interrupts = 0;
  List<AgentTask> fixtures = [];
  @override
  Future<List<AgentTask>> listTasks() async => fixtures;
  @override
  Future<SessionInfo> createSession(String channel, {String name = ''}) {
    createdSessions++;
    return super.createSession(channel, name: name);
  }

  @override
  Future<Map<String, Object?>> transcribe(
    List<int> bytes,
    String filename, {
    ContentScope contentScope = ContentScope.localOnly,
  }) async {
    transcriptions++;
    return {'text': ''};
  }

  @override
  Future<AgentTask> interruptTask({
    required String taskId,
    required int expectedObjectiveRevision,
    required String idempotencyKey,
    String reason = 'User requested cancellation',
  }) async {
    interrupts++;
    return fixtures.firstWhere((t) => t.taskId == taskId);
  }
}

class LayoutAudio extends CaptureAudio {
  int starts = 0,
      micStops = 0,
      recordStarts = 0,
      recordCancels = 0,
      disposals = 0;
  bool rejectStop = false;
  Completer<void>? pendingStop;
  @override
  Future<String> startArmed() async {
    starts++;
    return 'armed';
  }

  @override
  Future<void> stopArmed() async {
    micStops++;
    if (rejectStop) throw StateError('Synthetic stop not acknowledged');
    await pendingStop?.future;
  }

  @override
  Future<String> startRecording() async {
    recordStarts++;
    return 'recording';
  }

  @override
  Future<void> cancelRecording() async {
    recordCancels++;
  }

  @override
  Future<void> dispose() async {
    disposals++;
  }
}

class WindowLayoutAudio extends LayoutAudio implements WindowSpeechAudio {
  Completer<void>? pendingPlaybackStop;

  @override
  Future<void> stopPlayback() async {
    stops++;
    await pendingPlaybackStop?.future;
  }

  @override
  Future<void> configureWindows(bool enabled) async {}
  @override
  Future<int> nextUtterance() async => ++epoch;
  @override
  Future<Map<String, Object?>?> snapshotWindow() async => null;
  @override
  Future<bool> get userSpeaking async => false;
}

SessionInfo session(String id, String channel) => SessionInfo(
  sessionId: id,
  threadId: 'thread-$id',
  channel: channel,
  epoch: 1,
  name: 'Synthetic $channel',
);

AgentTask task(String id, String state) => AgentTask(
  taskId: id,
  sessionId: 'agent',
  threadId: 'thread-agent',
  epoch: 1,
  objective: 'Synthetic task $id',
  objectiveRevision: 2,
  state: state,
  idempotencyKey: 'synthetic-$id',
);

AppState wired(LayoutApi api) => AppState()
  ..connect(ConnectionProfile.defaults(), injected: api)
  ..converseSession = session('converse', 'converse')
  ..agentSession = session('agent', 'agent');

Future<void> settle(WidgetTester tester) async {
  await tester.pump(const Duration(milliseconds: 200));
  await tester.pump(const Duration(milliseconds: 20));
}

Future<void> close(WidgetTester tester, AppState state) async {
  await tester.pumpWidget(const SizedBox.shrink());
  await tester.pump();
  state.dispose();
}

Future<void> openConverseChat(WidgetTester tester) async {
  if (find.byKey(const ValueKey('converse-input')).evaluate().isNotEmpty) {
    return;
  }
  await tester.tap(find.byTooltip('Converse chat'));
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 300));
  expect(find.byKey(const ValueKey('converse-input')), findsOneWidget);
}

Future<void> closeConverseChat(WidgetTester tester) async {
  final closeChat = find.byTooltip('Close Converse chat').hitTestable();
  if (closeChat.evaluate().isEmpty) return;
  await tester.tap(closeChat);
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 300));
}

Future<void> navigateTo(WidgetTester tester, AppView view) async {
  await closeConverseChat(tester);
  final destination = find.byKey(
    ValueKey(
      view == AppView.agent ? 'navigation-agent' : 'navigation-converse',
    ),
  );
  if (destination.hitTestable().evaluate().isEmpty) {
    await tester.tap(find.byKey(const ValueKey('navigation-menu')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));
  }
  expect(destination.hitTestable(), findsOneWidget);
  await tester.tap(destination.hitTestable());
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 300));
  await settle(tester);
}

void main() {
  testWidgets(
    'navigation retains armed capture, sessions and separate drafts',
    (tester) async {
      final api = LayoutApi()..fixtures = [task('one', 'running')];
      final audio = LayoutAudio();
      final state = wired(api);
      final converse = state.converseSession, agent = state.agentSession;
      await tester.pumpWidget(KinaiyaApp(state: state, audio: audio));
      await settle(tester);
      final character = tester.state(find.byType(CharacterView));
      await openConverseChat(tester);
      await tester.enterText(
        find.byKey(const ValueKey('converse-input')),
        'Conversation draft',
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
      expect(audio.clips, 1);
      await navigateTo(tester, AppView.agent);
      expect(find.text('Live microphone on'), findsOneWidget);
      expect(find.byTooltip('Start Live listen (armed mic)'), findsNothing);
      expect(audio.micStops, 0);
      expect(audio.disposals, 0);
      expect(
        identical(
          tester.state(find.byType(CharacterView, skipOffstage: false)),
          character,
        ),
        isTrue,
      );
      await tester.enterText(
        find.byKey(const ValueKey('agent-input')),
        'Agent draft',
      );
      await tester.tap(find.text('Stop microphone'));
      await settle(tester);
      expect(audio.micStops, 1);
      expect(audio.recordCancels, 1);
      expect(api.interrupts, 0);
      expect(api.fixtures.single.state, 'running');
      await navigateTo(tester, AppView.character);
      await openConverseChat(tester);
      expect(
        tester
            .widget<TextField>(find.byKey(const ValueKey('converse-input')))
            .controller!
            .text,
        'Conversation draft',
      );
      expect(state.converseDraft, 'Conversation draft');
      expect(state.agentDraft, 'Agent draft');
      expect(identical(state.converseSession, converse), isTrue);
      expect(identical(state.agentSession, agent), isTrue);
      expect(api.createdSessions, 0);
      expect(find.byTooltip('Agent commands'), findsNothing);
      expect(tester.takeException(), isNull);
      await close(tester, state);
      expect(audio.disposals, 1);
    },
  );

  testWidgets('global microphone stop cancels a recording without sending it', (
    tester,
  ) async {
    final api = LayoutApi();
    final audio = LayoutAudio();
    final state = wired(api);
    await tester.pumpWidget(KinaiyaApp(state: state, audio: audio));
    await settle(tester);
    await openConverseChat(tester);
    expect(find.byKey(const ValueKey('tell-kinaiya')), findsOneWidget);
    await tester.tap(find.byTooltip('Record voice'));
    await settle(tester);
    expect(audio.recordStarts, 1);
    await navigateTo(tester, AppView.agent);
    expect(find.text('Recording a voice message'), findsOneWidget);
    await tester.tap(find.text('Stop microphone'));
    await settle(tester);
    expect(audio.recordCancels, 1);
    expect(api.transcriptions, 0);
    expect(api.createdSessions, 0);
    expect(api.interrupts, 0);
    expect(state.converseHistory, isEmpty);
    await close(tester, state);
  });

  testWidgets('unconfirmed microphone stop stays visible and retryable', (
    tester,
  ) async {
    final api = LayoutApi();
    final audio = LayoutAudio();
    final state = wired(api);
    await tester.pumpWidget(KinaiyaApp(state: state, audio: audio));
    await settle(tester);
    await tester.tap(find.byTooltip('Start Live listen (armed mic)'));
    await settle(tester);
    await navigateTo(tester, AppView.agent);
    audio.rejectStop = true;
    await tester.tap(find.text('Stop microphone'));
    await settle(tester);
    expect(find.text('Microphone stop unconfirmed'), findsOneWidget);
    expect(find.text('Stop microphone'), findsOneWidget);
    audio.rejectStop = false;
    await tester.tap(find.text('Stop microphone'));
    await settle(tester);
    expect(find.text('Microphone stop unconfirmed'), findsNothing);
    expect(api.interrupts, 0);
    await close(tester, state);
  });

  testWidgets('pending microphone stop times out visibly and can be retried', (
    tester,
  ) async {
    final api = LayoutApi();
    final audio = LayoutAudio();
    final state = wired(api);
    await tester.pumpWidget(KinaiyaApp(state: state, audio: audio));
    await settle(tester);
    await tester.tap(find.byTooltip('Start Live listen (armed mic)'));
    await settle(tester);
    await navigateTo(tester, AppView.agent);
    final firstStop = Completer<void>();
    audio.pendingStop = firstStop;
    await tester.tap(find.text('Stop microphone'));
    await settle(tester);
    expect(find.text('Stopping microphone…'), findsOneWidget);
    expect(
      tester
          .widget<TextButton>(
            find.widgetWithText(TextButton, 'Stop microphone'),
          )
          .onPressed,
      isNull,
    );
    await tester.pump(const Duration(seconds: 3));
    await settle(tester);
    expect(find.text('Microphone stop unconfirmed'), findsOneWidget);
    final secondStop = Completer<void>();
    audio.pendingStop = secondStop;
    await tester.tap(find.text('Stop microphone'));
    await settle(tester);
    firstStop.complete();
    await settle(tester);
    expect(find.text('Stopping microphone…'), findsOneWidget);
    secondStop.complete();
    await settle(tester);
    expect(find.text('Stopping microphone…'), findsNothing);
    expect(find.text('Microphone stop unconfirmed'), findsNothing);
    expect(api.transcriptions, 0);
    expect(api.interrupts, 0);
    await close(tester, state);
  });

  testWidgets(
    'window microphone stop retry waits for playback acknowledgment',
    (tester) async {
      final api = LayoutApi();
      final audio = WindowLayoutAudio();
      final state = wired(api)..streamingWindows = true;
      await tester.pumpWidget(KinaiyaApp(state: state, audio: audio));
      await settle(tester);
      await tester.tap(find.byTooltip('Start Live listen (armed mic)'));
      await settle(tester);
      await navigateTo(tester, AppView.agent);
      final firstStop = Completer<void>();
      audio.pendingPlaybackStop = firstStop;
      await tester.tap(find.text('Stop microphone'));
      await settle(tester);
      expect(audio.stops, 1);
      expect(find.text('Stopping microphone…'), findsOneWidget);
      await tester.pump(const Duration(seconds: 3));
      await settle(tester);
      expect(find.text('Microphone stop unconfirmed'), findsOneWidget);

      final secondStop = Completer<void>();
      audio.pendingPlaybackStop = secondStop;
      await tester.tap(find.text('Stop microphone'));
      await settle(tester);
      expect(audio.stops, 2);
      expect(find.text('Stopping microphone…'), findsOneWidget);
      firstStop.complete();
      await settle(tester);
      expect(find.text('Stopping microphone…'), findsOneWidget);
      secondStop.complete();
      await settle(tester);
      expect(find.text('Stopping microphone…'), findsNothing);
      expect(find.text('Microphone stop unconfirmed'), findsNothing);
      expect(api.transcriptions, 0);
      expect(api.interrupts, 0);
      await close(tester, state);
    },
  );

  testWidgets(
    'desktop task inspection preserves draft/session and truthful evidence',
    (tester) async {
      tester.view.devicePixelRatio = 1;
      tester.view.physicalSize = const Size(1560, 1000);
      addTearDown(tester.view.resetDevicePixelRatio);
      addTearDown(tester.view.resetPhysicalSize);
      final api = LayoutApi()
        ..fixtures = [task('one', 'running'), task('two', 'succeeded')];
      final state = wired(api)
        ..setView(AppView.agent)
        ..agentDraft = 'Keep this draft';
      final original = state.agentSession;
      await tester.pumpWidget(KinaiyaApp(state: state, audio: LayoutAudio()));
      await settle(tester);
      expect(find.byType(NavigationRail), findsOneWidget);
      expect(find.text('Workspace'), findsOneWidget);
      expect(find.text('Task details'), findsOneWidget);
      final inspect = find.widgetWithText(TextButton, 'Inspect task').last;
      await tester.ensureVisible(inspect);
      await tester.tap(inspect);
      await settle(tester);
      expect(
        find.text('Finished without independent completion evidence.'),
        findsOneWidget,
      );
      expect(find.text('Succeeded with evidence below.'), findsNothing);
      expect(identical(state.agentSession, original), isTrue);
      expect(state.agentDraft, 'Keep this draft');
      expect(api.createdSessions, 0);
      expect(api.interrupts, 0);
      await close(tester, state);
    },
  );

  for (final sample in [
    (360.0, 800.0, 1.0),
    (360.0, 900.0, 2.0),
    (1440.0, 900.0, 1.0),
    (1440.0, 900.0, 2.0),
  ]) {
    testWidgets(
      'both views fit ${sample.$1}x${sample.$2} at text scale ${sample.$3}',
      (tester) async {
        tester.view.devicePixelRatio = 1;
        tester.view.physicalSize = Size(sample.$1, sample.$2);
        tester.platformDispatcher.textScaleFactorTestValue = sample.$3;
        addTearDown(tester.view.resetDevicePixelRatio);
        addTearDown(tester.view.resetPhysicalSize);
        addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
        final api = LayoutApi()..fixtures = [task('one', 'running')];
        final state = wired(api);
        await tester.pumpWidget(KinaiyaApp(state: state, audio: LayoutAudio()));
        await settle(tester);
        expect(find.byType(AnchoredMoonlitScene), findsOneWidget);
        expect(tester.takeException(), isNull);
        state.setView(AppView.agent);
        await settle(tester);
        expect(find.byKey(const ValueKey('agent-input')), findsOneWidget);
        expect(tester.takeException(), isNull);
        await close(tester, state);
      },
    );
  }

  for (final view in AppView.values) {
    testWidgets('keyboard resize preserves the focused $view input', (
      tester,
    ) async {
      tester.view.devicePixelRatio = 1;
      tester.view.physicalSize = const Size(360, 800);
      tester.platformDispatcher.textScaleFactorTestValue = 2;
      addTearDown(tester.view.resetDevicePixelRatio);
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetViewInsets);
      addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
      final state = wired(LayoutApi())..setView(view);
      await tester.pumpWidget(KinaiyaApp(state: state, audio: LayoutAudio()));
      await settle(tester);
      if (view == AppView.character) await openConverseChat(tester);
      final field = find.byKey(
        ValueKey(view == AppView.character ? 'converse-input' : 'agent-input'),
      );
      await tester.ensureVisible(field);
      await tester.showKeyboard(field);
      await tester.enterText(field, 'Synthetic draft');
      final original = tester
          .widget<EditableText>(
            find.descendant(of: field, matching: find.byType(EditableText)),
          )
          .focusNode;
      expect(original.hasFocus, isTrue);
      tester.view.viewInsets = const FakeViewPadding(bottom: 320);
      await settle(tester);
      final current = tester
          .widget<EditableText>(
            find.descendant(of: field, matching: find.byType(EditableText)),
          )
          .focusNode;
      expect(identical(original, current), isTrue);
      expect(current.hasFocus, isTrue);
      expect(tester.takeException(), isNull);
      expect(
        view == AppView.character ? state.converseDraft : state.agentDraft,
        'Synthetic draft',
      );
      await close(tester, state);
    });
  }

  test('foreground contrast remains readable in both themes', () {
    double contrast(Color a, Color b) {
      final x = a.computeLuminance(), y = b.computeLuminance();
      return x > y ? (x + .05) / (y + .05) : (y + .05) / (x + .05);
    }

    for (final brightness in Brightness.values) {
      final c = kinaiyaTheme(brightness: brightness).colorScheme;
      expect(contrast(c.primary, c.onPrimary), greaterThanOrEqualTo(4.5));
      expect(contrast(c.surface, c.onSurface), greaterThanOrEqualTo(4.5));
      expect(
        contrast(c.surfaceContainerHigh, c.onSurface),
        greaterThanOrEqualTo(4.5),
      );
      expect(
        contrast(c.tertiaryContainer, c.onSurface),
        greaterThanOrEqualTo(4.5),
      );
    }
    expect(kinaiyaTheme().colorScheme.primary, const Color(0xFF141091));
  });
}
