import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/audio/speech_audio.dart';
import 'package:kinaiya_flutter/character/package.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/character_view.dart';
import 'package:kinaiya_flutter/ui/visual_theme.dart';
import 'package:kinaiya_flutter/ui/widgets.dart';

import 'client_test.dart' show mockTransport;

// Synthetic audio adapter with controllable capabilities and no service or
// model calls. Mirrors the existing test fakes without importing them.
class StageAudio implements SpeechAudio {
  StageAudio({this.mic = true, this.playback = true});
  bool mic, playback;
  int arms = 0, armStops = 0, clips = 0, stops = 0;
  int recordStarts = 0, recStops = 0, cancels = 0;
  int? lastVad;
  int epoch = 0;

  @override
  Future<bool> get availableMic async => mic;
  @override
  Future<bool> get availablePlayback async => playback;
  @override
  Future<String> startArmed() async {
    arms++;
    return 'armed';
  }

  @override
  Future<void> stopArmed() async {
    armStops++;
  }

  @override
  Future<String> beginClip() async {
    clips++;
    return 'clip-recording';
  }

  @override
  Future<int> get captureEpoch async => ++epoch;
  @override
  Future<RecordedAudio?> finishClip() async =>
      RecordedAudio('audio/webm', [1, 2, 3]);
  @override
  Future<void> configureVad({required int endSilenceMs}) async {
    lastVad = endSilenceMs;
  }

  @override
  Future<List<Map<String, Object?>>> pollBridgeEvents() async => const [];
  @override
  Future<void> stopPlayback() async {
    stops++;
  }

  @override
  Future<bool> queuePcm(String value, int rate) async => true;
  @override
  Future<void> awaitPlayback() async {}
  @override
  Future<bool> get isPlaying async => false;
  @override
  Future<RecordedAudio?> chooseRecording() async => null;
  @override
  Future<String> startRecording() async {
    recordStarts++;
    return 'recording';
  }

  @override
  Future<RecordedAudio?> stopRecording() async {
    recStops++;
    return null;
  }

  @override
  Future<void> cancelRecording() async {
    cancels++;
  }

  @override
  Future<void> dispose() async {}
}

class StageApi extends KinaiyaClient {
  StageApi({http.Client Function()? memoryFactory})
    : super(
        sessionBase: 'http://fixture',
        agentBase: 'http://fixture',
        converseBase: 'http://fixture',
        ownerToken: 'synthetic',
        httpClient: mockTransport(commandsMissing: false),
        memoryClientFactory:
            memoryFactory ??
            (() => MockClient((_) async => http.Response('unexpected', 500))),
      );
  @override
  Future<Map<String, Object?>> converseHealth() async => {'enabled': true};
}

List<ChatMsg> seedHistory() => [
  ChatMsg(role: 'user', text: 'First synthetic question.'),
  ChatMsg(
    role: 'kinaiya',
    text: 'First synthetic answer with detail.',
    note: 'played',
  ),
  ChatMsg(role: 'user', text: 'Second synthetic question.'),
  ChatMsg(role: 'kinaiya', text: 'Latest synthetic answer.'),
];

Future<AppState> pumpStage(
  WidgetTester tester, {
  required double width,
  required double height,
  double textScale = 1,
  bool showCharacter = true,
  bool showCaptions = true,
  ThemeData? theme,
  List<ChatMsg> history = const [],
  StageAudio? audio,
  http.Client Function()? memoryFactory,
}) async {
  tester.view.devicePixelRatio = 1;
  tester.view.physicalSize = Size(width, height);
  tester.platformDispatcher.textScaleFactorTestValue = textScale;
  addTearDown(tester.view.resetDevicePixelRatio);
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
  final api = StageApi(memoryFactory: memoryFactory);
  final state = AppState()
    ..connect(ConnectionProfile.defaults(), injected: api);
  for (final m in history) {
    state.converseHistory.add(m);
  }
  addTearDown(state.dispose);
  // Decode the real bundled pack outside FakeAsync. The widget still paints
  // the production sprite assets; no replacement fixture hides decode errors.
  final character = await tester.runAsync(
    () => loadDefaultCharacterSelection(rootBundle),
  );
  expect(character, isNotNull);
  await tester.pumpWidget(
    MaterialApp(
      theme: theme,
      home: Scaffold(
        body: ListenableBuilder(
          listenable: state,
          builder: (_, _) => CharacterView(
            state: state,
            audio: audio ?? StageAudio(),
            character: character,
            showCharacter: showCharacter,
            showCaptions: showCaptions,
          ),
        ),
      ),
    ),
  );
  await tester.pump(const Duration(milliseconds: 200));
  return state;
}

