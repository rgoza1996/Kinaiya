import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/config/options_catalog.dart';
import 'package:kinaiya_flutter/ui/settings_hub.dart';

class _HubCapture {
  SettingsAction? picked;
}

Future<_HubCapture> _openHub(
  WidgetTester tester, {
  SettingsAvailability availability = const SettingsAvailability(),
  SettingsBuildInfo buildInfo = const SettingsBuildInfo(
    label: 'Synthetic build label',
  ),
  Size size = const Size(800, 600),
  double textScale = 1.0,
}) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  final capture = _HubCapture();
  await tester.pumpWidget(
    MaterialApp(
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
            onPressed: () async {
              final result = await showDialog<SettingsAction>(
                context: context,
                builder: (_) => SettingsHub(
                  availability: availability,
                  buildInfo: buildInfo,
                ),
              );
              capture.picked = result;
            },
            child: const Text('Open settings'),
          ),
        ),
      ),
    ),
  );
  await tester.tap(find.text('Open settings'));
  await tester.pumpAndSettle();
  expect(find.byType(SettingsHub), findsOneWidget);
  return capture;
}

Future<void> _closeHarness(WidgetTester tester) async {
  // Dismiss any open dialog so the next pump does not leak routes.
  if (find.byType(SettingsHub).evaluate().isNotEmpty) {
    await tester.sendKeyEvent(LogicalKeyboardKey.escape);
    await tester.pumpAndSettle();
  }
  await tester.pumpWidget(const SizedBox.shrink());
}

