import 'dart:io';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/ui/converse_mouth.dart';
import 'package:kinaiya_flutter/ui/moonlit_home.dart';

const _boundary = ValueKey('portrait-render-proof');
const _atlas = AssetImage('assets/design/kinaiya-expression-reference.png');

Widget _scene({
  bool speaking = true,
  bool thinking = false,
  bool reduced = false,
  double openness = 0,
  String? viseme,
  Size size = const Size(768, 512),
}) => MaterialApp(
  home: MediaQuery(
    data: MediaQueryData(disableAnimations: reduced),
    child: Center(
      child: RepaintBoundary(
        key: _boundary,
        child: SizedBox.fromSize(
          size: size,
          child: ConversePortrait(
            speaking: speaking,
            thinking: thinking,
            mouthOpenness: openness,
            viseme: viseme,
          ),
        ),
      ),
    ),
  ),
);

Finder _mouth() => find.byKey(const ValueKey('converse-speech-mouth'));
ConverseMouthPose _pose(WidgetTester tester) =>
    (tester.widget<CustomPaint>(_mouth()).painter! as ConverseMouthPainter)
        .pose;

Future<void> _decode(WidgetTester tester) async {
  await tester.runAsync(
    () => precacheImage(_atlas, tester.element(find.byType(ConversePortrait))),
  );
  await tester.pumpAndSettle();
}

Future<ui.Image> _capture(WidgetTester tester) => tester
    .runAsync(() {
      final boundary = tester.renderObject<RenderRepaintBoundary>(
        find.byKey(_boundary),
      );
      return boundary.toImage(pixelRatio: 1);
    })
    .then((image) => image!);

Future<Uint8List> _pixels(WidgetTester tester, ui.Image image) async =>
    (await tester.runAsync(
      () => image.toByteData(format: ui.ImageByteFormat.rawRgba),
    ))!.buffer.asUint8List();

