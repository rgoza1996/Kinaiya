import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/presentation/controller.dart';
import 'package:kinaiya_flutter/presentation/platform.dart';
import 'package:kinaiya_flutter/ui/presentation_settings.dart';
import 'package:kinaiya_flutter/ui/visual_theme.dart';
import 'package:kinaiya_flutter/ui/widgets.dart';

class MemoryPresentationStorage implements PresentationStorage {
  @override
  bool get persistent => false;
  String? record;
  @override
  Future<String?> read() async => record;
  @override
  Future<void> write(String value) async => record = value;
}

class FailingPresentationStorage extends MemoryPresentationStorage {
  @override
  bool get persistent => true;
  @override
  Future<void> write(String value) async =>
      throw const FormatException('private-storage-error-marker');
}

Future<PresentationController> initialized(
  WidgetTester tester, {
  PresentationStorage? storage,
}) async {
  final controller = PresentationController(
    storage: storage ?? MemoryPresentationStorage(),
  );
  await tester.runAsync(controller.initialize);
  addTearDown(controller.dispose);
  return controller;
}

Future<void> mount(
  WidgetTester tester,
  PresentationController controller, {
  Future<String?> Function()? picker,
  Future<void> Function(String)? downloader,
  Size size = const Size(1000, 1100),
  double textScale = 1,
}) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  await tester.pumpWidget(
    MaterialApp(
      theme: kinaiyaTheme(),
      builder: (context, child) => MediaQuery(
        data: MediaQuery.of(context).copyWith(
          textScaler: TextScaler.linear(textScale),
          disableAnimations: true,
        ),
        child: child!,
      ),
      home: Scaffold(
        body: Builder(
          builder: (context) => TextButton(
            onPressed: () => showDialog<void>(
              context: context,
              builder: (_) => PresentationSettings(
                controller: controller,
                packagePicker: picker,
                packageDownloader: downloader,
              ),
            ),
            child: const Text('Open appearance'),
          ),
        ),
      ),
    ),
  );
  await tester.tap(find.text('Open appearance'));
  await tester.pumpAndSettle();
}

Future<void> press(WidgetTester tester, String label) async {
  final target = find.text(label);
  await tester.ensureVisible(target);
  await tester.tap(target);
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 300));
}

Future<void> finishIo(WidgetTester tester, bool Function() finished) async {
  for (var attempt = 0; attempt < 100 && !finished(); attempt++) {
    await tester.runAsync(
      () => Future<void>.delayed(const Duration(milliseconds: 20)),
    );
    await tester.pump();
  }
  expect(
    finished(),
    isTrue,
    reason: 'Local package operation completed within two seconds.',
  );
}

