import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/agent_chat.dart';
import 'package:kinaiya_flutter/ui/character_view.dart';
import 'package:kinaiya_flutter/ui/converse_interaction.dart';
import 'package:kinaiya_flutter/ui/moonlit_home.dart';
import 'package:kinaiya_flutter/ui/presentation_settings.dart';

import 'settings_navigation_test.dart' show NavigationFixture, routePump;

Future<void> _size(WidgetTester tester, Size size, {double scale = 1}) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1;
  tester.platformDispatcher.textScaleFactorTestValue = scale;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
}

Future<void> _navigate(WidgetTester tester, String page) async {
  final target = find.byKey(ValueKey('navigation-$page'));
  if (target.hitTestable().evaluate().isEmpty) {
    await tester.tap(find.byKey(const ValueKey('navigation-menu')));
    await routePump(tester);
  }
  if (target.evaluate().isEmpty) {
    await tester.scrollUntilVisible(
      target,
      120,
      scrollable: find
          .descendant(
            of: find.byType(Drawer),
            matching: find.byType(Scrollable),
          )
          .first,
    );
  }
  await tester.ensureVisible(target);
  await tester.pump();
  expect(target.hitTestable(), findsOneWidget);
  await tester.tap(target);
  await routePump(tester);
}

Finder _liveMicrophoneStatus() => find.descendant(
  of: find
      .ancestor(
        of: find.widgetWithText(TextButton, 'Stop microphone'),
        matching: find.byType(Wrap),
      )
      .first,
  matching: find.text('Live microphone on'),
);

