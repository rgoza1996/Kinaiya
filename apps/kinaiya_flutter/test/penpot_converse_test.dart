import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart' show RenderImage;
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/character/package.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/anchored_moonlit_scene.dart';
import 'package:kinaiya_flutter/ui/character_view.dart';
import 'package:kinaiya_flutter/ui/converse_design.dart';
import 'package:kinaiya_flutter/ui/design_tokens.dart';
import 'package:kinaiya_flutter/ui/moonlit_scene.dart';
import 'package:kinaiya_flutter/ui/settings_controls.dart';
import 'package:kinaiya_flutter/ui/visual_theme.dart';
import 'package:kinaiya_flutter/ui/widgets.dart';

import 'converse_stage_test.dart'
    show StageApi, StageAudio, seedHistory, visibleMascot;
import 'mascot_sprites_test.dart' show currentFrame, decoded, paintedFrame;

Rect _visiblePaintRegion(RenderBox box, Rect local) {
  var visible = MatrixUtils.transformRect(box.getTransformTo(null), local);
  RenderObject child = box;
  while (child.parent is RenderObject) {
    final parent = child.parent!;
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

/// Account for the actual fit/crop, not the unpainted Image widget bounds.
Rect _illustratedRegion(RenderImage image, Rect fractions) {
  final input = Size(
    image.image!.width.toDouble(),
    image.image!.height.toDouble(),
  );
  final fitted = applyBoxFit(image.fit ?? BoxFit.scaleDown, input, image.size);
  final alignment = image.alignment.resolve(image.textDirection);
  final source = alignment.inscribe(fitted.source, Offset.zero & input);
  final destination = alignment.inscribe(
    fitted.destination,
    Offset.zero & image.size,
  );
  final region = Rect.fromLTRB(
    input.width * fractions.left,
    input.height * fractions.top,
    input.width * fractions.right,
    input.height * fractions.bottom,
  ).intersect(source);
  if (region.isEmpty) return Rect.zero;
  final local = Rect.fromLTRB(
    destination.left +
        (region.left - source.left) / source.width * destination.width,
    destination.top +
        (region.top - source.top) / source.height * destination.height,
    destination.left +
        (region.right - source.left) / source.width * destination.width,
    destination.top +
        (region.bottom - source.top) / source.height * destination.height,
  );
  return _visiblePaintRegion(image, local);
}

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
  ConverseViewControls? controls,
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
            controls: controls,
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
    (const Size(1440, 1000), false, true, 1.0),
    (const Size(2688, 1416), false, true, 1.0),
    (const Size(390, 844), false, true, 1.0),
    (const Size(390, 844), true, true, 1.0),
    (const Size(390, 844), true, false, 1.0),
    (const Size(320, 568), true, true, 2.0),
  ]) {
    final (size, history, captions, scale) = sample;
    testWidgets('decoded idle contact intersects illustrated water at $sample', (
      tester,
    ) async {
      await designStage(
        tester,
        width: size.width,
        height: size.height,
        scale: scale,
        history: history ? seedHistory() : const [],
        captions: captions,
      );
      await decoded(tester);
      final raw = find.descendant(
        of: find.byKey(const ValueKey('moonlit-illustration')),
        matching: find.byType(RawImage),
      );
      for (var attempt = 0; attempt < 40; attempt++) {
        if (raw.evaluate().length == 1 &&
            tester.renderObject<RenderImage>(raw).image != null) {
          break;
        }
        await tester.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 10)),
        );
        await tester.pump(const Duration(milliseconds: 1));
      }
      expect(raw, findsOneWidget);
      final image = tester.renderObject<RenderImage>(raw);
      expect(
        image.image,
        isNotNull,
        reason: 'A fallback painter is not illustration evidence.',
      );
      final sceneFinder = find.byType(AnchoredMoonlitScene);
      expect(sceneFinder, findsOneWidget);
      final sceneSize = tester.getSize(sceneFinder);
      final phone = history
          ? sceneSize.width < sceneSize.height
          : size.width < size.height;
      final artwork = phone
          ? KinaiyaDesign.moonlitPhone
          : KinaiyaDesign.moonlitDesktop;
      expect(tester.widget<AnchoredMoonlitScene>(sceneFinder).artwork, artwork);
      expect(
        (tester
                    .widget<Image>(
                      find.byKey(const ValueKey('moonlit-illustration')),
                    )
                    .image
                as AssetImage)
            .assetName,
        artwork.asset,
      );
      expect(
        Size(image.image!.width.toDouble(), image.image!.height.toDouble()),
        artwork.sourceSize,
      );
      // Read-only source inspection places water just below these horizons.
      final water = _illustratedRegion(
        image,
        Rect.fromLTRB(0, phone ? .524 : .555, 1, 1),
      );
      final moon = _illustratedRegion(
        image,
        const Rect.fromLTRB(.4, .12, .6, .30),
      );
      expect(
        moon.isEmpty,
        isFalse,
        reason: 'The central upper moon is cropped out at $size.',
      );
      final sprite = tester.renderObject<RenderBox>(paintedFrame());
      final frame = currentFrame(tester);
      final fitted = applyBoxFit(BoxFit.contain, frame.src.size, sprite.size);
      final drawing = Alignment(
        frame.anchor.dx * 2 - 1,
        frame.anchor.dy * 2 - 1,
      ).inscribe(fitted.destination, Offset.zero & sprite.size);
      // Both curated seated poses put the contact ripple around 95% of the
      // frame height. Use a thin central band rather than the whole avatar box.
      final contact = _visiblePaintRegion(
        sprite,
        Rect.fromLTRB(
          drawing.left + drawing.width * .15,
          drawing.top + drawing.height * .93,
          drawing.right - drawing.width * .15,
          drawing.top + drawing.height * .98,
        ),
      );
      expect(water.isEmpty, isFalse);
      expect(contact.isEmpty, isFalse);
      expect(
        contact.overlaps(water),
        isTrue,
        reason:
            'Decoded seated contact $contact floats above painted water $water at $size.',
      );
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('Call and Tell copy has opaque readable surfaces over water', (
    tester,
  ) async {
    await designStage(tester);
    for (final entry in [
      ('call-kinaiya-copy', 'Call Kinaiya', 'Have a natural conversation'),
      ('tell-kinaiya-copy', 'Tell Kinaiya', 'Send a voice message'),
    ]) {
      final copy = find.byKey(ValueKey(entry.$1));
      expect(copy, findsOneWidget);
      final container = tester.widget<Container>(copy);
      final fill = (container.decoration! as BoxDecoration).color!;
      expect(
        fill.a,
        1,
        reason: 'Water must not show through the copy surface.',
      );
      final labels = find.descendant(of: copy, matching: find.byType(Text));
      expect(labels, findsNWidgets(2));
      expect(tester.widgetList<Text>(labels).map((text) => text.data), [
        entry.$2,
        entry.$3,
      ]);
      for (final text in tester.widgetList<Text>(labels)) {
        final ink = text.style!.color!;
        expect(ink.a, 1);
        final a = fill.computeLuminance(), b = ink.computeLuminance();
        expect(
          (a > b ? a + .05 : b + .05) / (a > b ? b + .05 : a + .05),
          greaterThanOrEqualTo(4.5),
          reason: '${text.data} must remain readable over the patterned water.',
        );
      }
    }
    expect(
      find.byWidgetPredicate((widget) => widget is ButtonStyleButton),
      findsNWidgets(2),
    );
    expect(find.byKey(const ValueKey('converse-input')), findsNothing);
    expect(find.byKey(const ValueKey('voice-options')), findsNothing);
    expect(find.text('Memory'), findsNothing);
    expect(find.text('Send'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('scene caption has an opaque readable surface over moon pixels', (
    tester,
  ) async {
    await designStage(
      tester,
      history: [ChatMsg(role: 'kinaiya', text: 'Caption over the moon')],
    );
    final text = tester.widget<Text>(
      find.byKey(const ValueKey('current-caption-text')),
    );
    final surfaces = tester.widgetList<Container>(
      find.byKey(const ValueKey('converse-scene-copy')),
    );
    expect(surfaces, isNotEmpty);
    for (final surface in surfaces) {
      final fill = (surface.decoration! as BoxDecoration).color!;
      expect(fill.a, 1);
      final a = fill.computeLuminance(),
          b = text.style!.color!.computeLuminance();
      expect(
        (a > b ? a + .05 : b + .05) / (a > b ? b + .05 : a + .05),
        greaterThanOrEqualTo(4.5),
      );
    }
    expect(tester.takeException(), isNull);
  });

  for (final sample in [
    (390.0, 844.0, 1.0, 0.0),
    (320.0, 568.0, 2.0, 0.0),
    (390.0, 844.0, 2.0, 300.0),
    (800.0, 360.0, 2.0, 0.0),
  ]) {
    testWidgets('design stage/history accessible at $sample', (tester) async {
      final controls = ConverseViewControls();
      await designStage(
        tester,
        width: sample.$1,
        height: sample.$2,
        scale: sample.$3,
        keyboard: sample.$4,
        history: seedHistory(),
        controls: controls,
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
      final chat = controls.openTextConversation!();
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      await tester.ensureVisible(find.byKey(const ValueKey('voice-options')));
      await tester.pump(const Duration(milliseconds: 200));
      expect(
        find.byKey(const ValueKey('voice-options')).hitTestable(),
        findsOneWidget,
      );
      await tester.ensureVisible(find.byTooltip('Close Converse chat'));
      await tester.tap(find.byTooltip('Close Converse chat'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      await chat;
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
      tester
          .widget<AnchoredMoonlitScene>(find.byType(AnchoredMoonlitScene))
          .artwork,
      KinaiyaDesign.moonlitDesktop,
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
    final controls = ConverseViewControls();
    final state = await designStage(tester, audio: audio, controls: controls);
    expect(find.byKey(const ValueKey('converse-input')), findsNothing);
    expect(find.byKey(const ValueKey('voice-options')), findsNothing);
    expect(find.text('Memory'), findsNothing);
    expect(find.text('Send'), findsNothing);
    expect(
      find.byWidgetPredicate((widget) => widget is ButtonStyleButton),
      findsNWidgets(2),
      reason: 'The design bottom bar contains only Call and Tell.',
    );
    final initialVadCalls = audio.vadCalls;
    final draftChat = controls.openTextConversation!();
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));
    expect(audio.vadCalls, initialVadCalls);
    await tester.ensureVisible(find.byKey(const ValueKey('converse-input')));
    await tester.pump(const Duration(milliseconds: 200));
    await tester.enterText(
      find.byKey(const ValueKey('converse-input')),
      'Synthetic draft',
    );
    await tester.pump();
    await tester.tap(find.byTooltip('Close Converse chat'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));
    await draftChat;
    await tester.ensureVisible(find.byKey(const ValueKey('call-kinaiya')));
    await tester.pump(const Duration(milliseconds: 200));
    await tester.tap(find.byKey(const ValueKey('call-kinaiya')));
    await tester.pump(const Duration(milliseconds: 200));
    expect(audio.arms, 1);
    expect(find.text('End conversation'), findsOneWidget);
    final configured = audio.vadCalls;
    final silence = audio.lastVad;
    final activeChat = controls.openTextConversation!();
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));
    expect(
      tester
          .widget<TextField>(find.byKey(const ValueKey('converse-input')))
          .controller!
          .text,
      'Synthetic draft',
    );
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
    await tester.tap(find.byTooltip('Close Converse chat'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));
    await activeChat;
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
