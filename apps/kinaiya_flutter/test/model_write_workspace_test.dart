import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/config/options_navigation.dart';
import 'package:kinaiya_flutter/presentation/controller.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/options_workspace.dart';
import 'package:kinaiya_flutter/ui/settings_controls.dart';
import 'agent_model_settings_test.dart';
import 'agent_model_transport_test.dart'
    show ModelTestClient, modelResponse, sentBody;

class ModelMemoryStorage implements PresentationStorage {
  @override
  bool get persistent => false;
  @override
  Future<String?> read() async => null;
  @override
  Future<void> write(String value) async {}
}

class ModelWorkspaceFixture {
  ModelWorkspaceFixture({
    this.token = 'synthetic-owner',
    this.backend = backendA,
    this.instance = instanceA,
    this.bound = true,
    this.writable = true,
    this.holdFirst = false,
  });
  final String token, backend, instance;
  final bool bound, writable, holdFirst;
  final posts = <Map<String, Object?>>[], lookups = <Map<String, Object?>>[];
  final firstResponse = Completer<http.StreamedResponse>();
  late final api = KinaiyaClient(
    sessionBase: 'https://same-host.invalid',
    agentBase: 'https://same-host.invalid/agent',
    converseBase: 'https://same-host.invalid/converse',
    ownerToken: token,
    httpClient: MockClient(read),
    agentWorkspaceClientFactory: () => MockClient(read),
    agentModelClientFactory: () => ModelTestClient((request) async {
      final body = sentBody(request);
      if (request.url.path.endsWith('/lookup')) {
        lookups.add(body);
        return modelResponse(lookupJson(body, recorded: false));
      }
      posts.add(body);
      if (holdFirst && posts.length == 1) return firstResponse.future;
      return modelResponse(receiptJson(body));
    }),
  );
  ConnectionProfile get profile => ConnectionProfile(
    sessionUrl: api.sessionBase,
    agentUrl: api.agentBase,
    converseUrl: api.converseBase,
    ownerToken: token,
  );
  Future<http.Response> read(http.Request request) async {
    final Object? body;
    if (request.url.path.endsWith('/options')) {
      body = {
        'schema_version': '1.0',
        'provider': {'provider': 'synthetic', 'model': 'local', 'local': true},
        'permissions': <String, Object?>{},
        'tools': <String>[],
        'max_steps': 8,
        'models': modelSnapshotJson(
          writeAuthority: bound
              ? authorityJson(
                  backend: backend,
                  instance: instance,
                  writable: writable,
                )
              : null,
        ),
      };
    } else if (request.url.path.endsWith('/model-selection')) {
      body = {
        'schema_version': '1.0',
        'harness_version': 'seed-v1',
        'provider': {'provider': 'synthetic', 'model': 'local', 'local': true},
      };
    } else {
      return http.Response(
        '{}',
        503,
        headers: {'content-type': 'application/json'},
      );
    }
    return http.Response(
      jsonEncode(body),
      200,
      headers: {'content-type': 'application/json'},
    );
  }
}

Future<void> mountModels(
  WidgetTester tester,
  AppState state,
  PresentationController presentation,
) async {
  await tester.pumpWidget(
    MaterialApp(
      home: OptionsWorkspace(
        state: state,
        presentation: presentation,
        converseControls: ConverseViewControls(),
        onClose: () {},
        initialPage: OptionsPage.models,
      ),
    ),
  );
  await tester.pumpAndSettle();
}

Future<void> saveDefault(WidgetTester tester) async {
  final button = find.byKey(const ValueKey('harness-model-save'));
  await tester.ensureVisible(button);
  await tester.tap(button);
  await tester.pumpAndSettle();
}

