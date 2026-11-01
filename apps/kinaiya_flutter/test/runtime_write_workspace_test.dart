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
import 'agent_model_transport_test.dart'
    show ModelTestClient, modelResponse, sentBody;
import 'model_write_workspace_test.dart' show ModelMemoryStorage;
import 'agent_runtime_settings_test.dart';

class RuntimeWorkspaceFixture {
  RuntimeWorkspaceFixture({
    required this.token,
    this.backend = runtimeBackendA,
    this.instance = runtimeInstanceA,
    this.stateToken = runtimeState,
    this.revision = 0,
    this.bound = true,
    this.writable = true,
    this.holdFirst = false,
    this.recorded = true,
  });
  final String token, backend, instance;
  final bool bound, writable, holdFirst, recorded;
  String stateToken;
  int revision;
  bool activeYolo = false;
  Map<String, Object?> activeSettings = runtimeSettingsJson();
  final posts = <Map<String, Object?>>[], lookups = <Map<String, Object?>>[];
  final firstResponse = Completer<http.StreamedResponse>();
  late final api = KinaiyaClient(
    sessionBase: 'https://runtime-workspace.invalid',
    agentBase: 'https://runtime-workspace.invalid/agent',
    converseBase: 'https://runtime-workspace.invalid/converse',
    ownerToken: token,
    httpClient: MockClient(read),
    agentWorkspaceClientFactory: () => MockClient(read),
    agentRuntimeClientFactory: () => ModelTestClient((request) async {
      final body = sentBody(request);
      if (request.url.path.endsWith('/lookup')) {
        lookups.add(body);
        return modelResponse(runtimeLookupJson(body, recorded: recorded));
      }
      posts.add(body);
      if (holdFirst && posts.length == 1) return firstResponse.future;
      activeYolo = body['yolo'] as bool;
      activeSettings = {...body['settings'] as Map<String, Object?>};
      revision = (body['expected_revision'] as int) + 1;
      stateToken = 'f' * 64;
      return modelResponse(runtimeReceiptJson(body));
    }),
  );
  ConnectionProfile get profile => ConnectionProfile(
    sessionUrl: api.sessionBase,
    agentUrl: api.agentBase,
    converseUrl: api.converseBase,
    ownerToken: token,
  );
  Future<http.Response> read(http.Request request) async {
    if (!request.url.path.endsWith('/options')) {
      return http.Response(
        '{}',
        503,
        headers: {'content-type': 'application/json'},
      );
    }
    return http.Response(
      jsonEncode({
        'schema_version': '1.0',
        'provider': null,
        'permissions': <String, Object?>{},
        'tools': <String>[],
        'runtime_settings': {
          ...runtimeSnapshotJson(
            backend: backend,
            instance: instance,
            state: stateToken,
            revision: revision,
            bound: bound,
            writable: writable,
            yolo: activeYolo,
          ),
          'settings': activeSettings,
          'saved_settings': activeSettings,
          'effective_write':
              activeSettings['file_writes'] != 'deny' &&
              (activeSettings['file_writes'] == 'allow' || activeYolo),
          'effective_shell':
              activeSettings['shell_commands'] != 'deny' &&
              (activeSettings['shell_commands'] == 'allow' || activeYolo),
        },
      }),
      200,
      headers: {'content-type': 'application/json'},
    );
  }
}

Future<void> mountRuntime(
  WidgetTester tester,
  AppState state,
  PresentationController presentation, {
  OptionsPage page = OptionsPage.permissions,
}) async {
  await tester.pumpWidget(
    MaterialApp(
      home: OptionsWorkspace(
        state: state,
        presentation: presentation,
        converseControls: ConverseViewControls(),
        onClose: () {},
        initialPage: page,
      ),
    ),
  );
  await tester.pumpAndSettle();
}

Future<void> changeYolo(WidgetTester tester) async {
  final control = find.widgetWithText(
    SwitchListTile,
    'YOLO for this service instance',
  );
  await tester.ensureVisible(control);
  await tester.tap(control);
  await tester.pumpAndSettle();
}

