import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/agent_activity.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/config/options_navigation.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/design_tokens.dart';

import 'settings_navigation_test.dart'
    show NavigationFixture, chooseSettings, openSettings, routePump;

Future<void> setSize(WidgetTester tester, Size size, {double scale = 1}) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1;
  tester.platformDispatcher.textScaleFactorTestValue = scale;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
}

void expectFrameUsesTheme(WidgetTester tester, String key) {
  final frame = find.byKey(ValueKey(key));
  expect(frame, findsOneWidget);
  final decoration = tester.widget<DecoratedBox>(
    find.descendant(of: frame, matching: find.byType(DecoratedBox)).first,
  );
  final border = (decoration.decoration as BoxDecoration).border! as Border;
  final outline = Theme.of(tester.element(frame)).colorScheme.outlineVariant;
  expect([
    border.top.color,
    border.right.color,
    border.bottom.color,
    border.left.color,
  ], everyElement(outline));
  expect(border.top.width, 1);
}

void main() {
  testWidgets('recorded tool groups disclose literal read-only shell output', (
    tester,
  ) async {
    await setSize(tester, const Size(1800, 1000));
    final fixture = NavigationFixture();
    await fixture.mount(tester);
    addTearDown(() => fixture.dispose(tester));
    await tester.tap(find.byKey(const ValueKey('navigation-agent')));
    await routePump(tester);
    fixture.state.agentHistory.addAll([
      ChatMsg(role: 'user', text: 'Synthetic prompt', eventId: 'prompt'),
      ChatMsg(
        role: 'tool',
        text: '',
        eventId: 'shell-1',
        toolActivity: const AgentToolActivity(
          eventId: 'shell-1',
          taskId: 'task-a',
          tool: 'shell',
          status: 'Recorded success',
          revision: 1,
          exitCode: 0,
          summary: 'shell exit=0: synthetic command',
          output: '<literal> output',
        ),
      ),
      ChatMsg(
        role: 'tool',
        text: '',
        eventId: 'read-1',
        toolActivity: const AgentToolActivity(
          eventId: 'read-1',
          taskId: 'task-a',
          tool: 'read_file',
          status: 'Recorded success',
          revision: 1,
        ),
      ),
      ChatMsg(role: 'kinaiya', text: 'Recorded response', eventId: 'answer'),
    ]);
    fixture.state.touch();
    await tester.pump();
    expect(find.text('Tool activity · 2 records'), findsOneWidget);
    expect(find.text('<literal> output'), findsNothing);
    await tester.tap(find.text('Tool activity · 2 records'));
    await tester.pumpAndSettle();
    expect(find.text('Recorded success'), findsNWidgets(2));
    expect(find.text('shell exit=0: synthetic command'), findsOneWidget);
    expect(find.text('<literal> output'), findsOneWidget);
    expect(find.byType(SelectableText), findsWidgets);
    expect(find.text('Exit code 0'), findsOneWidget);
    fixture.state.agentHistory.insert(
      3,
      ChatMsg(
        role: 'tool',
        text: '',
        eventId: 'shell-2',
        toolActivity: const AgentToolActivity(
          eventId: 'shell-2',
          taskId: 'task-a',
          tool: 'shell',
          status: 'Recorded failure',
          revision: 1,
          exitCode: 1,
        ),
      ),
    );
    fixture.state.touch();
    await tester.pump();
    expect(find.text('Tool activity · 3 records'), findsOneWidget);
    expect(find.text('<literal> output'), findsOneWidget);
    expect(find.text('Recorded failure'), findsOneWidget);
    await tester.tap(find.text('Tool activity · 3 records'));
    await tester.pumpAndSettle();
    expect(find.text('<literal> output'), findsNothing);
    expect(fixture.dispatchedEffect, isFalse);
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    'conversation rail jumps between loaded, variable-height messages',
    (tester) async {
      await setSize(tester, const Size(1800, 1000));
      final fixture = NavigationFixture();
      await fixture.mount(tester);
      addTearDown(() => fixture.dispose(tester));
      await tester.tap(find.byKey(const ValueKey('navigation-agent')));
      await routePump(tester);
      fixture.state.agentHistory.addAll([
        for (var i = 0; i < 80; i++)
          ChatMsg(
            role: i.isEven ? 'user' : 'kinaiya',
            text:
                'Message $i ${List.filled(i % 5, 'long synthetic content ').join()}',
            eventId: 'event-$i',
          ),
      ]);
      fixture.state.touch();
      await tester.pump();
      final rail = find.byKey(const ValueKey('agent-history-rail'));
      final railScroll = find.descendant(
        of: rail,
        matching: find.byType(Scrollable),
      );
      final last = find.byTooltip('Jump to Agent message 80 of 80');
      await tester.scrollUntilVisible(last, 240, scrollable: railScroll);
      await tester.tap(last);
      await tester.pumpAndSettle();
      final lastMessage = find.text(
        'Message 79 ${List.filled(4, 'long synthetic content ').join()}',
      );
      expect(lastMessage, findsOneWidget);
      final viewport = tester.getRect(
        find.byKey(const ValueKey('agent-history-scroll')),
      );
      expect(viewport.overlaps(tester.getRect(lastMessage)), isTrue);
      final first = find.byTooltip('Jump to your message 1 of 80');
      await tester.scrollUntilVisible(first, -240, scrollable: railScroll);
      await tester.tap(first);
      await tester.pumpAndSettle();
      expect(
        viewport.overlaps(tester.getRect(find.text('Message 0 '))),
        isTrue,
      );
      expect(fixture.dispatchedEffect, isFalse);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('conversation rail can return from task cards below history', (
    tester,
  ) async {
    await setSize(tester, const Size(1800, 1000));
    final fixture = NavigationFixture();
    await fixture.mount(tester);
    addTearDown(() => fixture.dispose(tester));
    await tester.tap(find.byKey(const ValueKey('navigation-agent')));
    await routePump(tester);
    fixture.state.agentSession = SessionInfo(
      sessionId: 'synthetic-session',
      threadId: 'synthetic-thread',
      channel: 'agent',
      epoch: 1,
      name: 'Synthetic',
    );
    fixture.state.tasks = [
      for (var i = 0; i < 15; i++)
        AgentTask(
          taskId: 'task-$i',
          sessionId: 'synthetic-session',
          threadId: 'synthetic-thread',
          epoch: 1,
          objective: 'Synthetic task $i',
          objectiveRevision: 1,
          state: 'succeeded',
          idempotencyKey: 'key-$i',
        ),
    ];
    fixture.state.agentHistory.addAll([
      ChatMsg(role: 'user', text: 'First recorded message', eventId: 'first'),
      ChatMsg(role: 'kinaiya', text: 'Recorded answer', eventId: 'answer'),
    ]);
    fixture.state.touch();
    await tester.pump();
    final history = find.byKey(const ValueKey('agent-history-scroll'));
    await tester.scrollUntilVisible(
      find.text('Synthetic task 0'),
      300,
      scrollable: find
          .descendant(of: history, matching: find.byType(Scrollable))
          .first,
    );
    await tester.tap(find.byTooltip('Jump to your message 1 of 2'));
    await tester.pumpAndSettle();
    expect(
      tester
          .getRect(history)
          .overlaps(tester.getRect(find.text('First recorded message'))),
      isTrue,
    );
    expect(tester.takeException(), isNull);
  });

  testWidgets('Agent notices and composer are outlined and remain usable', (
    tester,
  ) async {
    await setSize(tester, const Size(1800, 1000));
    final fixture = NavigationFixture();
    await fixture.mount(tester);
    addTearDown(() => fixture.dispose(tester));
    await tester.tap(find.byKey(const ValueKey('navigation-agent')));
    await routePump(tester);
    fixture.state.commandsAvailable = false;
    fixture.state.agentNotice = 'Synthetic notice';
    fixture.state.touch();
    await tester.pump();

    final composer = find.byKey(const ValueKey('agent-composer-surface'));
    final decoration =
        tester.widget<Container>(composer).decoration! as BoxDecoration;
    final border = decoration.border! as Border;
    final outline = Theme.of(
      tester.element(composer),
    ).colorScheme.outlineVariant;
    expect([
      border.top.color,
      border.right.color,
      border.bottom.color,
      border.left.color,
    ], everyElement(outline));
    for (final key in ['agent-registry-notice', 'agent-message-notice']) {
      final card = tester.widget<Card>(find.byKey(ValueKey(key)));
      expect((card.shape! as RoundedRectangleBorder).side.color, outline);
    }
    await tester.enterText(
      find.byKey(const ValueKey('agent-input')),
      'Keep this draft',
    );
    await tester.tap(find.text('DISMISS'));
    await tester.pump();
    expect(fixture.state.agentDraft, 'Keep this draft');
    expect(fixture.state.agentNotice, isEmpty);
    expect(find.byKey(const ValueKey('agent-registry-notice')), findsOneWidget);
    expect(tester.takeException(), isNull);
    expect(fixture.dispatchedEffect, isFalse);
  });

  testWidgets(
    'Connection fields have room and remain usable on narrow screens',
    (tester) async {
      await setSize(tester, const Size(1600, 1000));
      final fixture = NavigationFixture();
      await fixture.mount(tester);
      addTearDown(() => fixture.dispose(tester));
      await openSettings(tester);
      await chooseSettings(tester, OptionsPage.connection);

      Finder field(String label) => find.byWidgetPredicate(
        (widget) =>
            widget is TextField && widget.decoration?.labelText == label,
      );
      final fields = [
        'Session service URL',
        'Agent service URL',
        'Converse service URL',
        'Decisions service URL (optional)',
        'Owner access token',
      ].map(field).toList();
      for (var i = 0; i < fields.length - 1; i++) {
        expect(
          tester.getTopLeft(fields[i + 1]).dy -
              tester.getBottomLeft(fields[i]).dy,
          greaterThanOrEqualTo(16),
        );
      }

      await tester.enterText(fields.first, 'http://127.0.0.1:8760');
      tester.view.physicalSize = const Size(320, 640);
      await routePump(tester);
      await tester.ensureVisible(fields.last);
      await tester.enterText(fields.last, 'synthetic-token');
      expect(
        tester.widget<TextField>(fields.first).controller!.text,
        'http://127.0.0.1:8760',
      );
      expect(
        tester.widget<TextField>(fields.last).controller!.text,
        'synthetic-token',
      );
      final disclosure = find.byType(DropdownButtonFormField<BackendProfile>);
      await tester.ensureVisible(disclosure);
      await tester.tap(disclosure);
      await tester.pumpAndSettle();
      await tester.tap(find.text('Allow Meta Contributor (development)').last);
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
      expect(fixture.dispatchedEffect, isFalse);
    },
  );

  testWidgets('desktop workspace boundaries stay aligned across navigation', (
    tester,
  ) async {
    await setSize(tester, const Size(1800, 1000));
    final fixture = NavigationFixture();
    await fixture.mount(tester);
    addTearDown(() => fixture.dispose(tester));
    final divider = find.byKey(const ValueKey('shell-header-divider'));
    final originalDivider = tester.getRect(divider);
    final rail = tester.widget<Container>(
      find.byKey(const ValueKey('navigation-rail-boundary')),
    );
    final railBorder =
        (rail.foregroundDecoration! as BoxDecoration).border! as Border;
    expect(
      railBorder.right.color,
      Theme.of(tester.element(divider)).colorScheme.outlineVariant,
    );

    for (final page in [
      'agent',
      'decisions',
      'projects',
      'files',
      'media',
      'settings',
    ]) {
      await tester.tap(find.byKey(ValueKey('navigation-$page')));
      await routePump(tester);
      expect(tester.getRect(divider), originalDivider, reason: page);
      expect(tester.takeException(), isNull, reason: page);
      if (page == 'agent') {
        expectFrameUsesTheme(tester, 'agent-workspace-frame');
        final harness = find.byKey(const ValueKey('agent-harness-sidebar'));
        final sessions = find.byKey(const ValueKey('agent-session-sidebar'));
        final details = find.byKey(const ValueKey('agent-details-panel'));
        expect(tester.getTopLeft(harness).dy, tester.getTopLeft(sessions).dy);
        expect(tester.getTopLeft(harness).dy, tester.getTopLeft(details).dy);
        await tester.tap(find.byTooltip('Collapse details'));
        await tester.pump();
        expect(find.byKey(const ValueKey('agent-details-panel')), findsNothing);
        expect(
          find.byKey(const ValueKey('agent-details-collapsed')),
          findsOneWidget,
        );
        await tester.tap(find.byTooltip('Expand details'));
        await tester.pump();
        expect(
          find.byKey(const ValueKey('agent-details-panel')),
          findsOneWidget,
        );
      }
      if (page == 'settings') {
        expectFrameUsesTheme(tester, 'options-workspace-frame');
        expect(
          tester
              .getTopLeft(
                find.byKey(const ValueKey('options-sidebar-heading-divider')),
              )
              .dy,
          tester
              .getTopLeft(
                find.byKey(const ValueKey('options-content-heading-divider')),
              )
              .dy,
        );
        expect(find.text('Choose a category to get started.'), findsNothing);
        final artwork = find.byKey(const ValueKey('options-overview-artwork'));
        expect(tester.widget<Image>(artwork).fit, BoxFit.contain);
        final originalSize = tester.getSize(artwork);
        expect(originalSize.width, greaterThan(1000));
        expect(originalSize.height, greaterThan(700));
        tester.view.physicalSize = const Size(1200, 700);
        await routePump(tester);
        expect(tester.getSize(artwork).width, lessThan(originalSize.width));
        expect(tester.getSize(artwork).height, lessThan(originalSize.height));
        expect(tester.widget<Image>(artwork).fit, BoxFit.contain);
        expect(tester.takeException(), isNull);
        await fixture.presentation.setPreferences(
          fixture.presentation.preferences.copyWith(showCharacter: false),
        );
        await routePump(tester);
        expect(artwork, findsNothing);
      }
    }
    expect(fixture.dispatchedEffect, isFalse);
  });

  testWidgets('ultrawide Agent chat uses the available conversation width', (
    tester,
  ) async {
    await setSize(tester, const Size(2520, 1080));
    final fixture = NavigationFixture();
    await fixture.mount(tester);
    addTearDown(() => fixture.dispose(tester));
    await tester.tap(find.byKey(const ValueKey('navigation-agent')));
    await routePump(tester);
    fixture.state.commandsAvailable = false;
    fixture.state.touch();
    await tester.pump();

    final conversation = tester.getRect(
      find.byKey(const ValueKey('agent-conversation')),
    );
    final composer = tester.getRect(
      find
          .descendant(
            of: find.byKey(const ValueKey('agent-composer-surface')),
            matching: find.byType(DecoratedBox),
          )
          .first,
    );
    final notice = tester.getRect(
      find.descendant(
        of: find.byKey(const ValueKey('agent-registry-notice')),
        matching: find.byType(MaterialBanner),
      ),
    );
    expect(conversation.width, greaterThan(1120));
    expect(composer.left, closeTo(conversation.left + 16, .1));
    expect(composer.right, closeTo(conversation.right - 16, .1));
    expect(notice.left, closeTo(composer.left, .1));
    expect(notice.right, closeTo(composer.right, .1));
    expect(tester.takeException(), isNull);
    expect(fixture.dispatchedEffect, isFalse);
  });

  testWidgets('details can resize, collapse, and adapt to viewport changes', (
    tester,
  ) async {
    await setSize(tester, const Size(2520, 1080));
    final fixture = NavigationFixture();
    await fixture.mount(tester);
    addTearDown(() => fixture.dispose(tester));
    await tester.tap(find.byKey(const ValueKey('navigation-agent')));
    await routePump(tester);
    await tester.enterText(find.byKey(const ValueKey('agent-input')), 'Draft');

    final details = find.byKey(const ValueKey('agent-details-panel'));
    final handle = find.byKey(const ValueKey('agent-details-resize-handle'));
    expect(tester.getSize(details).width, 300);
    await tester.drag(handle, const Offset(-220, 0));
    await tester.pump();
    final draggedWidth = tester.getSize(details).width;
    expect(draggedWidth, greaterThan(490));

    final gesture = find
        .descendant(of: handle, matching: find.byType(GestureDetector))
        .first;
    Focus.of(tester.element(gesture)).requestFocus();
    await tester.pump();
    await tester.sendKeyEvent(LogicalKeyboardKey.arrowLeft);
    await tester.pump();
    expect(tester.getSize(details).width, closeTo(draggedWidth + 24, .1));
    final savedWidth = tester.getSize(details).width;

    tester.view.physicalSize = const Size(1500, 1000);
    await tester.pump();
    expect(
      tester.getSize(find.byKey(const ValueKey('agent-conversation'))).width,
      greaterThanOrEqualTo(440),
    );
    expect(tester.getSize(details).width, lessThan(savedWidth));
    tester.view.physicalSize = const Size(2520, 1080);
    await tester.pump();
    expect(tester.getSize(details).width, closeTo(savedWidth, .1));

    await tester.tap(find.byTooltip('Collapse details'));
    await tester.pump();
    expect(
      find.byKey(const ValueKey('agent-details-collapsed')),
      findsOneWidget,
    );
    expect(find.byIcon(Icons.chevron_left), findsWidgets);
    expect(fixture.state.agentDraft, 'Draft');
    await tester.tap(find.byTooltip('Expand details'));
    await tester.pump();
    expect(tester.getSize(details).width, closeTo(savedWidth, .1));
    expect(fixture.state.agentDraft, 'Draft');
    expect(tester.takeException(), isNull);
    expect(fixture.dispatchedEffect, isFalse);
  });

  testWidgets(
    'each sidebar resizes and snaps closed without losing its width',
    (tester) async {
      final semantics = tester.ensureSemantics();
      await setSize(tester, const Size(2520, 1080));
      final fixture = NavigationFixture();
      await fixture.mount(tester);
      addTearDown(() => fixture.dispose(tester));
      await tester.tap(find.byKey(const ValueKey('navigation-agent')));
      await routePump(tester);
      await tester.enterText(
        find.byKey(const ValueKey('agent-input')),
        'Retained draft',
      );

      final harness = find.byKey(const ValueKey('agent-harness-sidebar'));
      final sessions = find.byKey(const ValueKey('agent-session-sidebar'));
      final harnessHandle = find.byKey(
        const ValueKey('agent-harness-resize-handle'),
      );
      final sessionsHandle = find.byKey(
        const ValueKey('agent-sessions-resize-handle'),
      );
      final detailsHandle = find.byKey(
        const ValueKey('agent-details-resize-handle'),
      );
      expect(tester.getSize(harnessHandle).width, 24);
      expect(tester.getSize(sessionsHandle).width, 24);
      expect(tester.getSize(detailsHandle).width, 24);
      final handleSemantics = tester.getSemantics(sessionsHandle);
      expect(handleSemantics.flagsCollection.isEnabled, ui.Tristate.isTrue);
      expect(
        handleSemantics.getSemanticsData().hasAction(
          ui.SemanticsAction.increase,
        ),
        isTrue,
      );
      await tester.enterText(
        find.descendant(of: harness, matching: find.byType(TextField)),
        'historical',
      );

      await tester.drag(harnessHandle, const Offset(100, 0));
      await tester.pump();
      final harnessGesture = find
          .descendant(of: harnessHandle, matching: find.byType(GestureDetector))
          .first;
      Focus.of(tester.element(harnessGesture)).requestFocus();
      await tester.pump();
      final beforeKey = tester.getSize(harness).width;
      await tester.sendKeyEvent(LogicalKeyboardKey.arrowRight);
      await tester.pump();
      final harnessWidth = tester.getSize(harness).width;
      expect(harnessWidth, closeTo(beforeKey + 24, .1));
      expect(harnessWidth, greaterThan(300));
      await tester.drag(sessionsHandle, const Offset(100, 0));
      await tester.pump();
      final sessionsWidth = tester.getSize(sessions).width;
      expect(sessionsWidth, greaterThan(310));

      final sessionsGrip = tester.getRect(sessionsHandle);
      await tester.dragFrom(
        Offset(sessionsGrip.left + 2, sessionsGrip.center.dy),
        const Offset(-500, 0),
      );
      await tester.pump();
      expect(find.byTooltip('Expand sessions'), findsOneWidget);
      expect(find.byTooltip('Collapse harnesses'), findsOneWidget);
      expect(tester.getSize(sessions).width, 48);
      await tester.tap(find.byTooltip('Expand sessions'));
      await tester.pump();
      expect(tester.getSize(sessions).width, closeTo(sessionsWidth, .1));

      await tester.drag(harnessHandle, const Offset(-500, 0));
      await tester.pump();
      expect(find.byTooltip('Expand harnesses'), findsOneWidget);
      expect(find.byTooltip('Collapse sessions'), findsOneWidget);
      await tester.tap(find.byTooltip('Expand harnesses'));
      await tester.pump();
      expect(tester.getSize(harness).width, closeTo(harnessWidth, .1));
      expect(
        tester
            .widget<TextField>(
              find.descendant(of: harness, matching: find.byType(TextField)),
            )
            .controller!
            .text,
        'historical',
      );

      final detailsGrip = tester.getRect(detailsHandle);
      await tester.dragFrom(
        Offset(detailsGrip.right - 2, detailsGrip.center.dy),
        const Offset(400, 0),
      );
      await tester.pump();
      expect(find.byTooltip('Expand details'), findsOneWidget);
      await tester.tap(find.byTooltip('Expand details'));
      await tester.pump();
      expect(
        tester.getSize(find.byKey(const ValueKey('agent-details-panel'))).width,
        300,
      );
      expect(fixture.state.agentDraft, 'Retained draft');
      expect(tester.takeException(), isNull);
      expect(fixture.dispatchedEffect, isFalse);
      semantics.dispose();
    },
  );

  testWidgets('collapsed sidebars retain draggable, outlined dividers', (
    tester,
  ) async {
    final semantics = tester.ensureSemantics();
    await setSize(tester, const Size(2520, 1080));
    final fixture = NavigationFixture();
    await fixture.mount(tester);
    addTearDown(() => fixture.dispose(tester));
    await tester.tap(find.byKey(const ValueKey('navigation-agent')));
    await routePump(tester);
    await tester.enterText(
      find.byKey(const ValueKey('agent-input')),
      'Kept draft',
    );

    final harness = find.byKey(const ValueKey('agent-harness-sidebar'));
    final sessions = find.byKey(const ValueKey('agent-session-sidebar'));
    final harnessHandle = find.byKey(
      const ValueKey('agent-harness-resize-handle'),
    );
    final sessionsHandle = find.byKey(
      const ValueKey('agent-sessions-resize-handle'),
    );
    final detailsHandle = find.byKey(
      const ValueKey('agent-details-resize-handle'),
    );

    void expectWorkspaceBorder() {
      final divider = tester.widget<Container>(
        find
            .descendant(of: sessionsHandle, matching: find.byType(Container))
            .first,
      );
      final border = (divider.decoration! as BoxDecoration).border! as Border;
      expect(border.right.width, 1);
      expect(
        border.right.color,
        Theme.of(tester.element(sessionsHandle)).colorScheme.outlineVariant,
      );
      expect(
        tester.getTopLeft(sessionsHandle).dy,
        tester.getTopLeft(sessions).dy,
      );
      expect(
        tester.getBottomLeft(sessionsHandle).dy,
        tester.getBottomLeft(sessions).dy,
      );
    }

    expectWorkspaceBorder();
    await tester.tap(find.byTooltip('Collapse harnesses'));
    await tester.tap(find.byTooltip('Collapse sessions'));
    await tester.tap(find.byTooltip('Collapse details'));
    await tester.pump();
    for (final handle in [harnessHandle, sessionsHandle, detailsHandle]) {
      expect(handle, findsOneWidget);
      expect(tester.getSize(handle).width, 24);
      expect(tester.getSemantics(handle).getSemanticsData().value, 'Collapsed');
    }
    expectWorkspaceBorder();

    final drag = await tester.startGesture(tester.getCenter(sessionsHandle));
    await drag.moveBy(const Offset(110, 0));
    await tester.pump();
    expect(find.byTooltip('Collapse sessions'), findsOneWidget);
    final initialWidth = tester.getSize(sessions).width;
    await drag.moveBy(const Offset(100, 0));
    await tester.pump();
    expect(tester.getSize(sessions).width, greaterThan(initialWidth));
    await drag.up();
    await tester.pump();

    await tester.drag(harnessHandle, const Offset(250, 0));
    await tester.pump();
    expect(find.byTooltip('Collapse harnesses'), findsOneWidget);
    expect(tester.getSize(harness).width, greaterThanOrEqualTo(184));
    await tester.drag(detailsHandle, const Offset(-250, 0));
    await tester.pump();
    expect(find.byTooltip('Collapse details'), findsOneWidget);
    expect(find.byKey(const ValueKey('agent-details-panel')), findsOneWidget);

    await tester.tap(find.byTooltip('Collapse sessions'));
    await tester.pump();
    final sessionsGesture = find
        .descendant(of: sessionsHandle, matching: find.byType(GestureDetector))
        .first;
    Focus.of(tester.element(sessionsGesture)).requestFocus();
    await tester.pump();
    await tester.sendKeyEvent(LogicalKeyboardKey.arrowRight);
    await tester.pump();
    expect(find.byTooltip('Collapse sessions'), findsOneWidget);
    final startingWidth = tester.getSize(sessions).width;
    final roundTrip = await tester.startGesture(
      tester.getCenter(sessionsHandle),
    );
    await roundTrip.moveBy(Offset(-startingWidth - 50, 0));
    await tester.pump();
    expect(find.byTooltip('Expand sessions'), findsOneWidget);
    await roundTrip.moveBy(const Offset(400, 0));
    await tester.pump();
    expect(find.byTooltip('Collapse sessions'), findsOneWidget);
    await roundTrip.up();
    await tester.pump();
    expect(fixture.state.agentDraft, 'Kept draft');
    expect(tester.takeException(), isNull);
    expect(fixture.dispatchedEffect, isFalse);
    semantics.dispose();
  });

  testWidgets('workspace minimum keeps control labels on one line', (
    tester,
  ) async {
    await setSize(tester, const Size(2520, 1080));
    final fixture = NavigationFixture();
    await fixture.mount(tester);
    addTearDown(() => fixture.dispose(tester));
    await tester.tap(find.byKey(const ValueKey('navigation-agent')));
    await routePump(tester);

    final sessions = find.byKey(const ValueKey('agent-session-sidebar'));
    final handle = find.byKey(const ValueKey('agent-sessions-resize-handle'));
    Future<void> checkAtMinimum() async {
      await tester.drag(handle, const Offset(-110, 0));
      await tester.pump();
      expect(find.byTooltip('Collapse sessions'), findsOneWidget);
      expect(tester.getSize(sessions).width, greaterThanOrEqualTo(224));
      for (final label in ['Commands', 'Connections']) {
        final text = tester.renderObject<RenderParagraph>(
          find.descendant(of: sessions, matching: find.text(label)),
        );
        expect(
          text.getBoxesForSelection(
            TextSelection(baseOffset: 0, extentOffset: label.length),
          ),
          hasLength(1),
          reason: '$label should remain on one line',
        );
      }
      expect(tester.takeException(), isNull);
    }

    await checkAtMinimum();
    tester.platformDispatcher.textScaleFactorTestValue = 2;
    await tester.pump();
    await checkAtMinimum();
    expect(fixture.dispatchedEffect, isFalse);
  });

  testWidgets('workspace borders follow a custom palette', (tester) async {
    await setSize(tester, const Size(1800, 1000));
    final fixture = NavigationFixture();
    await fixture.mount(tester);
    addTearDown(() => fixture.dispose(tester));
    await fixture.presentation.setPreferences(
      fixture.presentation.preferences.copyWith(
        primaryOverride: '#7A2255',
        backgroundOverride: '#FFF4FA',
      ),
    );
    await routePump(tester);
    await tester.tap(find.byKey(const ValueKey('navigation-agent')));
    await routePump(tester);
    expectFrameUsesTheme(tester, 'agent-workspace-frame');
    await tester.tap(find.byKey(const ValueKey('navigation-settings')));
    await routePump(tester);
    expectFrameUsesTheme(tester, 'options-workspace-frame');
    expect(tester.takeException(), isNull);
  });

  testWidgets('narrow Options retains its navigation at double text size', (
    tester,
  ) async {
    await setSize(tester, const Size(320, 640), scale: 2);
    final fixture = NavigationFixture();
    await fixture.mount(tester);
    addTearDown(() => fixture.dispose(tester));
    await tester.tap(find.byKey(const ValueKey('settings-open')));
    await routePump(tester);
    expect(
      tester
          .widget<Image>(find.byKey(const ValueKey('options-overview-artwork')))
          .fit,
      BoxFit.contain,
    );
    expect(
      tester
          .widget<WorkspaceFrame>(
            find.byKey(const ValueKey('options-workspace-frame')),
          )
          .inset,
      isFalse,
    );
    await tester.tap(find.byTooltip('Options navigation'));
    await routePump(tester);
    expect(
      find.byKey(const ValueKey('settings-search')).hitTestable(),
      findsOneWidget,
    );
    expect(tester.takeException(), isNull);
    expect(fixture.dispatchedEffect, isFalse);
  });

  testWidgets('sidebar resizing preserves usable chat through window changes', (
    tester,
  ) async {
    await setSize(tester, const Size(2520, 1080));
    final fixture = NavigationFixture();
    await fixture.mount(tester);
    addTearDown(() => fixture.dispose(tester));
    await tester.tap(find.byKey(const ValueKey('navigation-agent')));
    await routePump(tester);

    await tester.drag(
      find.byKey(const ValueKey('agent-harness-resize-handle')),
      const Offset(2200, 0),
    );
    await tester.pump();
    expect(
      tester.getSize(find.byKey(const ValueKey('agent-conversation'))).width,
      greaterThanOrEqualTo(440),
    );
    expect(tester.takeException(), isNull);

    tester.view.physicalSize = const Size(1500, 900);
    await tester.pump();
    expect(
      tester.getSize(find.byKey(const ValueKey('agent-conversation'))).width,
      greaterThanOrEqualTo(440),
    );
    expect(find.byTooltip('Collapse sessions'), findsOneWidget);
    expect(tester.takeException(), isNull);
    tester.view.physicalSize = const Size(1120, 800);
    await tester.pump();
    expect(tester.takeException(), isNull);
    expect(fixture.dispatchedEffect, isFalse);
  });
}
