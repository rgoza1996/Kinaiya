import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/ui/design_tokens.dart';
import 'package:kinaiya_flutter/app.dart';
import 'package:kinaiya_flutter/presentation/controller.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/character_view.dart';
import 'package:kinaiya_flutter/ui/visual_theme.dart';

double contrast(Color a, Color b) {
  final x = a.computeLuminance(), y = b.computeLuminance();
  return (x > y ? x + .05 : y + .05) / (x > y ? y + .05 : x + .05);
}

void main() {
  testWidgets('fully initialized bundled selection keeps the owner design', (
    tester,
  ) async {
    final presentation = PresentationController();
    await tester.runAsync(presentation.initialize);
    expect(presentation.selected, isNotNull);
    expect(presentation.selected!.packageJson, isNull);
    final state = AppState();
    await tester.pumpWidget(
      KinaiyaApp(state: state, presentation: presentation),
    );
    await tester.pump(const Duration(milliseconds: 200));
    final view = tester.widget<CharacterView>(find.byType(CharacterView));
    expect(view.useDesignScene, isTrue);
    expect(
      Theme.of(tester.element(find.byType(CharacterView))).colorScheme.primary,
      KinaiyaDesign.blue,
    );
    await presentation.setPreferences(
      const PresentationPreferences(primaryOverride: '#12693C'),
    );
    await tester.pump();
    expect(
      tester.widget<CharacterView>(find.byType(CharacterView)).useDesignScene,
      isFalse,
    );
    expect(
      Theme.of(tester.element(find.byType(CharacterView))).colorScheme.primary,
      const Color(0xFF12693C),
    );
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
    presentation.dispose();
  });

  test(
    'owner design has readable text and actions on its layered surfaces',
    () {
      for (final brightness in Brightness.values) {
        final theme = kinaiyaTheme(designSystem: true, brightness: brightness);
        final scheme = theme.colorScheme;
        final action = theme.textButtonTheme.style!.foregroundColor!.resolve(
          {},
        )!;
        expect(scheme.primary, KinaiyaDesign.blue);
        expect(theme.textTheme.bodyMedium!.fontFamily, 'Inter');
        for (final surface in [
          scheme.surface,
          scheme.surfaceContainerLow,
          scheme.surfaceContainer,
          scheme.surfaceContainerHigh,
          scheme.surfaceContainerHighest,
        ]) {
          expect(
            contrast(surface, scheme.onSurface),
            greaterThanOrEqualTo(4.5),
          );
          expect(contrast(surface, action), greaterThanOrEqualTo(4.5));
        }
        expect(
          contrast(scheme.primary, scheme.onPrimary),
          greaterThanOrEqualTo(4.5),
        );
        expect(
          contrast(scheme.primaryContainer, scheme.onPrimaryContainer),
          greaterThanOrEqualTo(4.5),
        );
      }
    },
  );

  test(
    'explicit colors take precedence over the owner design in either mode',
    () {
      for (final brightness in Brightness.values) {
        for (final pair in [
          [Colors.white, const Color(0xFF001122)],
          [const Color(0xFF12693C), const Color(0xFFF4FFF5)],
          [kinaiyaIndigo, kinaiyaWhite],
        ]) {
          final scheme = kinaiyaTheme(
            designSystem: true,
            brightness: brightness,
            primary: pair[0],
            background: pair[1],
          ).colorScheme;
          expect(scheme.primary, pair[0]);
          expect(scheme.surface, pair[1]);
          expect(
            contrast(scheme.primary, scheme.onPrimary),
            greaterThanOrEqualTo(4.5),
          );
          expect(
            contrast(scheme.surface, scheme.onSurface),
            greaterThanOrEqualTo(4.5),
          );
        }
      }
    },
  );
}