Rect visibleMascot(WidgetTester tester) {
  var child = tester.renderObject<RenderBox>(find.byType(MascotWidget));
  var visible = tester.getRect(find.byType(MascotWidget));
  while (child.parent is RenderBox) {
    final parent = child.parent! as RenderBox;
    final clip = parent.describeApproximatePaintClip(child);
    if (clip != null) {
      visible = visible.intersect(
        MatrixUtils.transformRect(parent.getTransformTo(null), clip),
      );
    }
    child = parent;
  }
  return visible;
}

void main() {
  for (final sample in [
    (320.0, 800.0, 1.0, 'narrow 320px'),
    (360.0, 800.0, 1.0, 'phone'),
    (360.0, 800.0, 2.0, 'phone 2x text'),
    (320.0, 568.0, 2.0, 'short phone 2x text'),
    (800.0, 360.0, 2.0, 'landscape 2x text'),
    (768.0, 1024.0, 1.0, 'tablet'),
    (1440.0, 900.0, 1.0, 'desktop'),
    (1440.0, 900.0, 2.0, 'desktop 2x text'),
  ]) {
    testWidgets('stage stays prominent with history at ${sample.$4}', (
      tester,
    ) async {
      await pumpStage(
        tester,
        width: sample.$1,
        height: sample.$2,
        textScale: sample.$3,
        history: seedHistory(),
      );
      // The animated stage keeps a visually substantial size instead of
      // collapsing to a 72px avatar row.
      final mascotSize = tester.getSize(find.byType(MascotWidget));
      expect(mascotSize.height, greaterThanOrEqualTo(90));
      expect(mascotSize.width, greaterThanOrEqualTo(90));
      expect(visibleMascot(tester).height, greaterThanOrEqualTo(90));
      // Readable current caption plus an explicit full-history affordance.
      expect(
        find.byKey(const ValueKey('current-caption-text')),
        findsOneWidget,
      );
      expect(find.text('Kinaiya: Latest synthetic answer.'), findsWidgets);
      expect(
        find.byKey(const ValueKey('conversation-history')),
        findsOneWidget,
      );
      expect(find.text('Conversation history'), findsOneWidget);
      // Inline transcript keeps every message and delivery note.
      expect(find.text('First synthetic question.'), findsWidgets);
      // The list builds visible rows lazily. Scroll to verify the delivery
      // receipt instead of assuming every row is initially on screen at 2x.
      await tester.scrollUntilVisible(
        find.text('played'),
        60,
        scrollable: find
            .descendant(
              of: find.byType(ListView).first,
              matching: find.byType(Scrollable),
            )
            .first,
      );
      expect(find.text('played'), findsWidgets);
      // Call/Tell stay prominent with the mic available.
      expect(find.byKey(const ValueKey('call-kinaiya')), findsOneWidget);
      expect(find.byKey(const ValueKey('tell-kinaiya')), findsOneWidget);
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('history dialog carries the complete transcript', (tester) async {
    await pumpStage(tester, width: 360, height: 800, history: seedHistory());
    await tester.tap(find.byKey(const ValueKey('conversation-history')));
    await tester.pump();
    expect(find.text('Conversation history (4)'), findsOneWidget);
    for (final text in [
      'First synthetic question.',
      'First synthetic answer with detail.',
      'Second synthetic question.',
      'Latest synthetic answer.',
      'played',
    ]) {
      expect(find.text(text), findsWidgets);
    }
    await tester.tap(find.text('CLOSE'));
    await tester.pump();
    expect(find.text('Conversation history (4)'), findsNothing);
    // Closing the dialog leaves capture ownership untouched.
    expect(find.byKey(const ValueKey('call-kinaiya')), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('short landscape height does not overflow', (tester) async {
    await pumpStage(tester, width: 800, height: 360, history: seedHistory());
    expect(find.byType(MascotWidget), findsOneWidget);
    expect(find.byKey(const ValueKey('conversation-history')), findsOneWidget);
    expect(find.byKey(const ValueKey('converse-input')), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('captions-off stage keeps history access without inline text', (
    tester,
  ) async {
    await pumpStage(
      tester,
      width: 360,
      height: 800,
      showCaptions: false,
      history: seedHistory(),
    );
    expect(find.text('First synthetic question.'), findsNothing);
    expect(find.byKey(const ValueKey('current-caption-text')), findsNothing);
    expect(find.byKey(const ValueKey('conversation-history')), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey('conversation-history')));
    await tester.pump();
    expect(find.text('Latest synthetic answer.'), findsOneWidget);
    expect(find.text('played'), findsOneWidget);
    await tester.tap(find.text('CLOSE'));
    await tester.pump();
    expect(tester.takeException(), isNull);
  });

  testWidgets('non-character fallback retains transcript and history access', (
    tester,
  ) async {
    await pumpStage(
      tester,
      width: 360,
      height: 800,
      showCharacter: false,
      history: seedHistory(),
    );
    expect(find.byType(MascotWidget), findsNothing);
    expect(find.text('Latest synthetic answer.'), findsWidgets);
    expect(find.byKey(const ValueKey('conversation-history')), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('composer uses the custom dark palette surface pair', (
    tester,
  ) async {
    final theme = kinaiyaTheme(
      brightness: Brightness.dark,
      primary: const Color(0xFF9DFF9D),
      background: const Color(0xFF101418),
    );
    double contrast(Color a, Color b) {
      final x = a.computeLuminance(), y = b.computeLuminance();
      return x > y ? (x + .05) / (y + .05) : (y + .05) / (x + .05);
    }

    expect(
      contrast(theme.colorScheme.surface, theme.colorScheme.onSurface),
      greaterThanOrEqualTo(4.5),
    );
    await pumpStage(
      tester,
      width: 360,
      height: 800,
      theme: theme,
      history: seedHistory(),
    );
    final composer = find.ancestor(
      of: find.byKey(const ValueKey('voice-options')),
      matching: find.byType(Container),
    );
    final decoration =
        tester.widget<Container>(composer.first).decoration! as BoxDecoration;
    expect(decoration.color, theme.colorScheme.surface);
    expect(
      decoration.border,
      Border.all(color: theme.colorScheme.outlineVariant),
    );
    // Keyboard traversal reaches the input and history affordance.
    expect(find.byKey(const ValueKey('converse-input')), findsOneWidget);
    expect(find.byKey(const ValueKey('conversation-history')), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('voice options preserve capture, draft, and silence handling', (
    tester,
  ) async {
    final audio = StageAudio();
    final state = await pumpStage(
      tester,
      width: 360,
      height: 800,
      audio: audio,
    );
    await tester.enterText(
      find.byKey(const ValueKey('converse-input')),
      'Keep this draft',
    );
    await tester.tap(find.byTooltip('Start Live listen (armed mic)'));
    await tester.pump(const Duration(milliseconds: 100));
    expect(audio.arms, 1);
    expect(find.text('End conversation'), findsOneWidget);

    // Open options: silence controls appear without touching capture.
    await tester.ensureVisible(find.byKey(const ValueKey('voice-options')));
    await tester.pump(const Duration(milliseconds: 200));
    await tester.tap(find.byKey(const ValueKey('voice-options')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));
    expect(find.text('Silence'), findsOneWidget);
    expect(find.text('Listening (live)'), findsWidgets);
    expect(audio.armStops, 0);
    expect(audio.cancels, 0);
    expect(audio.clips, 1);

    // Changing silence uses the real handler and persists across reopen.
    await tester.tap(find.byType(DropdownButton<int>));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));
    await tester.tap(find.text('2.5s'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));
    expect(audio.lastVad, 2500);
    expect(
      tester
          .widget<DropdownButton<int>>(find.byType(DropdownButton<int>))
          .value,
      2500,
    );
    expect(audio.armStops, 0);
    expect(audio.clips, 1);
    await tester.tap(find.byTooltip('Close voice options'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));
    expect(find.text('Silence'), findsNothing);
    // Capture, draft, and session survive the close.
    expect(find.text('End conversation'), findsOneWidget);
    expect(audio.armStops, 0);
    final field = tester.widget<TextField>(
      find.byKey(const ValueKey('converse-input')),
    );
    expect(field.controller!.text, 'Keep this draft');
    expect(state.converseDraft, 'Keep this draft');
    await tester.tap(find.byKey(const ValueKey('voice-options')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));
    expect(
      tester
          .widget<DropdownButton<int>>(find.byType(DropdownButton<int>))
          .value,
      2500,
    );
    await tester.tap(find.byTooltip('Close voice options'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));

    // Disarming still stops capture exactly once.
    await tester.tap(find.text('End conversation'));
    await tester.pump(const Duration(milliseconds: 200));
    expect(audio.armStops, 1);
    expect(tester.takeException(), isNull);
  });

  testWidgets('stop stays available when readiness changes mid-recording', (
    tester,
  ) async {
    final audio = StageAudio();
    final state = await pumpStage(
      tester,
      width: 360,
      height: 800,
      audio: audio,
    );
    await tester.tap(find.byTooltip('Record voice'));
    await tester.pump();
    expect(audio.recordStarts, 1);
    expect(find.byTooltip('Stop recording'), findsOneWidget);
    // Readiness drops mid-capture; the stop itself must still land.
    state.converseEnabled = false;
    state.touch();
    await tester.pump();
    await tester.ensureVisible(find.byTooltip('Stop recording'));
    await tester.pump();
    await tester.tap(find.byTooltip('Stop recording'));
    await tester.pump();
    expect(audio.recStops, 1);
    expect(find.byTooltip('Stop recording'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    'armed microphone can be stopped after backend readiness is lost',
    (tester) async {
      final audio = StageAudio();
      final state = await pumpStage(
        tester,
        width: 360,
        height: 800,
        audio: audio,
      );
      await tester.tap(find.byKey(const ValueKey('call-kinaiya')));
      await tester.pump();
      expect(audio.arms, 1);
      state.converseEnabled = false;
      state.touch();
      await tester.pump();
      final stop = find.byKey(const ValueKey('call-kinaiya'));
      expect(tester.widget<FilledButton>(stop).onPressed, isNotNull);
      await tester.ensureVisible(stop);
      await tester.pump();
      await tester.tap(stop);
      await tester.pump(const Duration(milliseconds: 200));
      expect(audio.armStops, 1);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('microphone-less stub disables capture but keeps text', (
    tester,
  ) async {
    await pumpStage(
      tester,
      width: 360,
      height: 800,
      audio: StageAudio(mic: false, playback: false),
      history: seedHistory(),
    );
    expect(
      tester
          .widget<FilledButton>(find.byKey(const ValueKey('call-kinaiya')))
          .onPressed,
      isNull,
    );
    expect(
      tester
          .widget<OutlinedButton>(find.byKey(const ValueKey('tell-kinaiya')))
          .onPressed,
      isNull,
    );
    expect(find.byTooltip('Microphone unavailable here'), findsNWidgets(2));
    // Working text and full-history paths stay usable.
    expect(find.byKey(const ValueKey('converse-input')), findsOneWidget);
    await tester.enterText(
      find.byKey(const ValueKey('converse-input')),
      'Text still works',
    );
    await tester.pump();
    expect(
      tester.widget<FilledButton>(find.widgetWithText(FilledButton, 'Send')),
      isNotNull,
    );
    expect(
      tester
          .widget<FilledButton>(find.widgetWithText(FilledButton, 'Send'))
          .onPressed,
      isNotNull,
    );
    expect(find.byKey(const ValueKey('conversation-history')), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('memory entry stays available and preserves draft and capture', (
    tester,
  ) async {
    final memoryLog = <String>[];
    http.Client memoryFixture() => MockClient((http.Request req) async {
      memoryLog.add(req.url.path);
      return http.Response(jsonEncode({'unexpected': true}), 500);
    });
    final audio = StageAudio();
    final state = await pumpStage(
      tester,
      width: 320,
      height: 568,
      textScale: 2,
      memoryFactory: memoryFixture,
    );
    state.converseSession = SessionInfo(
      sessionId: 's-stage',
      threadId: 't-stage',
      channel: 'converse',
      epoch: 1,
    );
    await tester.enterText(
      find.byKey(const ValueKey('converse-input')),
      'Keep this draft',
    );
    await tester.tap(find.byTooltip('Start Live listen (armed mic)'));
    await tester.pump(const Duration(milliseconds: 100));
    expect(audio.arms, 1);
    expect(find.byKey(const ValueKey('voice-options')), findsOneWidget);
    expect(find.byKey(const ValueKey('converse-memory')), findsOneWidget);
    await tester.ensureVisible(find.byKey(const ValueKey('converse-memory')));
    await tester.pump(const Duration(milliseconds: 200));
    await tester.tap(find.byKey(const ValueKey('converse-memory')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 200));
    expect(find.text('Converse memory'), findsOneWidget);
    // Opening the dialog sends no session/configure/capture requests and
    // leaves the stage, draft, and armed capture untouched.
    expect(
      memoryLog.where((p) => p.endsWith('/configure') || p.endsWith('/use')),
      isEmpty,
    );
    expect(find.text('End conversation'), findsOneWidget);
    expect(audio.armStops, 0);
    expect(audio.cancels, 0);
    await tester.tap(find.text('CLOSE'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 200));
    expect(find.text('Converse memory'), findsNothing);
    expect(find.text('End conversation'), findsOneWidget);
    final field = tester.widget<TextField>(
      find.byKey(const ValueKey('converse-input')),
    );
    expect(field.controller!.text, 'Keep this draft');
    expect(state.converseDraft, 'Keep this draft');
    expect(audio.armStops, 0);
    await tester.tap(find.text('End conversation'));
    await tester.pump(const Duration(milliseconds: 200));
    expect(audio.armStops, 1);
    expect(tester.takeException(), isNull);
  });

  testWidgets('memory entry is present without history or character stage', (
    tester,
  ) async {
    http.Client memoryFixture() => MockClient(
      (_) async => http.Response(jsonEncode({'unexpected': true}), 500),
    );
    await pumpStage(
      tester,
      width: 360,
      height: 800,
      showCharacter: false,
      memoryFactory: memoryFixture,
    );
    expect(find.byType(MascotWidget), findsNothing);
    expect(find.byKey(const ValueKey('converse-memory')), findsOneWidget);
    expect(find.byKey(const ValueKey('voice-options')), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey('converse-memory')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 200));
    // No session exists in this stage, so the dialog explains why instead
    // of creating one.
    expect(find.textContaining('no current Converse session'), findsOneWidget);
    await tester.tap(find.text('CLOSE'));
    await tester.pump();
    expect(find.text('Converse memory'), findsNothing);
    expect(tester.takeException(), isNull);
  });
}
