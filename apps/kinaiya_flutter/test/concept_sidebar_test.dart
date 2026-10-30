import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/agent_chat.dart';
import 'package:kinaiya_flutter/ui/character_view.dart';

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
      'people',
      'files',
      'documents',
      'gallery',
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
    expect(find.byKey(const ValueKey('call-kinaiya')), findsOneWidget);
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
    for (final page in ['home', 'people', 'files', 'documents', 'gallery']) {
      await _navigate(tester, page);
      expect(find.byKey(ValueKey('workspace-$page')), findsOneWidget);
      if (page != 'home') {
        expect(find.textContaining('planned.'), findsOneWidget);
      }
      expect(find.text('Live microphone on'), findsOneWidget);
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
    'Home offers real navigation and Settings opens existing Options',
    (tester) async {
      await _size(tester, const Size(1440, 1000));
      final fixture = NavigationFixture();
      await fixture.mount(tester);
      addTearDown(() => fixture.dispose(tester));
      await _navigate(tester, 'home');
      await tester.tap(find.text('Open Converse'));
      await routePump(tester);
      expect(find.byKey(const ValueKey('call-kinaiya')), findsOneWidget);
      await _navigate(tester, 'settings');
      expect(find.byKey(const ValueKey('settings-search')), findsOneWidget);
      await tester.tap(find.byKey(const ValueKey('settings-close')));
      await routePump(tester);
      expect(find.byKey(const ValueKey('call-kinaiya')), findsOneWidget);
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
      expect(find.byKey(const ValueKey('workspace-home')), findsOneWidget);
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
