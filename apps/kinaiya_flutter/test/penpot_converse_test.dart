import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/character/package.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/character_view.dart';
import 'package:kinaiya_flutter/ui/converse_design.dart';
import 'package:kinaiya_flutter/ui/design_tokens.dart';
import 'package:kinaiya_flutter/ui/moonlit_scene.dart';
import 'package:kinaiya_flutter/ui/visual_theme.dart';
import 'package:kinaiya_flutter/ui/widgets.dart';

import 'converse_stage_test.dart'
    show StageApi, StageAudio, seedHistory, visibleMascot;

class DesignAudio extends StageAudio {
  int vadCalls = 0;
  @override
  Future<void> configureVad({required int endSilenceMs}) async {
    vadCalls++;
    await super.configureVad(endSilenceMs: endSilenceMs);
  }
}

class MissingSceneBundle extends CachingAssetBundle {
  int attempts = 0;
  @override
  Future<ByteData> load(String key) {
    if (key == KinaiyaDesign.moonlitBackground) {
      attempts++;
      return Future.error(StateError('Synthetic missing background'));
    }
    return rootBundle.load(key);
  }
}

Future<AppState> designStage(
  WidgetTester tester, {
  double width = 390,
  double height = 844,
  double scale = 1,
  double keyboard = 0,
  bool captions = true,
  bool characterVisible = true,
  List<ChatMsg> history = const [],
  StageAudio? audio,
}) async {
  tester.view.devicePixelRatio = 1;
  tester.view.physicalSize = Size(width, height);
  tester.view.viewInsets = FakeViewPadding(bottom: keyboard);
  tester.platformDispatcher.textScaleFactorTestValue = scale;
  addTearDown(tester.view.resetDevicePixelRatio);
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetViewInsets);
  addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
  final state = AppState()
    ..connect(ConnectionProfile.defaults(), injected: StageApi());
  state.converseHistory.addAll(history);
  addTearDown(state.dispose);
  final selection = await tester.runAsync(
    () => loadDefaultCharacterSelection(rootBundle),
  );
  expect(selection, isNotNull);
  await tester.pumpWidget(
    MaterialApp(
      theme: kinaiyaTheme(),
      home: Scaffold(
        body: ListenableBuilder(
          listenable: state,
          builder: (_, _) => CharacterView(
            state: state,
            character: selection,
            audio: audio ?? StageAudio(),
            useDesignScene: true,
            showCaptions: captions,
            showCharacter: characterVisible,
          ),
        ),
      ),
    ),
  );
  await tester.pump(const Duration(milliseconds: 200));
  return state;
}