void main() {
  test('bounded audio levels choose closed, half open and open mouths', () {
    for (final level in [double.nan, double.infinity, -1.0, 0.0, .079]) {
      expect(converseMouthPose(level, null), ConverseMouthPose.closed);
    }
    expect(converseMouthPose(.08, null), ConverseMouthPose.halfOpen);
    expect(converseMouthPose(.59, null), ConverseMouthPose.halfOpen);
    expect(converseMouthPose(.6, null), ConverseMouthPose.open);
    expect(converseMouthPose(10, null), ConverseMouthPose.open);
  });

  test(
    'timed visemes override the fallback shape while silence stays closed',
    () {
      expect(converseMouthPose(1, 'sil'), ConverseMouthPose.closed);
      expect(converseMouthPose(1, 'PP'), ConverseMouthPose.closed);
      expect(converseMouthPose(1, 'oh'), ConverseMouthPose.rounded);
      expect(converseMouthPose(1, 'ou'), ConverseMouthPose.pursed);
      expect(converseMouthPose(1, 'FF'), ConverseMouthPose.teeth);
      expect(converseMouthPose(1, 'TH'), ConverseMouthPose.teeth);
      expect(converseMouthPose(1, 'E'), ConverseMouthPose.halfOpen);
      expect(converseMouthPose(0, 'aa'), ConverseMouthPose.closed);
      expect(converseMouthPose(.3, 'unknown'), ConverseMouthPose.halfOpen);
      expect(converseMouthPose(1, 'unknown'), ConverseMouthPose.open);
    },
  );

  testWidgets('playback keeps the approved neutral portrait stable', (
    tester,
  ) async {
    await tester.pumpWidget(_scene(openness: .4));
    expect(_pose(tester), ConverseMouthPose.halfOpen);
    final image = tester.widget<Image>(find.byType(Image));
    expect(image.image, _atlas);
    final atlasPosition = tester.widget<Positioned>(
      find.ancestor(of: find.byType(Image), matching: find.byType(Positioned)),
    );
    expect(atlasPosition.left, 0);
    expect(atlasPosition.top, 0);
    await tester.pumpWidget(_scene(openness: 1));
    expect(_pose(tester), ConverseMouthPose.open);
    final nextPosition = tester.widget<Positioned>(
      find.ancestor(of: find.byType(Image), matching: find.byType(Positioned)),
    );
    expect(nextPosition.left, atlasPosition.left);
    expect(nextPosition.top, atlasPosition.top);
    expect(find.bySemanticsLabel('Kinaiya speaking'), findsOneWidget);
    await tester.pumpWidget(_scene());
    expect(_mouth(), findsNothing);
    // Speech remains semantically active during the quiet gaps between words.
    expect(find.bySemanticsLabel('Kinaiya speaking'), findsOneWidget);
  });

  testWidgets(
    'reduced motion, stopped playback and closed cues stop the mouth',
    (tester) async {
      await tester.pumpWidget(_scene(openness: 1, reduced: true, viseme: 'aa'));
      expect(_mouth(), findsNothing);
      await tester.pumpWidget(_scene(openness: 1, speaking: false));
      expect(_mouth(), findsNothing);
      await tester.pumpWidget(_scene(openness: 1, viseme: 'PP'));
      expect(_mouth(), findsNothing);
      await tester.pumpWidget(_scene(openness: 1, viseme: 'oh'));
      expect(_pose(tester), ConverseMouthPose.rounded);
      await tester.pumpWidget(_scene(openness: 1, reduced: true));
      expect(_mouth(), findsNothing);
    },
  );

  testWidgets('thinking still selects its original atlas pose', (tester) async {
    await tester.pumpWidget(
      _scene(thinking: true, speaking: false, openness: 1),
    );
    final atlasPosition = tester.widget<Positioned>(
      find.ancestor(of: find.byType(Image), matching: find.byType(Positioned)),
    );
    expect(atlasPosition.left, -768);
    expect(atlasPosition.top, 0);
    expect(_mouth(), findsNothing);
    expect(find.bySemanticsLabel('Kinaiya thinking'), findsOneWidget);
  });

  testWidgets(
    'face and mouth share one cover transform on wide and narrow views',
    (tester) async {
      for (final size in [const Size(760, 220), const Size(180, 320)]) {
        await tester.pumpWidget(_scene(openness: 1, size: size));
        final fitted = tester.widget<FittedBox>(find.byType(FittedBox));
        expect(fitted.fit, BoxFit.cover);
        final mouthSize = tester.getSize(_mouth());
        expect(mouthSize, ConverseMouthPainter.canvasSize);
        expect(tester.getSize(find.byKey(_boundary)), size);
        expect(tester.takeException(), isNull);
      }
    },
  );

  testWidgets('actual atlas rendering changes pixels only inside the mouth', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(800, 600);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await tester.pumpWidget(_scene());
    await _decode(tester);
    final closedImage = await _capture(tester);
    final closed = await _pixels(tester, closedImage);
    closedImage.dispose();
    for (final cue in <String?>[null, 'oh', 'ou', 'FF']) {
      await tester.pumpWidget(_scene(openness: 1, viseme: cue));
      await tester.pump();
      final image = await _capture(tester);
      final pixels = await _pixels(tester, image);
      expect(image.width, 768);
      expect(image.height, 512);
      var changed = 0;
      for (var y = 0; y < image.height; y++) {
        for (var x = 0; x < image.width; x++) {
          final i = (y * image.width + x) * 4;
          if (pixels[i] != closed[i] ||
              pixels[i + 1] != closed[i + 1] ||
              pixels[i + 2] != closed[i + 2]) {
            changed++;
            expect(
              ConverseMouthPainter.mouthBounds.contains(
                Offset(x.toDouble(), y.toDouble()),
              ),
              isTrue,
              reason:
                  'Unexpected portrait change outside mouth at $x,$y ($cue)',
            );
          }
        }
      }
      expect(changed, greaterThan(80), reason: 'Mouth must visibly change');
      image.dispose();
    }
  });

  const proofPath = String.fromEnvironment('KINAIYA_SPEECH_RENDER_PROOF');
  if (proofPath.isNotEmpty) {
    testWidgets('capture approved-artwork mouth poses for visual inspection', (
      tester,
    ) async {
      tester.view.physicalSize = const Size(1152, 568);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      await tester.runAsync(() async {
        final font = FontLoader('Inter')
          ..addFont(rootBundle.load('assets/fonts/Inter-Regular.ttf'));
        await font.load();
      });
      const poses = <(String, double, String?)>[
        ('Silence / closed', 0, null),
        ('Half open', .3, null),
        ('Open', 1, null),
        ('Rounded / oh', 1, 'oh'),
        ('Pursed / ou', 1, 'ou'),
        ('Teeth / FF', 1, 'FF'),
      ];
      await tester.pumpWidget(
        MaterialApp(
          home: RepaintBoundary(
            key: _boundary,
            child: ColoredBox(
              color: const Color(0xff061631),
              child: Column(
                children: [
                  for (var row = 0; row < 2; row++)
                    Row(
                      children: [
                        for (final pose in poses.skip(row * 3).take(3))
                          Column(
                            children: [
                              SizedBox(
                                width: 384,
                                height: 256,
                                child: ConversePortrait(
                                  speaking: true,
                                  mouthOpenness: pose.$2,
                                  viseme: pose.$3,
                                ),
                              ),
                              SizedBox(
                                height: 28,
                                child: Text(
                                  pose.$1,
                                  style: const TextStyle(
                                    fontFamily: 'Inter',
                                    color: Colors.white,
                                    fontSize: 15,
                                  ),
                                ),
                              ),
                            ],
                          ),
                      ],
                    ),
                ],
              ),
            ),
          ),
        ),
      );
      await tester.runAsync(
        () => precacheImage(
          _atlas,
          tester.element(find.byType(ConversePortrait).first),
        ),
      );
      await tester.pumpAndSettle();
      final image = await _capture(tester);
      final data = await tester.runAsync(
        () => image.toByteData(format: ui.ImageByteFormat.png),
      );
      await tester.runAsync(() async {
        await File(proofPath).parent.create(recursive: true);
        await File(proofPath).writeAsBytes(data!.buffer.asUint8List());
      });
      image.dispose();
    });
  }
}
