import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/design_tokens.dart';
import 'package:kinaiya_flutter/ui/mcp_settings.dart';
import 'package:kinaiya_flutter/ui/visual_theme.dart';

import 'provider_consent_test.dart' show ConsentClient;

Map<String, Object?> snapshot([String id = 'public-server']) => {
  'configured': true,
  'revision': 7,
  'blocked': false,
  'servers': [
    {
      'id': id,
      'transport': 'http',
      'enabled': false,
      'endpoint': 'http://127.0.0.1:9000/mcp',
      'content_scope': 'public_synthetic',
      'allowed_tools': ['lookup'],
      'credentialed': false,
    },
  ],
};

class McpFixture extends ConsentClient {
  String id = 'public-server';
  final controls = <Map<String, Object?>>[];
  Completer<Map<String, Object?>>? snapshotGate;
  Completer<Map<String, Object?>>? controlGate;
  bool failControl = false;
  bool malformed = false;

  @override
  Future<Map<String, Object?>> mcpManagement() async {
    if (snapshotGate != null) return snapshotGate!.future;
    return malformed ? {'configured': true, 'servers': 42} : snapshot(id);
  }

  @override
  Future<Map<String, Object?>> controlMcp(Map<String, Object?> body) async {
    controls.add(body);
    if (failControl) throw ApiException(503, 'synthetic interrupted response');
    if (controlGate != null) return controlGate!.future;
    return {'status': 'connected'};
  }
}

Future<AppState> mount(WidgetTester tester, McpFixture api) async {
  tester.view.physicalSize = const Size(1100, 1100);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  final state = AppState()
    ..connect(ConnectionProfile.defaults(), injected: api);
  state.agentInferenceAllowed = false;
  state.converseInferenceAllowed = false;
  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(
        body: Builder(
          builder: (context) => TextButton(
            onPressed: () => showDialog<void>(
              context: context,
              builder: (_) => McpSettings(state: state),
            ),
            child: const Text('Open settings'),
          ),
        ),
      ),
    ),
  );
  await tester.tap(find.text('Open settings'));
  await tester.pump();
  return state;
}

Future<void> finish(WidgetTester tester, AppState state) async {
  await tester.pumpWidget(const SizedBox.shrink());
  state.dispose();
}

