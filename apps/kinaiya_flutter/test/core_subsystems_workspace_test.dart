import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/config/options_navigation.dart';
import 'package:kinaiya_flutter/presentation/controller.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/options_workspace.dart';
import 'package:kinaiya_flutter/ui/settings_controls.dart';

import 'core_subsystems_test.dart' show nativeCoreFixture;

void main() {
  testWidgets(
    'actual narrow Options shell keeps Core available without Agent',
    (tester) async {
      tester.view.physicalSize = const Size(320, 480);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final sent = <Uri>[];
      final api = KinaiyaClient(
        sessionBase: 'http://127.0.0.1:8760',
        agentBase: 'http://127.0.0.1:8761',
        converseBase: 'http://127.0.0.1:8762',
        ownerToken: 'public-core-only-shell',
        httpClient: MockClient(
          (_) async => http.Response('Agent unavailable', 503),
        ),
        coreSubsystemClientFactory: () => MockClient((request) async {
          sent.add(request.url);
          return http.Response(
            jsonEncode(nativeCoreFixture()['snapshot']),
            200,
            headers: {'content-type': 'application/json'},
          );
        }),
      );
      final state = AppState()..client = api;
      final presentation = PresentationController();
      final controls = ConverseViewControls();
      await tester.pumpWidget(
        MaterialApp(
          builder: (context, child) => MediaQuery(
            data: MediaQuery.of(
              context,
            ).copyWith(textScaler: const TextScaler.linear(2)),
            child: child!,
          ),
          home: OptionsWorkspace(
            state: state,
            presentation: presentation,
            converseControls: controls,
            initialPage: OptionsPage.subsystems,
            onClose: () {},
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.text('Running services: Not observed'), findsOneWidget);
      expect(tester.takeException(), isNull);
      await tester.ensureVisible(find.text('Save for next launch'));
      expect(tester.takeException(), isNull);
      expect(sent, isNotEmpty);
      expect(sent.every((uri) => uri.port == 8760), isTrue);
      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
      presentation.dispose();
    },
  );
}