void main() {
  for (final sample in [
    (390.0, 844.0, 1.0, 0.0),
    (320.0, 568.0, 2.0, 0.0),
    (390.0, 844.0, 2.0, 300.0),
    (800.0, 360.0, 2.0, 0.0),
  ]) {
    testWidgets('design stage/history accessible at $sample', (tester) async {
      await designStage(
        tester,
        width: sample.$1,
        height: sample.$2,
        scale: sample.$3,
        keyboard: sample.$4,
        history: seedHistory(),
      );
      expect(find.byType(MascotWidget), findsOneWidget);
      expect(visibleMascot(tester).height, greaterThanOrEqualTo(100));
      expect(
        find.byKey(const ValueKey('current-caption-text')),
        findsOneWidget,
      );
      expect(
        find.byKey(const ValueKey('conversation-history')),
        findsOneWidget,
      );
      await tester.ensureVisible(find.byKey(const ValueKey('voice-options')));
      await tester.pump(const Duration(milliseconds: 200));
      expect(
        find.byKey(const ValueKey('voice-options')).hitTestable(),
        findsOneWidget,
      );
      await tester.ensureVisible(find.byKey(const ValueKey('call-kinaiya')));
      await tester.pump(const Duration(milliseconds: 200));
      expect(
        find.byKey(const ValueKey('call-kinaiya')).hitTestable(),
        findsOneWidget,
      );
      expect(
        tester.getSize(find.byKey(const ValueKey('call-kinaiya'))).width,
        80,
      );
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('desktop hero is substantial and uses real semantic animation', (
    tester,
  ) async {
    await designStage(tester, width: 1440, height: 1000);
    expect(tester.getSize(find.byType(MascotWidget)).height, greaterThan(320));
    expect(visibleMascot(tester).height, greaterThan(320));
    expect(find.byType(ConverseVoiceActions), findsOneWidget);
    expect(
      tester.widget<MoonlitScene>(find.byType(MoonlitScene)).useIllustration,
      isTrue,
    );
    // Missing image uses the deterministic painter, without losing controls.
    expect(find.byKey(const ValueKey('moonlit-illustration')), findsOneWidget);
    expect(find.text('Ready to talk'), findsOneWidget);
    expect(find.text('10:24 AM'), findsNothing);
    expect(find.text('Agent available'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('design gestures preserve capture, draft and options ownership', (
    tester,
  ) async {
    final audio = DesignAudio();
    final state = await designStage(tester, audio: audio);
    await tester.ensureVisible(find.byKey(const ValueKey('converse-input')));
    await tester.pump(const Duration(milliseconds: 200));
    await tester.enterText(
      find.byKey(const ValueKey('converse-input')),
      'Synthetic draft',
    );
    await tester.pump();
    await tester.ensureVisible(find.byKey(const ValueKey('call-kinaiya')));
    await tester.pump(const Duration(milliseconds: 200));
    await tester.tap(find.byKey(const ValueKey('call-kinaiya')));
    await tester.pump(const Duration(milliseconds: 200));
    expect(audio.arms, 1);
    expect(find.text('End conversation'), findsOneWidget);
    final configured = audio.vadCalls;
    final silence = audio.lastVad;
    await tester.ensureVisible(find.byKey(const ValueKey('voice-options')));
    await tester.pump(const Duration(milliseconds: 200));
    await tester.tap(find.byKey(const ValueKey('voice-options')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));
    expect(audio.vadCalls, configured);
    expect(audio.lastVad, silence);
    expect(audio.armStops, 0);
    expect(audio.cancels, 0);
    await tester.tap(find.byTooltip('Close voice options'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));
    expect(state.converseDraft, 'Synthetic draft');
    expect(audio.arms, 1);
    expect(audio.armStops, 0);
    // Backend loss must not disable the already-owned microphone stop.
    state.converseEnabled = false;
    state.touch();
    await tester.pump();
    await tester.ensureVisible(find.byKey(const ValueKey('call-kinaiya')));
    await tester.pump(const Duration(milliseconds: 200));
    await tester.tap(find.byKey(const ValueKey('call-kinaiya')));
    await tester.pump(const Duration(milliseconds: 200));
    expect(audio.armStops, 1);
    expect(tester.takeException(), isNull);
  });

  testWidgets('captions-off still opens all history and delivery notes', (
    tester,
  ) async {
    await designStage(tester, captions: false, history: seedHistory());
    expect(find.byKey(const ValueKey('current-caption-text')), findsNothing);
    await tester.ensureVisible(
      find.byKey(const ValueKey('conversation-history')),
    );
    await tester.pump(const Duration(milliseconds: 200));
    await tester.tap(find.byKey(const ValueKey('conversation-history')));
    await tester.pump();
    expect(find.text('Latest synthetic answer.'), findsOneWidget);
    expect(find.text('played'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('no-character setting does not display illustration or sprite', (
    tester,
  ) async {
    await designStage(tester, characterVisible: false, history: seedHistory());
    expect(find.byType(MascotWidget), findsNothing);
    expect(find.byKey(const ValueKey('moonlit-illustration')), findsNothing);
    expect(find.text('Latest synthetic answer.'), findsWidgets);
    expect(tester.takeException(), isNull);
  });

  testWidgets('illustration is opt-in and custom palette painter remains', (
    tester,
  ) async {
    const primary = Color(0xFF246040);
    await tester.pumpWidget(
      const MaterialApp(
        home: MoonlitScene(
          primary: primary,
          background: Color(0xFFFFEEDD),
          child: SizedBox.expand(),
        ),
      ),
    );
    expect(find.byKey(const ValueKey('moonlit-illustration')), findsNothing);
    expect(find.byType(CustomPaint), findsWidgets);
    expect(tester.takeException(), isNull);
  });

  testWidgets('missing decorative image keeps painter and child interactive', (
    tester,
  ) async {
    final bundle = MissingSceneBundle();
    var taps = 0;
    await tester.pumpWidget(
      MaterialApp(
        home: DefaultAssetBundle(
          bundle: bundle,
          child: MoonlitScene(
            useIllustration: true,
            child: Center(
              child: TextButton(
                onPressed: () => taps++,
                child: const Text('Keep talking'),
              ),
            ),
          ),
        ),
      ),
    );
    await tester.pump();
    expect(bundle.attempts, 1);
    expect(find.byType(CustomPaint), findsWidgets);
    await tester.tap(find.text('Keep talking'));
    expect(taps, 1);
    expect(tester.takeException(), isNull);
  });
}