void main() {
  testWidgets(
    'same-owner Options reopen never retries; explicit retry preserves original full body',
    (tester) async {
      await tester.binding.setSurfaceSize(const Size(1200, 1000));
      addTearDown(() => tester.binding.setSurfaceSize(null));
      final f = ModelWorkspaceFixture(holdFirst: true);
      final state = AppState()..connect(f.profile, injected: f.api);
      final presentation = PresentationController(
        storage: ModelMemoryStorage(),
      );
      await mountModels(tester, state, presentation);
      await saveDefault(tester);
      expect(f.posts.length, 1);
      final original = Map<String, Object?>.of(f.posts.single);
      await tester.pumpWidget(const SizedBox());
      await mountModels(tester, state, presentation);
      expect(find.text('Check save status'), findsOneWidget);
      expect(f.posts.length, 1);
      await tester.pump(const Duration(seconds: 8));
      await tester.pump();
      await tester.tap(find.text('Retry the same save'));
      await tester.pumpAndSettle();
      expect(f.posts.length, 2);
      expect(f.posts.last, original);
      expect(find.text('Check save status'), findsNothing);
      expect(f.lookups, isEmpty);
      f.firstResponse.complete(modelResponse(receiptJson(original)));
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
      state.dispose();
      presentation.dispose();
    },
  );

  testWidgets(
    'another owner and replacement backend hide pending controls; original backend restart recovers by proof',
    (tester) async {
      await tester.binding.setSurfaceSize(const Size(1200, 1000));
      addTearDown(() => tester.binding.setSurfaceSize(null));
      final original = ModelWorkspaceFixture(holdFirst: true);
      final state = AppState()
        ..connect(original.profile, injected: original.api);
      final presentation = PresentationController(
        storage: ModelMemoryStorage(),
      );
      await mountModels(tester, state, presentation);
      await saveDefault(tester);
      final body = Map<String, Object?>.of(original.posts.single);
      final otherOwner = ModelWorkspaceFixture(
        token: 'another-synthetic-owner',
      );
      state.connect(otherOwner.profile, injected: otherOwner.api);
      await tester.pumpAndSettle();
      expect(find.text('Check save status'), findsNothing);
      expect(find.text('Retry the same save'), findsNothing);
      expect(otherOwner.posts, isEmpty);
      expect(otherOwner.lookups, isEmpty);
      await saveDefault(tester);
      expect(otherOwner.posts.length, 1);
      expect(
        otherOwner.posts.single['idempotency_key'],
        isNot(body['idempotency_key']),
      );
      final replacement = ModelWorkspaceFixture(backend: backendB);
      state.connect(replacement.profile, injected: replacement.api);
      await tester.pumpAndSettle();
      expect(find.text('Check save status'), findsNothing);
      expect(find.textContaining('The model save is confirmed.'), findsNothing);
      expect(replacement.posts, isEmpty);
      expect(replacement.lookups, isEmpty);
      final restarted = ModelWorkspaceFixture(instance: instanceB);
      state.connect(restarted.profile, injected: restarted.api);
      await tester.pumpAndSettle();
      expect(find.text('Check save status'), findsOneWidget);
      expect(find.text('Retry the same save'), findsNothing);
      await tester.tap(find.text('Check save status'));
      await tester.pumpAndSettle();
      expect(restarted.lookups.single, body);
      expect(restarted.posts, isEmpty);
      expect(find.text('Check save status'), findsNothing);
      expect(
        find.textContaining('did not commit and can no longer apply'),
        findsOneWidget,
      );
      original.firstResponse.complete(modelResponse(receiptJson(body)));
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
      state.dispose();
      presentation.dispose();
    },
  );

  testWidgets(
    'legacy and retired hosts expose metadata with explicitly read-only model saves',
    (tester) async {
      final presentation = PresentationController(
        storage: ModelMemoryStorage(),
      );
      for (final f in [
        ModelWorkspaceFixture(bound: false),
        ModelWorkspaceFixture(writable: false),
      ]) {
        final state = AppState()..connect(f.profile, injected: f.api);
        await mountModels(tester, state, presentation);
        expect(
          find.textContaining('Model settings are read-only.'),
          findsOneWidget,
        );
        final save = tester.widget<FilledButton>(
          find.byKey(const ValueKey('harness-model-save')),
        );
        expect(save.onPressed, isNull);
        expect(f.posts, isEmpty);
        expect(f.lookups, isEmpty);
        await tester.pumpWidget(const SizedBox());
        state.dispose();
      }
      presentation.dispose();
    },
  );
}
