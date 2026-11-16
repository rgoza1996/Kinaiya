import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/core_subsystems.dart';
import 'package:kinaiya_flutter/api/core_subsystems_client.dart';
import 'package:kinaiya_flutter/config/options_navigation.dart';
import 'package:kinaiya_flutter/state/core_subsystem_writes.dart';
import 'package:kinaiya_flutter/ui/options_subsystems.dart';

import 'core_subsystems_test.dart' show nativeCoreFixture;

http.Response reply(Object? data) => http.Response(
  jsonEncode(data),
  200,
  headers: {'content-type': 'application/json', 'cache-control': 'no-store'},
);

Future<void> showCore(
  WidgetTester tester,
  CoreSubsystemApi api,
  CoreSubsystemWrites writes, {
  Size size = const Size(1100, 1300),
  double scale = 1,
}) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  await tester.pumpWidget(
    MaterialApp(
      builder: (context, child) => MediaQuery(
        data: MediaQuery.of(
          context,
        ).copyWith(textScaler: TextScaler.linear(scale)),
        child: child!,
      ),
      home: Scaffold(
        body: SingleChildScrollView(
          child: OptionsSubsystems(
            api: api,
            active: () => true,
            writes: writes,
          ),
        ),
      ),
    ),
  );
  await tester.pumpAndSettle();
}

Future<void> choose(WidgetTester tester, String label) async {
  await tester.tap(find.byKey(const Key('core-subsystem-choice')));
  await tester.pumpAndSettle();
  if (find.text(label).evaluate().isEmpty) {
    await tester.scrollUntilVisible(
      find.text(label),
      120,
      scrollable: find.byType(Scrollable).last,
    );
  }
  await tester.ensureVisible(find.text(label).last);
  await tester.pumpAndSettle();
  await tester.tap(find.text(label).last);
  await tester.pumpAndSettle();
}