void main() {
  testWidgets('inspection and explicit test work with inference denied', (
    tester,
  ) async {
    final api = McpFixture();
    final state = await mount(tester, api);
    await tester.pumpAndSettle();
    expect(find.text('public-server · disabled'), findsOneWidget);
    expect(api.controls, isEmpty);
    await tester.tap(find.text('test public-server'));
    await tester.pumpAndSettle();
    expect(api.controls, isEmpty);
    await tester.tap(find.widgetWithText(FilledButton, 'test'));
    await tester.pumpAndSettle();
    expect(api.controls.single['operation'], 'test');
    expect(api.controls.single['expected_revision'], 7);
    expect(api.createTasks + api.turns, 0);
    expect(find.textContaining('Connection check completed'), findsOneWidget);
    await finish(tester, state);
  });

  testWidgets('saving a disabled profile requires review and does not enable', (
    tester,
  ) async {
    final api = McpFixture();
    final state = await mount(tester, api);
    await tester.pumpAndSettle();
    await tester.tap(find.text('Add or replace profile'));
    await tester.pumpAndSettle();
    final save = find.widgetWithText(FilledButton, 'Save disabled profile');
    expect(tester.widget<FilledButton>(save).onPressed, isNull);
    await tester.enterText(
      find.byType(TextField),
      jsonEncode({
        'id': 'new-public',
        'transport': 'http',
        'url': 'http://127.0.0.1:9000/mcp',
        'enabled': false,
      }),
    );
    await tester.tap(find.byType(CheckboxListTile));
    await tester.pump();
    await tester.tap(save);
    await tester.pumpAndSettle();
    expect(api.controls.single['operation'], 'configure');
    expect(api.controls.single['reviewed'], isTrue);
    expect((api.controls.single['config'] as Map)['enabled'], isFalse);
    expect(api.createTasks + api.turns, 0);
    await finish(tester, state);
  });

  testWidgets('enable remains an explicit reviewed owner action', (
    tester,
  ) async {
    final api = McpFixture();
    final state = await mount(tester, api);
    await tester.pumpAndSettle();
    await tester.tap(find.text('enable public-server'));
    await tester.pumpAndSettle();
    expect(api.controls, isEmpty);
    await tester.tap(find.widgetWithText(FilledButton, 'enable'));
    await tester.pumpAndSettle();
    expect(api.controls.single['operation'], 'enable');
    expect(api.controls.single['reviewed'], isTrue);
    expect(api.controls.single['idempotency_key'], isA<String>());
    await finish(tester, state);
  });

  testWidgets('lost acknowledgment is never retried automatically', (
    tester,
  ) async {
    final api = McpFixture()..failControl = true;
    final state = await mount(tester, api);
    await tester.pumpAndSettle();
    await tester.tap(find.text('test public-server'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(FilledButton, 'test'));
    await tester.pumpAndSettle();
    expect(api.controls.length, 1);
    expect(find.textContaining('Request not confirmed'), findsOneWidget);
    expect(
      tester
          .widget<FilledButton>(
            find.widgetWithText(FilledButton, 'Add or replace profile'),
          )
          .onPressed,
      isNull,
    );
    await tester.pump(const Duration(seconds: 1));
    expect(api.controls.length, 1);
    await tester.tap(find.text('Refresh connections'));
    await tester.pumpAndSettle();
    expect(
      tester
          .widget<FilledButton>(
            find.widgetWithText(FilledButton, 'Add or replace profile'),
          )
          .onPressed,
      isNotNull,
    );
    await finish(tester, state);
  });

  testWidgets('late old-backend metadata never replaces current settings', (
    tester,
  ) async {
    final old = McpFixture()..snapshotGate = Completer<Map<String, Object?>>();
    final state = await mount(tester, old);
    final fresh = McpFixture()..id = 'fresh-server';
    state.connect(ConnectionProfile.defaults(), injected: fresh);
    await tester.pumpAndSettle();
    expect(find.text('fresh-server · disabled'), findsOneWidget);
    old.snapshotGate!.complete(snapshot('private-old-server'));
    await tester.pumpAndSettle();
    expect(find.text('fresh-server · disabled'), findsOneWidget);
    expect(find.textContaining('private-old-server'), findsNothing);
    await finish(tester, state);
  });

  testWidgets(
    'a profile dialog cannot submit old data to a reconnected backend',
    (tester) async {
      final old = McpFixture();
      final state = await mount(tester, old);
      await tester.pumpAndSettle();
      await tester.tap(find.text('Add or replace profile'));
      await tester.pumpAndSettle();
      await tester.enterText(
        find.byType(TextField),
        jsonEncode({'id': 'old', 'enabled': false}),
      );
      await tester.tap(find.byType(CheckboxListTile));
      final fresh = McpFixture()..id = 'new-server';
      state.connect(ConnectionProfile.defaults(), injected: fresh);
      await tester.pumpAndSettle();
      await tester.tap(
        find.widgetWithText(FilledButton, 'Save disabled profile'),
      );
      await tester.pumpAndSettle();
      expect(old.controls, isEmpty);
      expect(fresh.controls, isEmpty);
      await finish(tester, state);
    },
  );

  testWidgets('malformed management metadata fails closed', (tester) async {
    final api = McpFixture()..malformed = true;
    final state = await mount(tester, api);
    await tester.pumpAndSettle();
    expect(
      find.textContaining('Could not inspect MCP management'),
      findsOneWidget,
    );
    expect(tester.takeException(), isNull);
    expect(api.controls, isEmpty);
    await finish(tester, state);
  });

  testWidgets(
    'owner text hierarchy roles distinguish primary content from technical metadata and scale across palettes at 320px/2x text',
    (tester) async {
      tester.view.physicalSize = const Size(320, 480);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);

      final api = McpFixture();
      final state = AppState()
        ..connect(ConnectionProfile.defaults(), injected: api);
      state.agentInferenceAllowed = false;
      state.converseInferenceAllowed = false;

      final themes = <ThemeData>[
        kinaiyaTheme(brightness: Brightness.light, designSystem: true),
        kinaiyaTheme(brightness: Brightness.dark, designSystem: true),
        kinaiyaTheme(
          brightness: Brightness.dark,
          primary: const Color(0xFF0E7C66),
          background: const Color(0xFF101926),
          designSystem: true,
        ),
      ];

      for (final theme in themes) {
        await tester.pumpWidget(
          MaterialApp(
            theme: theme,
            builder: (context, child) => MediaQuery(
              data: MediaQuery.of(
                context,
              ).copyWith(textScaler: const TextScaler.linear(2.0)),
              child: child!,
            ),
            home: Scaffold(body: McpSettings(state: state, embedded: true)),
          ),
        );
        await tester.pumpAndSettle();
        expect(tester.takeException(), isNull);

        final pageTitle = tester.widget<Text>(
          find.byKey(const ValueKey('mcp-page-title')),
        );
        final supporting = tester.widget<Text>(
          find.byKey(const ValueKey('mcp-supporting-guidance')),
        );
        final revision = tester.widget<Text>(
          find.byKey(const ValueKey('mcp-technical-revision')),
        );
        final serverTitle = tester.widget<Text>(
          find.byKey(const ValueKey('mcp-server-title-public-server')),
        );
        final serverScope = tester.widget<Text>(
          find.byKey(const ValueKey('mcp-server-scope-public-server')),
        );
        final serverEndpoint = tester.widget<Text>(
          find.byKey(const ValueKey('mcp-server-endpoint-public-server')),
        );

        expect(
          pageTitle.style!.fontSize!,
          greaterThan(serverTitle.style!.fontSize!),
        );
        expect(
          serverTitle.style!.fontSize!,
          greaterThan(serverScope.style!.fontSize!),
        );
        expect(
          serverScope.style!.fontSize!,
          greaterThan(supporting.style!.fontSize!),
        );
        expect(
          supporting.style!.fontSize!,
          greaterThan(revision.style!.fontSize!),
        );
        expect(revision.style!.fontFamily, 'monospace');
        expect(serverEndpoint.style!.fontFamily, 'monospace');
        expect(pageTitle.style!.color, theme.colorScheme.onSurface);
        expect(serverScope.style!.color, theme.colorScheme.onSurface);
        expect(revision.style!.color, theme.colorScheme.onSurfaceVariant);

        // Primary content appears above technical endpoint metadata inside the card.
        expect(
          tester
              .getTopLeft(
                find.byKey(const ValueKey('mcp-server-scope-public-server')),
              )
              .dy,
          lessThan(
            tester
                .getTopLeft(
                  find.byKey(
                    const ValueKey('mcp-server-endpoint-public-server'),
                  ),
                )
                .dy,
          ),
        );

        // Required content is never truncated with ellipsis.
        expect(serverScope.overflow, isNot(TextOverflow.ellipsis));
        expect(serverEndpoint.overflow, isNot(TextOverflow.ellipsis));

        final roles = KinaiyaDesign.textRoles(
          tester.element(find.byType(McpSettings)),
        );
        expect(roles.pageTitle.fontSize, 22);
        expect(roles.sectionTitle.fontSize, 16);
        expect(roles.primaryContent.fontSize, 15);
        expect(roles.supportingText.fontSize, 14);
        expect(roles.technicalMetadata.fontSize, 13);
      }

      await finish(tester, state);
    },
  );
}
