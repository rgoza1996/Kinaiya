import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/capture_modes.dart';
import 'package:kinaiya_flutter/api/capture_modes_client.dart';
import 'package:kinaiya_flutter/state/capture_mode_writes.dart';
import 'package:kinaiya_flutter/ui/voice_capture_mode.dart';

import 'capture_modes_test.dart' show nativeCaptureFixture;

void main() {
  for (final lostAck in [false, true]) {
    testWidgets(
      'saved mode and current capture stay distinct, lostAck=$lostAck',
      (tester) async {
        if (lostAck) {
          tester.view.physicalSize = const Size(320, 480);
          tester.view.devicePixelRatio = 1;
          tester.platformDispatcher.textScaleFactorTestValue = 2;
          addTearDown(tester.view.resetPhysicalSize);
          addTearDown(tester.view.resetDevicePixelRatio);
          addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
        }
        final fixture = nativeCaptureFixture();
        var snapshot = fixture['snapshot'] as Map<String, Object?>;
        Map<String, Object?>? receipt;
        CaptureModeChange? original;
        var writes = 0, lookups = 0;
        final registry = CaptureModeWrites();
        final api = CaptureModeApi(
          baseUrl: 'https://converse.invalid',
          ownerToken: 'public-capture-widget',
          clientFactory: () => MockClient((request) async {
            expect(request.url.host, 'converse.invalid');
            expect(
              request.headers['authorization'],
              'Bearer public-capture-widget',
            );
            if (request.method == 'GET') {
              return http.Response(
                jsonEncode(snapshot),
                200,
                headers: {'content-type': 'application/json'},
              );
            }
            final body = jsonDecode(request.body) as Map<String, Object?>;
            if (request.url.path.endsWith('/lookup')) {
              lookups++;
              expect(body, original!.body);
              return http.Response(
                jsonEncode({
                  ...fixture['lookup_saved'] as Map<String, Object?>,
                  'request_sha256': original!.digest,
                  'receipt': receipt,
                }),
                200,
                headers: {'content-type': 'application/json'},
              );
            }
            writes++;
            original = CaptureModeChange.fromJson(body);
            expect(original!.selection.mode, CaptureMode.legacySilence);
            receipt = {
              ...fixture['receipt'] as Map<String, Object?>,
              'idempotency_key': original!.key,
            };
            snapshot = {
              ...snapshot,
              'revision': 1,
              'state_token': 'e' * 64,
              'selection': original!.selection.json,
              'selected_mode': 'legacy_silence',
            };
            if (lostAck) throw http.ClientException('PUBLIC-ACK-LOSS');
            return http.Response(
              jsonEncode(receipt),
              200,
              headers: {'content-type': 'application/json'},
            );
          }),
        );
        await tester.pumpWidget(
          MaterialApp(
            home: Scaffold(
              body: SingleChildScrollView(
                child: VoiceCaptureModeOptions(
                  api: api,
                  active: () => true,
                  capturing: true,
                  currentMode: CaptureMode.rollingWindows,
                  writes: registry,
                ),
              ),
            ),
          ),
        );
        await tester.pumpAndSettle();
        expect(find.text('Current capture: Rolling Windows'), findsOneWidget);
        expect(
          find.text('Adaptive Live is not available on this host.'),
          findsOneWidget,
        );
        expect(writes, 0);
        await tester.ensureVisible(
          find.byKey(const Key('capture-mode-choice')),
        );
        await tester.pumpAndSettle();
        await tester.tap(find.byKey(const Key('capture-mode-choice')));
        await tester.pumpAndSettle();
        if (find.text('Legacy Silence').evaluate().isEmpty) {
          await tester.scrollUntilVisible(
            find.text('Legacy Silence'),
            120,
            scrollable: find.byType(Scrollable).last,
          );
        }
        await tester.ensureVisible(find.text('Legacy Silence').last);
        await tester.pumpAndSettle();
        await tester.tap(find.text('Legacy Silence').last);
        await tester.pumpAndSettle();
        final save = find.widgetWithText(FilledButton, 'Save Capture Mode');
        await tester.ensureVisible(save);
        await tester.tap(save);
        await tester.pumpAndSettle();
        expect(writes, 1);
        expect(find.text('Current capture: Rolling Windows'), findsOneWidget);
        if (lostAck) {
          expect(tester.widget<FilledButton>(save).onPressed, isNull);
          final lookup = find.text('Check save status');
          await tester.ensureVisible(lookup);
          await tester.tap(lookup);
          await tester.pumpAndSettle();
          expect(lookups, 1);
        }
        expect(writes, 1, reason: 'Recovery must never resend the mutation');
        expect(
          find.textContaining('Selected for next Call: Legacy Silence'),
          findsOneWidget,
        );
        await tester.pumpWidget(const SizedBox.shrink());
        await api.close();
        registry.dispose();
      },
    );
  }

  testWidgets('unconfirmed stop is not presented as an inactive microphone', (
    tester,
  ) async {
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: SingleChildScrollView(
            child: VoiceCaptureModeOptions(
              api: null,
              active: () => false,
              capturing: true,
              currentMode: CaptureMode.rollingWindows,
              stopUnconfirmed: true,
            ),
          ),
        ),
      ),
    );
    expect(find.text('Microphone stop: Unconfirmed'), findsOneWidget);
    expect(find.text('Current Call: Inactive'), findsNothing);
    await tester.pumpWidget(const SizedBox.shrink());
  });
}