double contrast(Color a, Color b) {
  final x = a.computeLuminance(), y = b.computeLuminance();
  return (x > y ? x + 0.05 : y + 0.05) / (x > y ? y + 0.05 : x + 0.05);
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('default colors preserve the established light and dark schemes', () {
    final light = kinaiyaTheme().colorScheme;
    expect(light.primary, kinaiyaIndigo);
    expect(light.surface, kinaiyaWhite);
    expect(light.onSurface, kinaiyaNavy);
    expect(light.surfaceContainer, kinaiyaMist);
    final dark = kinaiyaTheme(brightness: Brightness.dark).colorScheme;
    expect(dark.primary, kinaiyaWhite);
    expect(dark.surface, kinaiyaNavy);
    expect(dark.onPrimary, kinaiyaIndigo);
    for (final brightness in Brightness.values) {
      expect(
        kinaiyaTheme(
          brightness: brightness,
          primary: kinaiyaIndigo,
          background: kinaiyaWhite,
        ).colorScheme,
        kinaiyaTheme(brightness: brightness).colorScheme,
      );
    }
  });

  test(
    'custom fills change appearance with readable foregrounds and controls',
    () {
      for (final brightness in Brightness.values) {
        for (final colors in [
          [const Color(0xFFFFFFFE), const Color(0xFFFDFDFD)],
          [const Color(0xFF010101), const Color(0xFF020202)],
          [const Color(0xFF777777), const Color(0xFF777777)],
          [const Color(0xFFCC3366), const Color(0xFF88AA77)],
        ]) {
          final theme = kinaiyaTheme(
            brightness: brightness,
            primary: colors[0],
            background: colors[1],
          );
          final scheme = theme.colorScheme;
          expect(scheme.primary, colors[0]);
          expect(theme.scaffoldBackgroundColor, colors[1]);
          expect(
            scheme,
            isNot(kinaiyaTheme(brightness: brightness).colorScheme),
          );
          for (final pair in [
            [scheme.primary, scheme.onPrimary],
            [scheme.surface, scheme.onSurface],
            [scheme.surfaceContainerLow, scheme.onSurface],
            [scheme.surfaceContainer, scheme.onSurface],
            [scheme.surfaceContainerHigh, scheme.onSurface],
            [scheme.surfaceContainerHighest, scheme.onSurface],
            [scheme.primaryContainer, scheme.onPrimaryContainer],
            [scheme.secondary, scheme.onSecondary],
            [scheme.error, scheme.onError],
            [scheme.errorContainer, scheme.onErrorContainer],
            [scheme.tertiaryContainer, scheme.onTertiaryContainer],
          ]) {
            expect(contrast(pair[0], pair[1]), greaterThanOrEqualTo(4.5));
          }
          final textButtonInk = theme.textButtonTheme.style!.foregroundColor!
              .resolve({})!;
          for (final surface in [
            scheme.surface,
            scheme.surfaceContainerLow,
            scheme.surfaceContainer,
            scheme.surfaceContainerHigh,
            scheme.surfaceContainerHighest,
          ]) {
            expect(contrast(textButtonInk, surface), greaterThanOrEqualTo(4.5));
          }
          expect(
            theme.inputDecorationTheme.fillColor,
            scheme.surfaceContainerHigh,
          );
        }
      }
    },
  );

  testWidgets('custom theme paints selected surface and button fills', (
    tester,
  ) async {
    Future<void> showColors(Color primary, Color background) =>
        tester.pumpWidget(
          MaterialApp(
            theme: kinaiyaTheme(primary: primary, background: background),
            home: Scaffold(
              body: FilledButton(
                onPressed: () {},
                child: const Text('Sample action'),
              ),
            ),
          ),
        );
    final surface = find
        .descendant(of: find.byType(Scaffold), matching: find.byType(Material))
        .first;
    final button = find
        .descendant(
          of: find.byType(FilledButton),
          matching: find.byType(Material),
        )
        .first;
    await showColors(const Color(0xFFCC3366), const Color(0xFFEAF2DD));
    expect(tester.widget<Material>(surface).color, const Color(0xFFEAF2DD));
    expect(tester.widget<Material>(button).color, const Color(0xFFCC3366));
    await showColors(const Color(0xFF77AACC), const Color(0xFF142135));
    await tester.pumpAndSettle();
    expect(tester.widget<Material>(surface).color, const Color(0xFF142135));
    expect(tester.widget<Material>(button).color, const Color(0xFF77AACC));
  });

  testWidgets('shows pack metadata and truthful session/platform notices', (
    tester,
  ) async {
    final controller = await initialized(tester);
    await mount(tester, controller);
    expect(find.text(controller.selected!.pack.name), findsOneWidget);
    expect(
      find.text('Author: ${controller.selected!.pack.author}'),
      findsOneWidget,
    );
    expect(
      find.text('Version: ${controller.selected!.pack.packVersion}'),
      findsOneWidget,
    );
    expect(
      find.text('License: ${controller.selected!.pack.license}'),
      findsOneWidget,
    );
    expect(find.textContaining('last for this session'), findsOneWidget);
    if (!presentationImportSupported) {
      final import = tester.widget<OutlinedButton>(
        find.widgetWithText(OutlinedButton, 'Import character pack'),
      );
      expect(import.onPressed, isNull);
      expect(find.textContaining('available in the web app'), findsOneWidget);
    }
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    '390px dialog with 2x text scrolls to every preference and closes by keyboard',
    (tester) async {
      final controller = await initialized(tester);
      await mount(tester, controller, size: const Size(390, 844), textScale: 2);
      for (final label in [
        'Reduce motion',
        'Show character',
        'Show captions',
        'Use character pack colors',
      ]) {
        await press(tester, label);
        expect(tester.takeException(), isNull);
      }
      expect(controller.preferences.reducedMotion, isTrue);
      expect(controller.preferences.showCharacter, isFalse);
      expect(controller.preferences.showCaptions, isFalse);
      expect(controller.preferences.usePackPalette, isFalse);
      await tester.ensureVisible(find.text('Apply color overrides'));
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
      await tester.sendKeyEvent(LogicalKeyboardKey.escape);
      await tester.pumpAndSettle();
      expect(find.byType(PresentationSettings), findsNothing);
    },
  );

  testWidgets(
    'palette validates exact hex, applies colors and clears overrides',
    (tester) async {
      final controller = await initialized(tester);
      await mount(tester, controller);
      final primary = find.widgetWithText(TextField, 'Primary color override');
      final background = find.widgetWithText(
        TextField,
        'Background color override',
      );
      await tester.ensureVisible(primary);
      await tester.enterText(primary, '#GGGGGG');
      await press(tester, 'Apply color overrides');
      expect(find.text('Use # followed by six color digits.'), findsOneWidget);
      expect(controller.preferences.primaryOverride, isNull);
      await tester.ensureVisible(primary);
      await tester.enterText(primary, '#aabbcc');
      await tester.ensureVisible(background);
      await tester.enterText(background, '#000001');
      await press(tester, 'Apply color overrides');
      expect(controller.preferences.primaryOverride, '#AABBCC');
      expect(controller.preferences.backgroundOverride, '#000001');
      await press(tester, 'Clear color overrides');
      expect(controller.preferences.primaryOverride, isNull);
      expect(controller.preferences.backgroundOverride, isNull);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'import previews real artwork; cancel preserves current and Use activates',
    (tester) async {
      final controller = await initialized(tester);
      final original = controller.selected!;
      final raw = await tester.runAsync(controller.exportPackage);
      final envelope = jsonDecode(raw!) as Map<String, dynamic>;
      (envelope['manifest'] as Map<String, dynamic>)['name'] =
          'Preview character';
      final package = jsonEncode(envelope);
      await mount(tester, controller, picker: () async => package);
      await tester.tap(find.text('Import character pack'));
      await finishIo(
        tester,
        () => find.byType(MascotWidget).evaluate().isNotEmpty,
      );
      await tester.pumpAndSettle();
      expect(find.text('Preview character pack'), findsOneWidget);
      final drawn = find.byWidgetPredicate(
        (widget) =>
            widget is CustomPaint && widget.painter is MascotFramePainter,
      );
      await finishIo(tester, () => drawn.evaluate().isNotEmpty);
      final painter =
          tester.widget<CustomPaint>(drawn).painter! as MascotFramePainter;
      expect(painter.image.width, original.pack.sheets.values.first.width);
      expect(painter.src, original.pack.sheets.values.first.frameRect(0));
      await tester.pump(const Duration(seconds: 1));
      expect(
        (tester.widget<CustomPaint>(drawn).painter! as MascotFramePainter).src,
        painter.src,
      );
      final mascot = tester.widget<MascotWidget>(find.byType(MascotWidget));
      expect(mascot.pack.name, 'Preview character');
      expect(mascot.bundle, isNotNull);
      expect(identical(controller.selected, original), isTrue);
      await press(tester, 'Cancel preview');
      expect(find.byType(MascotWidget), findsNothing);
      expect(identical(controller.selected, original), isTrue);
      await press(tester, 'Import character pack');
      await finishIo(
        tester,
        () => find.byType(MascotWidget).evaluate().isNotEmpty,
      );
      await tester.pumpAndSettle();
      await press(tester, 'Use character pack');
      expect(controller.selected!.pack.name, 'Preview character');
      expect(find.byType(MascotWidget), findsNothing);
      await press(tester, 'Restore default character');
      await tester.runAsync(
        () async => Future<void>.delayed(const Duration(milliseconds: 100)),
      );
      await tester.pumpAndSettle();
      expect(controller.selected!.pack.name, original.pack.name);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'picker cancel and invalid untrusted content preserve selection',
    (tester) async {
      final controller = await initialized(tester);
      final original = controller.selected;
      var invalid = false;
      await mount(
        tester,
        controller,
        picker: () async => invalid ? 'private-untrusted-marker' : null,
      );
      await press(tester, 'Import character pack');
      expect(controller.selected, same(original));
      expect(find.text('Preview character pack'), findsNothing);
      invalid = true;
      await press(tester, 'Import character pack');
      expect(controller.selected, same(original));
      expect(
        find.textContaining('Could not open this character pack'),
        findsOneWidget,
      );
      expect(find.textContaining('private-untrusted-marker'), findsNothing);
      expect(find.text(controller.notice!), findsOneWidget);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'export downloads only after explicit action and keeps selection',
    (tester) async {
      final controller = await initialized(tester);
      final original = controller.selected;
      String? downloaded;
      await mount(
        tester,
        controller,
        downloader: (raw) async => downloaded = raw,
      );
      expect(downloaded, isNull);
      await press(tester, 'Export current pack');
      await tester.runAsync(
        () async => Future<void>.delayed(const Duration(milliseconds: 50)),
      );
      await tester.pumpAndSettle();
      expect(
        (jsonDecode(downloaded!) as Map<String, dynamic>)['package_schema'],
        1,
      );
      expect(controller.selected, same(original));
      expect(find.text('Character pack exported.'), findsOneWidget);
    },
  );

  testWidgets(
    'activation, export and restore retain local storage failure notice',
    (tester) async {
      final controller = await initialized(
        tester,
        storage: FailingPresentationStorage(),
      );
      final original = controller.selected!;
      final exported = await tester.runAsync(controller.exportPackage);
      final envelope = jsonDecode(exported!) as Map<String, dynamic>;
      (envelope['manifest'] as Map<String, dynamic>)['name'] =
          'Session character';
      final package = jsonEncode(envelope);
      String? downloaded;
      await mount(
        tester,
        controller,
        picker: () async => package,
        downloader: (raw) async => downloaded = raw,
      );
      await press(tester, 'Import character pack');
      await finishIo(
        tester,
        () => find.byType(MascotWidget).evaluate().isNotEmpty,
      );
      await press(tester, 'Use character pack');
      expect(controller.selected!.pack.name, 'Session character');
      const warning =
          'Presentation is active for this session, but could not be saved locally.';
      expect(controller.notice, warning);
      expect(find.text('Character pack selected.'), findsOneWidget);
      expect(find.text(warning), findsOneWidget);
      await press(tester, 'Export current pack');
      await finishIo(
        tester,
        () => find.text('Character pack exported.').evaluate().isNotEmpty,
      );
      await tester.pumpAndSettle();
      expect(
        (jsonDecode(downloaded!) as Map<String, dynamic>)['package_schema'],
        1,
      );
      expect(find.text('Character pack exported.'), findsOneWidget);
      expect(find.text(warning), findsOneWidget);
      await press(tester, 'Restore default character');
      await finishIo(
        tester,
        () => controller.selected!.pack.name == original.pack.name,
      );
      await tester.pumpAndSettle();
      expect(controller.selected!.pack.name, original.pack.name);
      expect(find.text('Default character restored.'), findsOneWidget);
      expect(find.text(warning), findsOneWidget);
      expect(find.textContaining('private-storage-error-marker'), findsNothing);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('timed-out picker is retired before a late result arrives', (
    tester,
  ) async {
    final controller = await initialized(tester);
    final original = controller.selected;
    final picker = Completer<String?>();
    await mount(tester, controller, picker: () => picker.future);
    await tester.tap(find.text('Import character pack'));
    await tester.pump();
    final button = tester.widget<OutlinedButton>(
      find.widgetWithText(OutlinedButton, 'Import character pack'),
    );
    expect(button.onPressed, isNull);
    await tester.pump(const Duration(minutes: 2, milliseconds: 1));
    await tester.pump();
    expect(
      find.textContaining('Could not open this character pack'),
      findsOneWidget,
    );
    picker.complete('untrusted late file');
    await tester.pumpAndSettle();
    expect(controller.selected, same(original));
    expect(find.text('Preview character pack'), findsNothing);
    expect(find.textContaining('untrusted late file'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    'late picker completion after close cannot activate or update disposed UI',
    (tester) async {
      final controller = await initialized(tester);
      final original = controller.selected;
      final picker = Completer<String?>();
      await mount(tester, controller, picker: () => picker.future);
      await tester.tap(find.text('Import character pack'));
      await tester.pump();
      await tester.tap(find.byTooltip('Close appearance settings'));
      await tester.pumpAndSettle();
      picker.complete('untrusted late file');
      await tester.pumpAndSettle();
      expect(controller.selected, same(original));
      expect(tester.takeException(), isNull);
    },
  );
}