void main() {
  test('Core mode selection is a searchable native Options destination', () {
    final found = searchOptionsPages('subsystems');
    expect(found.first.page, OptionsPage.subsystems);
    expect(found.first.category.label, 'Core');
  });

  testWidgets(
    'all modes save through Core without claiming runtime readiness',
    (tester) async {
      final f = nativeCoreFixture(), sent = <http.Request>[];
      var snapshot = f['snapshot'] as Map<String, Object?>;
      final api = CoreSubsystemApi(
        baseUrl: 'http://127.0.0.1:8760',
        ownerToken: 'public-core-ui-owner',
        clientFactory: () => MockClient((request) async {
          sent.add(request);
          if (request.method == 'GET') return reply(snapshot);
          final body = jsonDecode(request.body) as Map<String, Object?>;
          final selection = body['selection'] as Map<String, Object?>;
          final revision = (body['expected_revision'] as int) + 1;
          snapshot = {
            ...snapshot,
            'revision': revision,
            'selection': selection,
            'selected_mode': selection['source'] == 'host'
                ? snapshot['host_mode']
                : selection['mode'],
            'state_token': 'f' * 64,
          };
          return reply({
            ...(f['receipt'] as Map<String, Object?>),
            'idempotency_key': body['idempotency_key'],
            'revision': revision,
            'selection': selection,
            'selected_mode_observed': snapshot['selected_mode'],
          });
        }),
      );
      addTearDown(api.close);
      await showCore(tester, api, CoreSubsystemWrites());
      expect(find.text('Running services: Not observed'), findsOneWidget);
      expect(find.textContaining('Startup declaration: Agent'), findsOneWidget);
      for (final mode in CoreSubsystemMode.values) {
        await choose(tester, mode.label);
        await tester.ensureVisible(find.text('Save for next launch'));
        await tester.tap(find.text('Save for next launch'));
        await tester.pumpAndSettle();
        expect(snapshot['selected_mode'], mode.wire);
        expect(find.text('Running services: Not observed'), findsOneWidget);
        expect(tester.takeException(), isNull);
      }
      await choose(tester, 'Host defaults');
      await tester.tap(find.text('Save for next launch'));
      await tester.pumpAndSettle();
      final posts = sent.where((r) => r.method == 'POST').toList();
      expect(posts.length, 7);
      expect(
        posts.every((r) => r.url.path == '/v1/core/options/subsystems'),
        isTrue,
      );
      expect((jsonDecode(posts.last.body) as Map)['selection'], {
        'source': 'host',
        'mode': null,
      });
      expect(
        sent.every(
          (r) => r.headers['Authorization'] == 'Bearer public-core-ui-owner',
        ),
        isTrue,
      );
      expect(sent.any((r) => r.url.port != 8760), isFalse);
    },
  );

  testWidgets(
    'uncertain original survives navigation and reconnect, lookup never resends it',
    (tester) async {
      final f = nativeCoreFixture(), sent = <http.Request>[];
      final held = Completer<http.Response>(), writes = CoreSubsystemWrites();
      Map<String, Object?>? original;
      var fenced = false;
      http.Client factory() {
        // Exercise legal synchronous acquisition latency under the native budget.
        // A tiny fixture-wide budget can expire before reaching the held save.
        final acquiring = Stopwatch()..start();
        while (acquiring.elapsed < const Duration(milliseconds: 50)) {}
        return MockClient((request) async {
          sent.add(request);
          if (request.method == 'GET') return reply(f['snapshot']);
          final body = jsonDecode(request.body) as Map<String, Object?>;
          if (request.url.path.endsWith('/lookup')) {
            expect(body, original);
            return reply({
              ...(f['lookup_before'] as Map<String, Object?>),
              'request_sha256': CoreSubsystemChange.fromJson(body).digest,
              'mutation_fenced': fenced,
            });
          }
          original = body;
          return held.future;
        });
      }

      final api = CoreSubsystemApi(
        baseUrl: 'http://127.0.0.1:8760',
        ownerToken: 'public-uncertain-core-owner',
        clientFactory: factory,
      );
      addTearDown(api.close);
      await showCore(tester, api, writes);
      await choose(tester, CoreSubsystemMode.all.label);
      await tester.tap(find.text('Save for next launch'));
      await tester.pump();
      expect(
        original,
        isNotNull,
        reason: 'Observe the original admission before advancing its deadline',
      );
      expect(
        sent
            .where((r) => r.method == 'POST' && !r.url.path.endsWith('/lookup'))
            .length,
        1,
      );
      // Expire the existing native ten-second result budget for the held save.
      await tester.pump(const Duration(seconds: 11));
      await tester.pumpAndSettle();
      expect(find.text('Check save status'), findsOneWidget);
      expect(
        tester
            .widget<FilledButton>(
              find.widgetWithText(FilledButton, 'Save for next launch'),
            )
            .onPressed,
        isNull,
      );
      await tester.pumpWidget(const SizedBox.shrink());
      final reconnected = CoreSubsystemApi(
        baseUrl: 'http://127.0.0.1:8760',
        ownerToken: 'public-uncertain-core-owner',
        clientFactory: factory,
      );
      addTearDown(reconnected.close);
      await showCore(tester, reconnected, writes);
      await tester.tap(find.text('Check save status'));
      await tester.pumpAndSettle();
      expect(find.textContaining('could still apply'), findsOneWidget);
      fenced = true;
      await tester.tap(find.text('Check save status'));
      await tester.pumpAndSettle();
      expect(find.textContaining('cannot apply'), findsOneWidget);
      expect(
        sent
            .where((r) => r.method == 'POST' && !r.url.path.endsWith('/lookup'))
            .length,
        1,
      );
      held.complete(reply({'invalid': 'late original'}));
      await tester.pumpAndSettle();
    },
  );

  testWidgets('narrow large-text view preserves status and guarded actions', (
    tester,
  ) async {
    final previousHitTestPolicy = WidgetController.hitTestWarningShouldBeFatal;
    WidgetController.hitTestWarningShouldBeFatal = true;
    addTearDown(
      () =>
          WidgetController.hitTestWarningShouldBeFatal = previousHitTestPolicy,
    );
    final api = CoreSubsystemApi(
      baseUrl: 'http://127.0.0.1:8760',
      ownerToken: 'public-small-core-owner',
      clientFactory: () =>
          MockClient((_) async => reply(nativeCoreFixture()['snapshot'])),
    );
    addTearDown(api.close);
    await showCore(
      tester,
      api,
      CoreSubsystemWrites(),
      size: const Size(320, 480),
      scale: 2,
    );
    expect(tester.takeException(), isNull);
    await tester.ensureVisible(find.text('Save for next launch'));
    expect(tester.takeException(), isNull);
    expect(find.text('Running services: Not observed'), findsOneWidget);
    expect(
      tester
          .widget<FilledButton>(
            find.widgetWithText(FilledButton, 'Save for next launch'),
          )
          .onPressed,
      isNull,
    );
    await tester.ensureVisible(find.byKey(const Key('core-subsystem-choice')));
    await choose(tester, CoreSubsystemMode.all.label);
    expect(
      tester
          .state<FormFieldState<String>>(
            find.byKey(const Key('core-subsystem-choice')),
          )
          .value,
      CoreSubsystemMode.all.wire,
    );
    final field = find.byKey(const Key('core-subsystem-choice'));
    final label = find.descendant(
      of: field,
      matching: find.text(CoreSubsystemMode.all.label),
    );
    final bounds = tester.getRect(field), textBounds = tester.getRect(label);
    expect(
      bounds.top <= textBounds.top && bounds.bottom >= textBounds.bottom,
      isTrue,
    );
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    'changed host observation preserves the draft and explicit reset adopts inspected state',
    (tester) async {
      final f = nativeCoreFixture();
      var snapshot = f['snapshot'] as Map<String, Object?>;
      final api = CoreSubsystemApi(
        baseUrl: 'http://127.0.0.1:8760',
        ownerToken: 'public-host-change-core',
        clientFactory: () => MockClient((_) async => reply(snapshot)),
      );
      addTearDown(api.close);
      await showCore(tester, api, CoreSubsystemWrites());
      await choose(tester, CoreSubsystemMode.all.label);
      snapshot = {
        ...snapshot,
        'host_mode': 'decisions',
        'selected_mode': 'decisions',
        'state_token': 'd' * 64,
      };
      await tester.tap(find.text('Refresh selection'));
      await tester.pumpAndSettle();
      final field = find.byKey(const Key('core-subsystem-choice'));
      expect(
        tester.state<FormFieldState<String>>(field).value,
        CoreSubsystemMode.all.wire,
      );
      expect(
        tester
            .widget<FilledButton>(
              find.widgetWithText(FilledButton, 'Save for next launch'),
            )
            .onPressed,
        isNull,
      );
      await tester.tap(find.text('Use inspected selection'));
      await tester.pumpAndSettle();
      expect(tester.state<FormFieldState<String>>(field).value, 'host');
      expect(tester.takeException(), isNull);
    },
  );
}