void main() {
  testWidgets('desktop sidebar has all concept destinations and can expand', (
    tester,
  ) async {
    await _size(tester, const Size(1440, 1000));
    final fixture = NavigationFixture();
    await fixture.mount(tester);
    addTearDown(() => fixture.dispose(tester));
    var rail = tester.widget<NavigationRail>(find.byType(NavigationRail));
    expect(rail.destinations, hasLength(8));
    expect(rail.extended, isFalse);
    for (final page in [
      'home',
      'converse',
      'agent',
      'decisions',
      'projects',
      'files',
      'media',
      'settings',
    ]) {
      expect(find.byKey(ValueKey('navigation-$page')), findsOneWidget);
    }
    await tester.tap(find.byKey(const ValueKey('navigation-expand')));
    await routePump(tester);
    rail = tester.widget<NavigationRail>(find.byType(NavigationRail));
    expect(rail.extended, isTrue);
    expect(find.byTooltip('Collapse navigation'), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey('navigation-expand')));
    await routePump(tester);
    expect(
      tester.widget<NavigationRail>(find.byType(NavigationRail)).extended,
      isFalse,
    );
    expect(fixture.dispatchedEffect, isFalse);
    expect(tester.takeException(), isNull);
  });

  testWidgets('narrow navigation uses the drawer and keeps both chat routes', (
    tester,
  ) async {
    await _size(tester, const Size(390, 844));
    final fixture = NavigationFixture();
    await fixture.mount(tester);
    addTearDown(() => fixture.dispose(tester));
    expect(find.byType(NavigationBar), findsNothing);
    expect(find.byType(NavigationRail), findsNothing);
    expect(find.byTooltip('Open navigation').hitTestable(), findsOneWidget);
    await _navigate(tester, 'agent');
    expect(fixture.state.view, AppView.agent);
    expect(find.byKey(const ValueKey('agent-input')), findsOneWidget);
    await _navigate(tester, 'converse');
    expect(fixture.state.view, AppView.character);
    expect(find.byType(ConverseInteraction), findsOneWidget);
    expect(find.byKey(const ValueKey('converse-call')), findsOneWidget);
    expect(fixture.state.converseDraft, 'Retained conversation draft');
    expect(fixture.state.agentDraft, 'Retained Agent draft');
    expect(fixture.dispatchedEffect, isFalse);
    expect(tester.takeException(), isNull);
  });

  testWidgets('workspace pages retain mounted panes and armed capture', (
    tester,
  ) async {
    await _size(tester, const Size(1440, 1000));
    final fixture = NavigationFixture();
    await fixture.mount(tester);
    addTearDown(() => fixture.dispose(tester));
    final character = tester.state(find.byType(CharacterView));
    final agent = tester.state(find.byType(AgentChat, skipOffstage: false));
    final session = fixture.state.converseSession;
    await tester.ensureVisible(find.byKey(const ValueKey('call-kinaiya')));
    await tester.tap(find.byKey(const ValueKey('call-kinaiya')));
    await routePump(tester);
    expect(fixture.audio.arms, 1);
    final requests = fixture.requests.length;
    final vad = fixture.audio.vadCalls;
    for (final page in ['home', 'decisions', 'projects', 'files', 'media']) {
      await _navigate(tester, page);
      if (page == 'home') {
        expect(find.byType(MoonlitHome), findsOneWidget);
        expect(find.byKey(const ValueKey('home-setup')), findsOneWidget);
      } else {
        expect(find.byKey(ValueKey('workspace-$page')), findsOneWidget);
        expect(
          find.text(switch (page) {
            'decisions' => 'Trace what Kinaiya considered and decided',
            'projects' =>
              'Project management is not connected to this host yet. Existing Agent sessions remain available in Agent.',
            'files' =>
              'Workspace file browsing is planned. Opening this page does not read files or grant Agent access.',
            _ =>
              'A generated media library is not connected to this host yet. No example files are shown as your media.',
          }),
          findsOneWidget,
        );
      }
      expect(_liveMicrophoneStatus(), findsOneWidget);
      expect(
        identical(
          tester.state(find.byType(CharacterView, skipOffstage: false)),
          character,
        ),
        isTrue,
      );
      expect(
        identical(
          tester.state(find.byType(AgentChat, skipOffstage: false)),
          agent,
        ),
        isTrue,
      );
    }
    await tester.tap(find.byTooltip('Appearance'));
    await routePump(tester);
    expect(find.byType(PresentationSettings), findsOneWidget);
    await tester.tap(find.byTooltip('Close options'));
    await routePump(tester);
    expect(find.byKey(const ValueKey('workspace-media')), findsOneWidget);
    expect(_liveMicrophoneStatus(), findsOneWidget);
    expect(
      identical(
        tester.state(find.byType(CharacterView, skipOffstage: false)),
        character,
      ),
      isTrue,
    );
    expect(
      identical(
        tester.state(find.byType(AgentChat, skipOffstage: false)),
        agent,
      ),
      isTrue,
    );
    await tester.tap(find.text('Open Agent Chat'));
    await routePump(tester);
    expect(fixture.state.view, AppView.agent);
    expect(find.byKey(const ValueKey('agent-input')), findsOneWidget);
    await _navigate(tester, 'converse');
    expect(identical(fixture.state.converseSession, session), isTrue);
    expect(fixture.state.converseDraft, 'Retained conversation draft');
    expect(fixture.state.agentDraft, 'Retained Agent draft');
    expect(fixture.requests.skip(requests), everyElement(startsWith('GET ')));
    expect(fixture.dispatchedEffect, isFalse);
    expect(fixture.audio.arms, 1);
    expect(fixture.audio.armStops, 0);
    expect(fixture.audio.vadCalls, vad);
    expect(fixture.audio.disposals, 0);
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    'Home setup and connected sidebar preserve navigation through Options',
    (tester) async {
      await _size(tester, const Size(1440, 1000));
      final fixture = NavigationFixture();
      await fixture.mount(tester);
      addTearDown(() => fixture.dispose(tester));
      await _navigate(tester, 'home');
      expect(find.byType(MoonlitHome), findsOneWidget);
      await tester.ensureVisible(find.byKey(const ValueKey('home-setup')));
      await tester.tap(find.byKey(const ValueKey('home-setup')));
      await routePump(tester);
      await tester.tap(find.byKey(const ValueKey('setup-bubble-setup')));
      await routePump(tester);
      expect(find.text('Kinaiya setup'), findsOneWidget);
      await tester.tap(find.byTooltip('Close settings popover'));
      await routePump(tester);
      await tester.tap(find.byKey(const ValueKey('setup-bubble-view')));
      await routePump(tester);
      await tester.ensureVisible(find.text('Cancel'));
      await tester.pump();
      expect(find.text('Cancel').hitTestable(), findsOneWidget);
      await tester.tap(find.text('Cancel'));
      await routePump(tester);
      expect(find.byKey(const ValueKey('call-kinaiya')), findsOneWidget);
      await _navigate(tester, 'converse');
      expect(find.byType(ConverseInteraction), findsOneWidget);
      expect(find.byKey(const ValueKey('converse-call')), findsOneWidget);
      await _navigate(tester, 'settings');
      expect(find.byKey(const ValueKey('settings-search')), findsOneWidget);
      await tester.tap(find.byKey(const ValueKey('settings-close')));
      await routePump(tester);
      expect(find.byKey(const ValueKey('converse-call')), findsOneWidget);
      expect(fixture.dispatchedEffect, isFalse);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    '320px enlarged drawer scrolls to Settings and remains closable',
    (tester) async {
      await _size(tester, const Size(320, 480), scale: 2);
      final fixture = NavigationFixture();
      await fixture.mount(tester);
      addTearDown(() => fixture.dispose(tester));
      addTearDown(tester.view.resetViewInsets);
      await _navigate(tester, 'home');
      tester.view.viewInsets = const FakeViewPadding(bottom: 220);
      await routePump(tester);
      expect(
        find.byKey(const ValueKey('navigation-menu')).hitTestable(),
        findsOneWidget,
      );
      await _navigate(tester, 'settings');
      expect(
        find.byKey(const ValueKey('settings-close')).hitTestable(),
        findsOneWidget,
      );
      await tester.tap(find.byKey(const ValueKey('settings-close')));
      await routePump(tester);
      expect(find.byType(MoonlitHome), findsOneWidget);
      expect(find.byKey(const ValueKey('home-setup')), findsOneWidget);
      expect(fixture.dispatchedEffect, isFalse);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('Converse chat header opens support tools without dispatch', (
    tester,
  ) async {
    await _size(tester, const Size(390, 844));
    final fixture = NavigationFixture();
    await fixture.mount(tester);
    addTearDown(() => fixture.dispose(tester));
    final requests = fixture.requests.length;
    await tester.tap(find.byTooltip('Converse chat'));
    await routePump(tester);
    expect(find.byTooltip('Close Converse chat'), findsOneWidget);
    expect(find.byKey(const ValueKey('converse-input')), findsOneWidget);
    expect(fixture.requests.length, requests);
    expect(fixture.audio.arms, 0);
    expect(fixture.audio.recordStarts, 0);
    await tester.tap(find.byTooltip('Close Converse chat'));
    await routePump(tester);
    expect(find.byKey(const ValueKey('call-kinaiya')), findsOneWidget);
    expect(fixture.state.converseDraft, 'Retained conversation draft');
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    'a retired header callback cannot open a replacement owner chat',
    (tester) async {
      await _size(tester, const Size(390, 844));
      final first = NavigationFixture();
      await first.mount(tester);
      final callback = tester
          .widget<IconButton>(find.byKey(const ValueKey('converse-chat-open')))
          .onPressed!;
      await first.dispose(tester);
      final second = NavigationFixture();
      await second.mount(tester);
      addTearDown(() => second.dispose(tester));
      callback();
      await routePump(tester);
      expect(find.byTooltip('Close Converse chat'), findsNothing);
      expect(second.state.converseDraft, 'Retained conversation draft');
      expect(second.dispatchedEffect, isFalse);
      expect(tester.takeException(), isNull);
    },
  );
}
