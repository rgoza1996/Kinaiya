import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart' show RenderImage;
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/ui/anchored_moonlit_scene.dart';

const _source = Size(1536, 1024);
const _asset = 'assets/design/kinaiya-moonlit-background-v1.png';

class MissingArtworkBundle extends CachingAssetBundle {
  @override
  Future<ByteData> load(String key) =>
      Future.error(StateError('Synthetic missing artwork'));
}

Future<RenderImage> decodedBackground(WidgetTester tester) async {
  final raw = find.descendant(
    of: find.byKey(const ValueKey('moonlit-illustration')),
    matching: find.byType(RawImage),
  );
  for (var attempt = 0; attempt < 50; attempt++) {
    if (raw.evaluate().length == 1) {
      final image = tester.renderObject<RenderImage>(raw);
      if (image.image != null) return image;
    }
    await tester.runAsync(
      () => Future<void>.delayed(const Duration(milliseconds: 10)),
    );
    await tester.pump();
  }
  fail('The bundled background did not decode.');
}

void main() {
  for (final sample in [
    (const Size(390, 844), 844.0, 560.0, const Offset(.5, .68)),
    (const Size(1536, 960), 960.0, 690.0, const Offset(.5, .68)),
    (const Size(2688, 1352), 1352.0, 990.0, const Offset(.5, .68)),
    (const Size(390, 844), 1920.0, 620.0, const Offset(.5, .68)),
    (const Size(1536, 960), 960.0, 660.0, const Offset(.25, .68)),
    (const Size(1536, 960), 960.0, 660.0, const Offset(.75, .68)),
  ]) {
    testWidgets(
      'decoded background preserves anchor, aspect and coverage $sample',
      (tester) async {
        final (viewport, canvasHeight, surfaceY, anchor) = sample;
        tester.view.physicalSize = viewport;
        tester.view.devicePixelRatio = 1;
        addTearDown(tester.view.resetPhysicalSize);
        addTearDown(tester.view.resetDevicePixelRatio);
        await tester.pumpWidget(
          MaterialApp(
            home: Scaffold(
              body: SingleChildScrollView(
                child: AnchoredMoonlitScene(
                  artwork: MoonlitArtwork(
                    asset: _asset,
                    sourceSize: _source,
                    surfaceAnchor: anchor,
                  ),
                  surfaceY: surfaceY,
                  child: SizedBox(width: viewport.width, height: canvasHeight),
                ),
              ),
            ),
          ),
        );
        final image = await decodedBackground(tester);
        expect(image.image!.width, _source.width);
        expect(image.image!.height, _source.height);
        final canvas = tester.getRect(find.byType(AnchoredMoonlitScene));
        expect(canvas.size, Size(viewport.width, canvasHeight));
        final fitted = applyBoxFit(image.fit!, _source, image.size);
        final localPaint = image.alignment
            .resolve(image.textDirection)
            .inscribe(fitted.destination, Offset.zero & image.size);
        final painted = localPaint.shift(image.localToGlobal(Offset.zero));
        const epsilon = .0001;
        expect(painted.width / painted.height, closeTo(1.5, epsilon));
        expect(
          painted.left + painted.width * anchor.dx,
          closeTo(canvas.center.dx, epsilon),
        );
        expect(
          painted.top + painted.height * anchor.dy,
          closeTo(canvas.top + surfaceY, epsilon),
        );
        expect(painted.left, lessThanOrEqualTo(canvas.left + epsilon));
        expect(painted.top, lessThanOrEqualTo(canvas.top + epsilon));
        expect(painted.right, greaterThanOrEqualTo(canvas.right - epsilon));
        expect(painted.bottom, greaterThanOrEqualTo(canvas.bottom - epsilon));
        expect(tester.takeException(), isNull);
      },
    );
  }

  testWidgets('missing artwork uses explicit fallback and keeps child input', (
    tester,
  ) async {
    var taps = 0;
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: DefaultAssetBundle(
            bundle: MissingArtworkBundle(),
            child: AnchoredMoonlitScene(
              artwork: const MoonlitArtwork(
                asset: 'assets/design/synthetic-missing.png',
                sourceSize: _source,
                surfaceAnchor: Offset(.5, .68),
              ),
              surfaceY: 400,
              fallback: const ColoredBox(
                key: ValueKey('artwork-fallback'),
                color: Colors.blue,
              ),
              child: SizedBox.expand(
                child: Center(
                  child: TextButton(
                    onPressed: () => taps++,
                    child: const Text('Keep controls'),
                  ),
                ),
              ),
            ),
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();
    expect(find.byKey(const ValueKey('artwork-fallback')), findsOneWidget);
    expect(find.byType(RawImage), findsNothing);
    await tester.tap(find.text('Keep controls'));
    expect(taps, 1);
    expect(tester.takeException(), isNull);
  });
}
