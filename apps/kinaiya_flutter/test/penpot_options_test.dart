import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/config/options_catalog.dart';
import 'package:kinaiya_flutter/ui/settings_hub.dart';

Future<List<SettingsAction?>> _openOptions(
  WidgetTester tester, {
  Size size = const Size(1440, 1024),
  double scale = 1,
  SettingsAvailability availability = const SettingsAvailability(),
  ThemeData? theme,
}) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  final returned = <SettingsAction?>[];
  await tester.pumpWidget(
    MaterialApp(
      theme: theme ?? ThemeData.dark(),
      builder: (context, child) => MediaQuery(
        data: MediaQuery.of(
          context,
        ).copyWith(textScaler: TextScaler.linear(scale)),
        child: child!,
      ),
      home: Scaffold(
        body: Builder(
          builder: (context) => TextButton(
            onPressed: () async {
              returned.add(
                await showDialog<SettingsAction>(
                  context: context,
                  builder: (_) => SettingsHub(
                    availability: availability,
                    buildInfo: const SettingsBuildInfo(
                      label: 'Synthetic local build',
                    ),
                  ),
                ),
              );
            },
            child: const Text('Open options'),
          ),
        ),
      ),
    ),
  );
  await tester.tap(find.text('Open options'));
  await tester.pumpAndSettle();
  addTearDown(() async => tester.pumpWidget(const SizedBox.shrink()));
  return returned;
}

void main() {
  testWidgets('desktop Options uses the supplied two-panel composition', (
    tester,
  ) async {
    await _openOptions(tester);
    expect(find.text('Options'), findsOneWidget);
    expect(
      find.text('Tune your workspace, voice and privacy.'),
      findsOneWidget,
    );
    expect(find.text('Preferences'), findsOneWidget);
    expect(find.text('Quick settings'), findsOneWidget);
    expect(find.text('About'), findsOneWidget);
    final preferences = find.byKey(const Key('options-preferences'));
    final summary = find.byKey(const Key('options-summary'));
    expect(tester.getTopLeft(preferences).dy, tester.getTopLeft(summary).dy);
    expect(
      tester.getTopRight(preferences).dx,
      lessThan(tester.getTopLeft(summary).dx),
    );
    expect(tester.getSize(find.byType(Dialog)).width, greaterThan(1000));
    for (final action in SettingsAction.values) {
      expect(find.byKey(Key('settings-entry-${action.name}')), findsOneWidget);
    }
    expect(tester.takeException(), isNull);
  });

  testWidgets('narrow enlarged Options stacks panels and keeps Close fixed', (
    tester,
  ) async {
    final returned = await _openOptions(
      tester,
      size: const Size(320, 480),
      scale: 2,
    );
    expect(
      tester.getTopLeft(find.byKey(const Key('options-summary'))).dy,
      greaterThan(
        tester.getBottomLeft(find.byKey(const Key('options-preferences'))).dy,
      ),
    );
    expect(tester.takeException(), isNull);
    expect(
      find.byKey(const Key('settings-search')).hitTestable(),
      findsOneWidget,
    );
    await tester.tap(find.byKey(const Key('settings-search')));
    await tester.pumpAndSettle();
    expect(
      tester
          .widget<TextField>(find.byKey(const Key('settings-search')))
          .focusNode!
          .hasFocus,
      isTrue,
    );
    await tester.ensureVisible(find.text('Synthetic local build'));
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
    expect(
      find.byKey(const Key('settings-close')).hitTestable(),
      findsOneWidget,
    );
    await tester.tap(find.byKey(const Key('settings-close')));
    await tester.pumpAndSettle();
    expect(returned, <SettingsAction?>[null]);
  });

  testWidgets(
    'quick navigation obeys availability and only returns an action',
    (tester) async {
      final returned = await _openOptions(tester);
      final voice = tester.widget<TextButton>(
        find.byKey(const Key('options-quick-voice')),
      );
      expect(voice.onPressed, isNull);
      expect(returned, isEmpty);
      await tester.tap(find.byKey(const Key('options-quick-voice')));
      await tester.pumpAndSettle();
      expect(returned, isEmpty);
      expect(find.byType(SettingsHub), findsOneWidget);
      await tester.tap(find.byKey(const Key('options-quick-appearance')));
      await tester.pumpAndSettle();
      expect(returned, <SettingsAction?>[SettingsAction.appearance]);
    },
  );

  testWidgets(
    'available voice shortcut returns the existing voice destination',
    (tester) async {
      final returned = await _openOptions(
        tester,
        availability: const SettingsAvailability(voiceOptions: true),
      );
      await tester.tap(find.byKey(const Key('options-quick-voice')));
      await tester.pumpAndSettle();
      expect(returned, <SettingsAction?>[SettingsAction.voice]);
    },
  );

  testWidgets('search keeps metadata filtering without unrelated shortcuts', (
    tester,
  ) async {
    final returned = await _openOptions(tester);
    await tester.enterText(
      find.byKey(const Key('settings-search')),
      'retained notes',
    );
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('settings-entry-memory')), findsOneWidget);
    expect(find.byKey(const Key('settings-entry-voice')), findsNothing);
    expect(find.text('Quick settings'), findsNothing);
    expect(find.text('About'), findsOneWidget);
    expect(returned, isEmpty);
    expect(find.byType(Switch), findsNothing);
    expect(find.byType(Slider), findsNothing);
  });

  testWidgets('active palette overrides the Options surfaces', (tester) async {
    final custom = ThemeData.dark().copyWith(
      scaffoldBackgroundColor: const Color(0xFF302238),
      colorScheme: ThemeData.dark().colorScheme.copyWith(
        surfaceContainerLow: const Color(0xFF412D4C),
        outlineVariant: const Color(0xFFDBA5CB),
      ),
    );
    await _openOptions(tester, theme: custom);
    expect(
      tester.widget<Dialog>(find.byType(Dialog)).backgroundColor,
      custom.scaffoldBackgroundColor,
    );
    final panel = tester.widget<Container>(
      find
          .descendant(
            of: find.byKey(const Key('options-preferences')),
            matching: find.byType(Container),
          )
          .first,
    );
    final decoration = panel.decoration! as BoxDecoration;
    expect(decoration.color, custom.colorScheme.surfaceContainerLow);
    expect(
      (decoration.border! as Border).top.color,
      custom.colorScheme.outlineVariant,
    );
  });
}
