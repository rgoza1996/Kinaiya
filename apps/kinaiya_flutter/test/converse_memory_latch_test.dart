import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;

import 'converse_memory_regression_test.dart' as probe;

class Uncertain extends probe.Fixture {
  Uncertain(super.id);
  @override
  Future<http.Response> respond(http.Request request) async {
    if (request.url.path == '/v1/memory/use') {
      writes.add((jsonDecode(request.body) as Map).cast<String, Object?>());
      return http.Response('', 503);
    }
    return super.respond(request);
  }
}

void main() {
  testWidgets(
    'uncertain enable survives equal refresh close and replacement client',
    (tester) async {
      final fixture = Uncertain('independent-uncertain-retention');
      await probe.open(tester, fixture.app());
      probe.select(tester);
      await tester.pump();
      await probe.click(tester, 'converse-memory-use');
      expect(fixture.writes.length, 1);
      await probe.click(tester, 'converse-memory-refresh');
      probe.select(tester);
      await tester.pump();
      expect(
        tester
            .widget<FilledButton>(
              find.byKey(const ValueKey('converse-memory-use')),
            )
            .onPressed,
        isNull,
      );
      await probe.click(tester, 'converse-memory-close');
      await probe.open(tester, fixture.app());
      probe.select(tester);
      await tester.pump();
      expect(
        tester
            .widget<FilledButton>(
              find.byKey(const ValueKey('converse-memory-use')),
            )
            .onPressed,
        isNull,
      );
      expect(
        fixture.writes.length,
        1,
        reason: 'Refresh and reopening never resend the write',
      );
      fixture.generation++;
      await probe.click(tester, 'converse-memory-refresh');
      probe.select(tester);
      await tester.pump();
      expect(
        tester
            .widget<FilledButton>(
              find.byKey(const ValueKey('converse-memory-use')),
            )
            .onPressed,
        isNotNull,
      );
      expect(fixture.writes.length, 1);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('320x480 two-times text remains scrollable and Escape closes', (
    tester,
  ) async {
    tester.view.devicePixelRatio = 1;
    tester.view.physicalSize = const Size(320, 480);
    tester.platformDispatcher.textScaleFactorTestValue = 2;
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
    final fixture = probe.Fixture('independent-small-layout');
    await probe.open(tester, fixture.app());
    expect(find.text('Converse memory'), findsOneWidget);
    await tester.scrollUntilVisible(
      find.byKey(const ValueKey('converse-memory-stop')),
      80,
      scrollable: find.byType(Scrollable).first,
    );
    expect(tester.takeException(), isNull);
    await tester.sendKeyEvent(LogicalKeyboardKey.escape);
    await tester.pumpAndSettle();
    expect(find.text('Converse memory'), findsNothing);
    expect(tester.takeException(), isNull);
    expect(fixture.writes, isEmpty);
  });
}
