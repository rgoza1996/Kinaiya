import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/memory_models.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'converse_memory_test.dart' as fixture;
import 'converse_memory_regression_test.dart' as helpers;

void main() {
  testWidgets(
    'late runtime A acknowledgment cannot clear runtime B uncertainty',
    (tester) async {
      final binding = MemoryBinding(
        'independent-late-runtime-ack',
        'synthetic-thread',
        1,
      );
      final script = fixture.MemoryScript(binding)
        ..seedTwoActive()
        ..runtimeId = 'runtime-A'
        ..generation = 0;
      final old = Completer<http.Response>();
      script.behavior['/v1/memory/use'] = (body) {
        if (body['expected_runtime_id'] == 'runtime-A') return old.future;
        return Future.value(http.Response('', 503));
      };
      final api = KinaiyaClient.mock(
        sessionBase: 'http://127.0.0.1:19990',
        agentBase: 'http://127.0.0.1:19991',
        converseBase: 'http://127.0.0.1:19992',
        mockTransport: MockClient((_) async => http.Response('', 500)),
        memoryClientFactory: () => MockClient(script.call),
      );
      final app = AppState()
        ..connect(
          ConnectionProfile(
            sessionUrl: api.sessionBase,
            agentUrl: api.agentBase,
            converseUrl: api.converseBase,
            ownerToken: 'public-synthetic-owner',
          ),
          injected: api,
        );
      app.converseSession = SessionInfo(
        sessionId: binding.sessionId,
        threadId: binding.threadId,
        epoch: binding.epoch,
        channel: 'converse',
      );
      addTearDown(app.dispose);
      void select() => tester
          .widget<CheckboxListTile>(
            find.byKey(const ValueKey('memory-select-n-1')),
          )
          .onChanged!(true);
      FilledButton button() => tester.widget<FilledButton>(
        find.byKey(const ValueKey('converse-memory-use')),
      );
      try {
        await helpers.open(tester, app);
        select();
        await tester.pump();
        await helpers.click(tester, 'converse-memory-use');
        expect(
          script.payloads
              .where((p) => p['expected_runtime_id'] == 'runtime-A')
              .length,
          1,
        );
        script.runtimeId = 'runtime-B';
        script.generation = 0;
        await helpers.click(tester, 'converse-memory-refresh');
        select();
        await tester.pump();
        await helpers.click(tester, 'converse-memory-use');
        expect(
          script.payloads
              .where((p) => p['expected_runtime_id'] == 'runtime-B')
              .length,
          1,
        );
        expect(
          button().onPressed,
          isNull,
          reason: 'B503 keeps replacement enable paused',
        );
        old.complete(
          http.Response(
            jsonEncode({
              ...binding.toJson(),
              'runtime_id': 'runtime-A',
              'use_generation': 1,
              'enabled': true,
              'implementation_id': 'korina-dag-v1',
              'selector_ids': ['n-1'],
              'pending': false,
              'notice': 'ready',
            }),
            200,
            headers: {'content-type': 'application/json'},
          ),
        );
        await tester.pumpAndSettle();
        await helpers.click(tester, 'converse-memory-refresh');
        select();
        await tester.pump();
        final replacementEnabled = button().onPressed != null;
        expect(
          script.payloads.where((p) => p.containsKey('expected_runtime_id')),
          hasLength(2),
          reason: 'Refresh and late acknowledgment never replay a write',
        );
        expect(
          replacementEnabled,
          isFalse,
          reason: 'Old A receipt cannot settle potentially live B control',
        );
        expect(tester.takeException(), isNull);
      } finally {
        if (!old.isCompleted) old.complete(http.Response('', 503));
      }
    },
  );
}