Future<void> saveRuntime(WidgetTester tester) async {
  final save = find.byKey(const Key('options-save-runtime'));
  await tester.ensureVisible(save);
  await tester.tap(save);
  await tester.pumpAndSettle();
}

bool visibleYolo(WidgetTester tester) => tester
    .widget<SwitchListTile>(
      find.widgetWithText(SwitchListTile, 'YOLO for this service instance'),
    )
    .value;

void main() {
  testWidgets(
    'same-owner reopen does not send and explicit retry preserves exact bound body',
    (tester) async {
      await tester.binding.setSurfaceSize(const Size(1200, 1100));
      addTearDown(() => tester.binding.setSurfaceSize(null));
      final f = RuntimeWorkspaceFixture(
        token: 'runtime-reopen-owner',
        holdFirst: true,
      );
      final state = AppState()..connect(f.profile, injected: f.api);
      final presentation = PresentationController(
        storage: ModelMemoryStorage(),
      );
      await mountRuntime(tester, state, presentation);
      await changeYolo(tester);
      await saveRuntime(tester);
      expect(f.posts.length, 1);
      final body = f.posts.single;
      expect(body['schema_version'], '1.1');
      expect(body['expected_backend'], runtimeBackendA);
      expect(body['expected_instance'], runtimeInstanceA);
      await tester.pumpWidget(const SizedBox());
      await mountRuntime(tester, state, presentation);
      expect(f.posts.length, 1);
      expect(find.text('Check execution save status'), findsOneWidget);
      await tester.pump(const Duration(seconds: 8));
      await tester.pump();
      await tester.tap(find.text('Retry the same execution save'));
      await tester.pumpAndSettle();
      expect(f.posts.length, 2);
      expect(f.posts.last, body);
      expect(f.lookups, isEmpty);
      expect(find.text('Check execution save status'), findsNothing);
      expect(visibleYolo(tester), isTrue);
      f.firstResponse.complete(modelResponse(runtimeReceiptJson(body)));
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
      state.dispose();
      presentation.dispose();
    },
  );

  testWidgets(
    'owner and backend switches hide old work; same DB restart uses full proof without historical YOLO reactivation',
    (tester) async {
      await tester.binding.setSurfaceSize(const Size(1200, 1100));
      addTearDown(() => tester.binding.setSurfaceSize(null));
      final original = RuntimeWorkspaceFixture(
        token: 'runtime-scope-owner',
        holdFirst: true,
      );
      final state = AppState()
        ..connect(original.profile, injected: original.api);
      final presentation = PresentationController(
        storage: ModelMemoryStorage(),
      );
      await mountRuntime(tester, state, presentation);
      await changeYolo(tester);
      await saveRuntime(tester);
      final body = original.posts.single;
      expect(body['yolo'], isTrue);
      final other = RuntimeWorkspaceFixture(token: 'runtime-other-owner');
      state.connect(other.profile, injected: other.api);
      await tester.pumpAndSettle();
      expect(find.text('Check execution save status'), findsNothing);
      expect(find.text('Retry the same execution save'), findsNothing);
      expect(visibleYolo(tester), isFalse);
      expect(other.posts, isEmpty);
      expect(other.lookups, isEmpty);
      final replacement = RuntimeWorkspaceFixture(
        token: original.token,
        backend: runtimeBackendB,
      );
      state.connect(replacement.profile, injected: replacement.api);
      await tester.pumpAndSettle();
      expect(find.text('Check execution save status'), findsNothing);
      expect(find.textContaining('save confirmed'), findsNothing);
      expect(visibleYolo(tester), isFalse);
      expect(replacement.posts, isEmpty);
      expect(replacement.lookups, isEmpty);
      final restarted = RuntimeWorkspaceFixture(
        token: original.token,
        instance: runtimeInstanceB,
        stateToken: 'f' * 64,
        revision: 1,
      );
      state.connect(restarted.profile, injected: restarted.api);
      await tester.pumpAndSettle();
      expect(find.text('Check execution save status'), findsOneWidget);
      expect(find.text('Retry the same execution save'), findsNothing);
      await tester.tap(find.text('Check execution save status'));
      await tester.pumpAndSettle();
      expect(restarted.lookups.single, body);
      expect(restarted.posts, isEmpty);
      expect(find.text('Check execution save status'), findsNothing);
      expect(
        find.textContaining('Review the refreshed active values'),
        findsOneWidget,
      );
      expect(visibleYolo(tester), isFalse);
      expect(restarted.activeYolo, isFalse);
      original.firstResponse.complete(modelResponse(runtimeReceiptJson(body)));
      await tester.pumpAndSettle();
      expect(visibleYolo(tester), isFalse);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
      state.dispose();
      presentation.dispose();
    },
  );

  testWidgets(
    'dirty same-owner draft survives close and reconnect until actual active-state review',
    (tester) async {
      await tester.binding.setSurfaceSize(const Size(1200, 1100));
      addTearDown(() => tester.binding.setSurfaceSize(null));
      final f = RuntimeWorkspaceFixture(token: 'runtime-draft-owner');
      final state = AppState()..connect(f.profile, injected: f.api);
      final presentation = PresentationController(
        storage: ModelMemoryStorage(),
      );
      await mountRuntime(
        tester,
        state,
        presentation,
        page: OptionsPage.workspace,
      );
      final directory = find.byKey(const Key('options-shell-directory'));
      await tester.enterText(directory, './draft//./folder/');
      await tester.pumpAndSettle();
      await tester.pumpWidget(const SizedBox());
      await mountRuntime(
        tester,
        state,
        presentation,
        page: OptionsPage.workspace,
      );
      expect(
        tester.widget<TextField>(directory).controller!.text,
        './draft//./folder/',
      );
      final next = RuntimeWorkspaceFixture(
        token: f.token,
        stateToken: 'f' * 64,
        revision: 1,
      );
      state.connect(next.profile, injected: next.api);
      await tester.pumpAndSettle();
      expect(
        tester.widget<TextField>(directory).controller!.text,
        './draft//./folder/',
      );
      expect(
        find.textContaining('The host changed while this draft was open.'),
        findsOneWidget,
      );
      expect(
        tester
            .widget<FilledButton>(find.byKey(const Key('options-save-runtime')))
            .onPressed,
        isNull,
      );
      expect(f.posts, isEmpty);
      expect(next.posts, isEmpty);
      await tester.ensureVisible(find.text('Use active values'));
      await tester.tap(find.text('Use active values'));
      await tester.pumpAndSettle();
      expect(tester.widget<TextField>(directory).controller!.text, '.');
      expect(
        find.textContaining('The host changed while this draft was open.'),
        findsNothing,
      );
      await tester.pumpWidget(const SizedBox());
      state.dispose();
      presentation.dispose();
    },
  );

  testWidgets(
    'legacy unsupported and retired snapshots remain visible with explicit read-only writes',
    (tester) async {
      await tester.binding.setSurfaceSize(const Size(1200, 1100));
      addTearDown(() => tester.binding.setSurfaceSize(null));
      final presentation = PresentationController(
        storage: ModelMemoryStorage(),
      );
      for (final f in [
        RuntimeWorkspaceFixture(
          token: 'runtime-legacy-owner',
          bound: false,
          instance: 'legacy-instance',
        ),
        RuntimeWorkspaceFixture(
          token: 'runtime-retired-owner',
          writable: false,
        ),
      ]) {
        final state = AppState()..connect(f.profile, injected: f.api);
        await mountRuntime(tester, state, presentation);
        expect(
          find.textContaining('Execution settings are read-only.'),
          findsOneWidget,
        );
        expect(
          // Separate host-wide controls from the scoped tool-rule editor.
          find.text('Service-wide execution controls'),
          findsOneWidget,
        );
        expect(
          tester
              .widget<FilledButton>(
                find.byKey(const Key('options-save-runtime')),
              )
              .onPressed,
          isNull,
        );
        expect(
          tester
              .widget<SwitchListTile>(
                find.widgetWithText(
                  SwitchListTile,
                  'YOLO for this service instance',
                ),
              )
              .onChanged,
          isNull,
        );
        expect(f.posts, isEmpty);
        expect(f.lookups, isEmpty);
        await tester.pumpWidget(const SizedBox());
        state.dispose();
      }
      presentation.dispose();
    },
  );
}