void main() {
  testWidgets('memory selection returns memory while offline', (tester) async {
    final capture = await _openHub(tester);
    addTearDown(() => _closeHarness(tester));
    expect(find.byKey(const Key('settings-entry-memory')), findsOneWidget);
    await tester.ensureVisible(find.byKey(const Key('settings-entry-memory')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('settings-entry-memory')));
    await tester.pumpAndSettle();
    expect(capture.picked, SettingsAction.memory);
    expect(find.byType(SettingsHub), findsNothing);
  });

  testWidgets('appearance selection returns appearance', (tester) async {
    final capture = await _openHub(tester);
    addTearDown(() => _closeHarness(tester));
    await tester.ensureVisible(
      find.byKey(const Key('settings-entry-appearance')),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('settings-entry-appearance')));
    await tester.pumpAndSettle();
    expect(capture.picked, SettingsAction.appearance);
  });

  testWidgets('disabled selection cannot return and shows reason', (
    tester,
  ) async {
    const availability = SettingsAvailability();
    final capture = await _openHub(tester, availability: availability);
    addTearDown(() => _closeHarness(tester));
    expect(
      find.text('Voice options are unavailable in this view.'),
      findsOneWidget,
    );
    expect(
      find.text('Connect and load the Agent controls first.'),
      findsOneWidget,
    );
    expect(find.text('Connect to the owner service first.'), findsOneWidget);
    await tester.ensureVisible(find.byKey(const Key('settings-entry-voice')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('settings-entry-voice')));
    await tester.pumpAndSettle();
    expect(capture.picked, isNull);
    expect(find.byType(SettingsHub), findsOneWidget);
    await tester.ensureVisible(
      find.byKey(const Key('settings-entry-agentControls')),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('settings-entry-agentControls')));
    await tester.pumpAndSettle();
    expect(capture.picked, isNull);
    expect(find.byType(SettingsHub), findsOneWidget);
  });

  testWidgets('search field is bounded to 128 characters', (tester) async {
    await _openHub(tester);
    addTearDown(() => _closeHarness(tester));
    final field = tester.widget<TextField>(
      find.byKey(const Key('settings-search')),
    );
    expect(field.maxLength, 128);
  });

  testWidgets('multiword search matches every query word', (tester) async {
    await _openHub(tester);
    addTearDown(() => _closeHarness(tester));
    await tester.enterText(
      find.byKey(const Key('settings-search')),
      'saved notes',
    );
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('settings-entry-memory')), findsOneWidget);
    expect(find.byKey(const Key('settings-entry-voice')), findsNothing);
    expect(find.byKey(const Key('settings-entry-appearance')), findsNothing);
  });

  testWidgets('keyword search matches entry metadata', (tester) async {
    await _openHub(tester);
    addTearDown(() => _closeHarness(tester));
    await tester.enterText(find.byKey(const Key('settings-search')), 'palette');
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('settings-entry-appearance')), findsOneWidget);
    expect(find.byKey(const Key('settings-entry-memory')), findsNothing);
  });

  testWidgets('category search matches section label', (tester) async {
    await _openHub(tester);
    addTearDown(() => _closeHarness(tester));
    await tester.enterText(
      find.byKey(const Key('settings-search')),
      'Converse',
    );
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('settings-entry-voice')), findsOneWidget);
    expect(find.byKey(const Key('settings-entry-memory')), findsOneWidget);
    expect(find.byKey(const Key('settings-entry-appearance')), findsNothing);
    expect(find.byKey(const Key('settings-entry-connection')), findsNothing);
  });

  testWidgets('disjoint words match nothing; clear control restores entries', (
    tester,
  ) async {
    await _openHub(tester);
    addTearDown(() => _closeHarness(tester));
    await tester.enterText(
      find.byKey(const Key('settings-search')),
      'palette memory',
    );
    await tester.pumpAndSettle();
    expect(find.text('No settings match your search.'), findsOneWidget);
    expect(find.byKey(const Key('settings-entry-memory')), findsNothing);
    await tester.tap(find.byTooltip('Clear search'));
    await tester.pumpAndSettle();
    expect(find.text('No settings match your search.'), findsNothing);
    expect(find.byKey(const Key('settings-entry-memory')), findsOneWidget);
    expect(find.byKey(const Key('settings-entry-connection')), findsOneWidget);
    final field = tester.widget<TextField>(
      find.byKey(const Key('settings-search')),
    );
    expect(field.controller?.text ?? '', isEmpty);
  });

  testWidgets('availability updates while open', (tester) async {
    tester.view.physicalSize = const Size(800, 600);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    final availability = ValueNotifier<SettingsAvailability>(
      const SettingsAvailability(),
    );
    addTearDown(availability.dispose);
    final capture = _HubCapture();
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: Builder(
            builder: (context) => TextButton(
              onPressed: () async {
                final result = await showDialog<SettingsAction>(
                  context: context,
                  builder: (_) => ValueListenableBuilder<SettingsAvailability>(
                    valueListenable: availability,
                    builder: (_, value, _) => SettingsHub(
                      availability: value,
                      buildInfo: const SettingsBuildInfo(
                        label: 'Synthetic build label',
                      ),
                    ),
                  ),
                );
                capture.picked = result;
              },
              child: const Text('Open settings'),
            ),
          ),
        ),
      ),
    );
    await tester.tap(find.text('Open settings'));
    await tester.pumpAndSettle();
    expect(
      find.text('Voice options are unavailable in this view.'),
      findsOneWidget,
    );
    availability.value = const SettingsAvailability(
      voiceOptions: true,
      agentControls: true,
      mcpConnections: true,
    );
    await tester.pumpAndSettle();
    expect(
      find.text('Voice options are unavailable in this view.'),
      findsNothing,
    );
    await tester.ensureVisible(find.byKey(const Key('settings-entry-voice')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('settings-entry-voice')));
    await tester.pumpAndSettle();
    expect(capture.picked, SettingsAction.voice);
    expect(find.byType(SettingsHub), findsNothing);
  });

  testWidgets('about shows literal build info with odd punctuation', (
    tester,
  ) async {
    const label = 'v "1.0" <beta> & \'quoted\' \u2014 \u2713 synthetic';
    const revision = 'rev#1/extra:;{}[]\\!@\$%^&*()';
    await _openHub(
      tester,
      buildInfo: const SettingsBuildInfo(label: label, revision: revision),
    );
    addTearDown(() => _closeHarness(tester));
    await tester.ensureVisible(find.text(label));
    await tester.pumpAndSettle();
    expect(find.text(label), findsOneWidget);
    expect(find.text(revision), findsOneWidget);
    expect(find.widgetWithText(SelectableText, label), findsOneWidget);
    expect(find.widgetWithText(SelectableText, revision), findsOneWidget);
    expect(find.text('About'), findsOneWidget);
  });

  testWidgets('default build info says Development build', (tester) async {
    tester.view.physicalSize = const Size(800, 600);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await tester.pumpWidget(
      const MaterialApp(
        home: Scaffold(body: SettingsHub(availability: SettingsAvailability())),
      ),
    );
    await tester.ensureVisible(find.text('Development build'));
    await tester.pumpAndSettle();
    expect(find.text('Development build'), findsOneWidget);
  });

  testWidgets('Escape closes without returning an action', (tester) async {
    final capture = await _openHub(tester);
    addTearDown(() => _closeHarness(tester));
    await tester.sendKeyEvent(LogicalKeyboardKey.escape);
    await tester.pumpAndSettle();
    expect(find.byType(SettingsHub), findsNothing);
    expect(capture.picked, isNull);
  });

  testWidgets('keyboard activation selects the focused entry', (tester) async {
    const availability = SettingsAvailability(
      voiceOptions: true,
      agentControls: true,
      mcpConnections: true,
    );
    final capture = await _openHub(tester, availability: availability);
    addTearDown(() => _closeHarness(tester));
    await tester.tap(find.byKey(const Key('settings-search')));
    await tester.pump();
    await tester.sendKeyEvent(LogicalKeyboardKey.tab);
    await tester.pump();
    // Focus should now be on the first entry; activating returns it.
    await tester.sendKeyEvent(LogicalKeyboardKey.enter);
    await tester.pumpAndSettle();
    expect(capture.picked, SettingsAction.connection);
    expect(find.byType(SettingsHub), findsNothing);
  });

  testWidgets('320x480 at 2x text keeps dismissal and selection reachable', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(320, 480);
    tester.view.devicePixelRatio = 1;
    tester.platformDispatcher.textScaleFactorTestValue = 2;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
    final capture = await _openHub(
      tester,
      size: const Size(320, 480),
      textScale: 2,
    );
    addTearDown(() => _closeHarness(tester));
    expect(tester.takeException(), isNull);
    await tester.ensureVisible(
      find.byKey(const Key('settings-entry-connection')),
    );
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
    expect(
      find.byKey(const Key('settings-entry-connection')).hitTestable(),
      findsOneWidget,
    );
    await tester.tap(find.byKey(const Key('settings-entry-connection')));
    await tester.pumpAndSettle();
    expect(capture.picked, SettingsAction.connection);
    expect(find.byType(SettingsHub), findsNothing);
  });

  testWidgets('keyboard and enlarged text keep search and Close reachable', (
    tester,
  ) async {
    final capture = await _openHub(
      tester,
      size: const Size(320, 480),
      textScale: 2,
    );
    addTearDown(() => _closeHarness(tester));
    addTearDown(tester.view.resetViewInsets);
    await tester.tap(find.byKey(const Key('settings-search')));
    tester.view.viewInsets = const FakeViewPadding(bottom: 220);
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
    expect(
      find.byKey(const Key('settings-search')).hitTestable(),
      findsOneWidget,
    );
    await tester.enterText(
      find.byKey(const Key('settings-search')),
      'saved notes',
    );
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
    final close = find.byKey(const Key('settings-close'));
    expect(close.hitTestable(), findsOneWidget);
    expect(tester.getBottomRight(close).dy, lessThanOrEqualTo(260));
    await tester.tap(close);
    await tester.pumpAndSettle();
    expect(capture.picked, isNull);
    expect(find.byType(SettingsHub), findsNothing);
  });

  testWidgets('320x480 at 2x text Close stays reachable', (tester) async {
    tester.view.physicalSize = const Size(320, 480);
    tester.view.devicePixelRatio = 1;
    tester.platformDispatcher.textScaleFactorTestValue = 2;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
    final capture = await _openHub(
      tester,
      size: const Size(320, 480),
      textScale: 2,
    );
    addTearDown(() => _closeHarness(tester));
    await tester.ensureVisible(find.byKey(const Key('settings-close')));
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
    expect(
      find.byKey(const Key('settings-close')).hitTestable(),
      findsOneWidget,
    );
    await tester.tap(find.byKey(const Key('settings-close')));
    await tester.pumpAndSettle();
    expect(find.byType(SettingsHub), findsNothing);
    expect(capture.picked, isNull);
  });
}
