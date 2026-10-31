import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/ui/agent_workspace_sidebars.dart';

void main() {
  testWidgets(
    'unapproved metadata stays inspectable; core and initial build invoke nothing',
    (tester) async {
      final selected = <String>[];
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: AgentHarnessSidebar(
              entries: const [
                HarnessSidebarEntry(
                  id: 'registered',
                  name: 'Literal <name>',
                  kind: 'context_selector.v1',
                  description: 'Registered selector only',
                  active: false,
                  selectable: false,
                  eligibility: 'unapproved',
                ),
              ],
              selectedId: null,
              collapsed: false,
              loading: false,
              error: null,
              onToggle: () {},
              onRefresh: () {},
              onSelect: selected.add,
            ),
          ),
        ),
      );
      expect(selected, isEmpty);
      expect(find.text('Kinaiya Core'), findsOneWidget);
      expect(find.text('unapproved'), findsOneWidget);
      await tester.tap(find.byKey(const ValueKey('harness-registered')));
      expect(selected, ['registered']);
      expect(tester.takeException(), isNull);
    },
  );
  testWidgets(
    'session empty error create and load-more controls reflect supplied state',
    (tester) async {
      var creates = 0, refreshes = 0, more = 0;
      Future<void> mount({
        List<SessionSidebarEntry> entries = const [],
        String? error,
        bool hasMore = false,
        bool canCreate = false,
      }) async {
        await tester.pumpWidget(
          MaterialApp(
            home: Scaffold(
              body: AgentSessionSidebar(
                entries: entries,
                harnessLabel: 'Real selector',
                collapsed: false,
                loading: false,
                error: error,
                canCreate: canCreate,
                hasMore: hasMore,
                onToggle: () {},
                onRefresh: () => refreshes++,
                onCreate: () => creates++,
                onLoadMore: () => more++,
                onSelect: (_) {},
                controls: const Text('Existing controls'),
              ),
            ),
          ),
        );
      }

      await mount();
      expect(find.text('No sessions yet.'), findsOneWidget);
      expect(
        tester
            .widget<OutlinedButton>(
              find.widgetWithText(OutlinedButton, 'New session'),
            )
            .onPressed,
        isNull,
      );
      await mount(error: 'Read failed');
      await tester.tap(find.text('Retry'));
      expect(refreshes, 1);
      expect(creates, 0);
      await mount(
        entries: const [
          SessionSidebarEntry(
            id: 'native-session',
            label: 'Real session',
            selected: true,
          ),
        ],
        hasMore: true,
        canCreate: true,
      );
      await tester.tap(find.text('New session'));
      expect(creates, 1);
      await tester.tap(find.text('Load more'));
      expect(more, 1);
      expect(tester.takeException(), isNull);
    },
  );
  testWidgets(
    'long literal names at 320px and doubled text stay bounded and scrollable',
    (tester) async {
      tester.view.devicePixelRatio = 1;
      tester.view.physicalSize = const Size(320, 360);
      tester.platformDispatcher.textScaleFactorTestValue = 2;
      addTearDown(tester.view.resetDevicePixelRatio);
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
      final long = 'Long literal name ' * 24;
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: AgentHarnessSidebar(
              entries: [
                HarnessSidebarEntry(
                  id: 'long',
                  name: long,
                  kind: 'context_selector.v1',
                  description: long,
                  active: false,
                  selectable: false,
                ),
              ],
              selectedId: 'long',
              collapsed: false,
              loading: false,
              error: null,
              onToggle: () {},
              onRefresh: () {},
              onSelect: (_) {},
            ),
          ),
        ),
      );
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: AgentSessionSidebar(
              entries: [
                SessionSidebarEntry(
                  id: 'long',
                  label: long,
                  selected: true,
                  subtitle: long,
                ),
              ],
              harnessLabel: long,
              collapsed: false,
              loading: false,
              error: null,
              canCreate: false,
              hasMore: false,
              onToggle: () {},
              onRefresh: () {},
              onCreate: () {},
              onLoadMore: () {},
              onSelect: (_) {},
              controls: Text(long),
            ),
          ),
        ),
      );
      expect(tester.takeException(), isNull);
      expect(find.byType(SingleChildScrollView), findsWidgets);
    },
  );
}
