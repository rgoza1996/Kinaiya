import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/converse_setup.dart';
import 'package:kinaiya_flutter/presentation/controller.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/model_setup.dart';

Map<String, Object?> setupFixture() => {
  'contract_version': 1,
  'revision': 'a' * 64,
  'roles': {
    for (final role in ['conversation', 'stt', 'tts', 'decisions'])
      role: {
        'selected_id': 'fixture:$role',
        'label': 'Synthetic $role',
        'configured': true,
        'source': role == 'conversation' ? 'runtime' : 'host_configuration',
        'options': [
          {'id': 'fixture:$role', 'label': 'Synthetic $role'},
        ],
        'mutable': false,
        'reason': 'Configured on host; switching requires a service restart.',
      },
  },
  'inputs': {
    'supported': ['text'],
    'selected': ['text'],
    'direct_audio': false,
    'stt_required': true,
  },
  'reasoning': {
    'selected': 'none',
    'options': ['none'],
    'mutable': false,
    'reason': 'Host-managed.',
  },
  'operation_state': {
    'text': {
      'active': 0,
      'cleanup_pending': 0,
      'cleanup_failed': 0,
      'admitting': 0,
      'capacity': 32,
    },
    'speech': {'active': 0, 'cancellation_pending': 0, 'cleanup_failed': 0},
  },
};

class SetupStorage implements PresentationStorage {
  final writes = <String>[];
  bool failWrites = false;
  @override
  bool get persistent => true;
  @override
  Future<String?> read() async => null;
  @override
  Future<void> write(String value) async {
    if (failWrites) throw StateError('private storage backend details');
    writes.add(value);
  }
}

// Continuous presentation drift deliberately never settles. Advance a bounded
// 320 ms of real frames for HTTP completion, route and popover transitions.
Future<void> pumpSetupFrames(WidgetTester tester) async {
  await tester.pump();
  for (var frame = 0; frame < 20; frame++) {
    await tester.pump(const Duration(milliseconds: 16));
  }
}

void main() {
  test('setup rejects unsupported or incoherent declarations', () {
    expect(
      ConverseSetup.fromJson(setupFixture()).roles['tts']!.label,
      'Synthetic tts',
    );
    final future = setupFixture();
    (future['reasoning']! as Map)['selected'] = 'future_level';
    (future['reasoning']! as Map)['options'] = ['future_level'];
    expect(ConverseSetup.fromJson(future).reasoning, 'future_level');
    for (final mutate in <void Function(Map<String, Object?>)>[
      (data) => data['contract_version'] = 2,
      (data) => data['revision'] = 'unbound',
      (data) => (data['inputs']! as Map)['direct_audio'] = true,
      (data) => (data['inputs']! as Map)['supported'] = ['text', 'video'],
      (data) =>
          ((data['roles']! as Map)['conversation'] as Map)['mutable'] = true,
      (data) => ((data['roles']! as Map)['conversation'] as Map)['label'] =
          'secret\nfield',
      (data) =>
          ((data['roles']! as Map)['conversation'] as Map)['options'] = [],
      (data) =>
          (data['reasoning']! as Map)['selected'] = 'incoherent-selection',
    ]) {
      final data = setupFixture();
      mutate(data);
      expect(() => ConverseSetup.fromJson(data), throwsFormatException);
    }
  });

  for (final small in [false, true]) {
    testWidgets(
      'setup save and cancel change only local visibility${small ? ' at320px2x' : ''}',
      (tester) async {
        await tester.binding.setSurfaceSize(
          small ? const Size(320, 640) : const Size(1200, 900),
        );
        addTearDown(() => tester.binding.setSurfaceSize(null));
        final requests = <http.Request>[];
        final storage = SetupStorage();
        final presentation = PresentationController(storage: storage);
        final state = AppState();
        final api = KinaiyaClient(
          sessionBase: 'http://localhost:8760',
          agentBase: 'http://localhost:8761',
          converseBase: 'http://localhost:8762',
          ownerToken: 'synthetic-owner',
          httpClient: MockClient((request) async {
            requests.add(request);
            return http.Response(jsonEncode(setupFixture()), 200);
          }),
        );
        state.client = api;
        await tester.runAsync(presentation.initialize);
        await tester.pumpWidget(
          MaterialApp(
            builder: (context, child) => MediaQuery(
              data: MediaQuery.of(
                context,
              ).copyWith(textScaler: TextScaler.linear(small ? 2 : 1)),
              child: child!,
            ),
            home: Builder(
              builder: (context) => Scaffold(
                body: TextButton(
                  onPressed: () => showDialog<void>(
                    context: context,
                    builder: (_) => ModelSetupDialog(
                      state: state,
                      presentation: presentation,
                    ),
                  ),
                  child: const Text('Open'),
                ),
              ),
            ),
          ),
        );
        await tester.tap(find.text('Open'));
        await pumpSetupFrames(tester);
        expect(find.text('Synthetic conversation'), findsOneWidget);
        expect(requests.single.method, 'GET');
        expect(requests.single.url.path, '/v1/converse/setup');
        expect(
          requests.single.headers['Authorization'],
          'Bearer synthetic-owner',
        );
        expect(storage.writes, isEmpty);
        final toggle = find.byKey(const ValueKey('show-converse-view'));
        await tester.ensureVisible(toggle);
        await tester.pump();
        expect(toggle.hitTestable(), findsOneWidget);
        await tester.tap(toggle);
        await tester.pump();
        expect(presentation.preferences.showConverseView, isTrue);
        await tester.ensureVisible(find.text('Cancel'));
        await tester.pump();
        expect(find.text('Cancel').hitTestable(), findsOneWidget);
        await tester.tap(find.text('Cancel'));
        await pumpSetupFrames(tester);
        expect(presentation.preferences.showConverseView, isTrue);
        expect(storage.writes, isEmpty);
        await tester.tap(find.text('Open'));
        await pumpSetupFrames(tester);
        await tester.ensureVisible(toggle);
        await tester.pump();
        expect(toggle.hitTestable(), findsOneWidget);
        await tester.tap(toggle);
        await tester.pump();
        await presentation.setPreferences(
          presentation.preferences.copyWith(showCaptions: false),
        );
        await tester.ensureVisible(
          find.byKey(const ValueKey('save-model-setup')),
        );
        await tester.pump();
        expect(
          find.byKey(const ValueKey('save-model-setup')).hitTestable(),
          findsOneWidget,
        );
        await tester.tap(find.byKey(const ValueKey('save-model-setup')));
        await pumpSetupFrames(tester);
        expect(presentation.preferences.showConverseView, isFalse);
        expect(presentation.preferences.showCaptions, isFalse);
        expect(requests.every((request) => request.method == 'GET'), isTrue);
        expect(state.converseEnabled, isTrue);
        expect(tester.takeException(), isNull);
        await tester.pumpWidget(const SizedBox.shrink());
        state.dispose();
        presentation.dispose();
      },
    );
  }

  for (final small in [false, true]) {
    testWidgets(
      'five glass bubbles expose only bounded read-only native declarations (small=$small)',
      (tester) async {
        await tester.binding.setSurfaceSize(
          small ? const Size(320, 740) : const Size(1536, 1024),
        );
        addTearDown(() => tester.binding.setSurfaceSize(null));
        final methods = <String>[];
        final state = AppState()
          ..client = KinaiyaClient(
            sessionBase: 'http://synthetic',
            agentBase: 'http://synthetic',
            converseBase: 'http://synthetic',
            ownerToken: 'synthetic-owner',
            httpClient: MockClient((request) async {
              methods.add(request.method);
              return http.Response(jsonEncode(setupFixture()), 200);
            }),
          );
        final presentation = PresentationController(storage: SetupStorage());
        await tester.runAsync(presentation.initialize);
        await tester.pumpWidget(
          MaterialApp(
            home: MediaQuery(
              data: MediaQueryData(
                size: small ? const Size(320, 740) : const Size(1536, 1024),
                textScaler: TextScaler.linear(small ? 2 : 1),
              ),
              child: ModelSetupDialog(state: state, presentation: presentation),
            ),
          ),
        );
        await pumpSetupFrames(tester);
        for (final section in [
          'Model',
          'Reasoning',
          'Inputs',
          'Speech',
          'Decisions',
        ]) {
          final bubble = find.byKey(
            ValueKey('setup-bubble-${section.toLowerCase()}'),
          );
          await tester.ensureVisible(bubble);
          await pumpSetupFrames(tester);
          expect(bubble.hitTestable(), findsOneWidget);
          await tester.tap(bubble);
          await pumpSetupFrames(tester);
          final popover = find.byKey(const ValueKey('setup-popover'));
          expect(popover, findsOneWidget);
          await tester.ensureVisible(popover);
          await pumpSetupFrames(tester);
          expect(find.text('$section settings'), findsOneWidget);
          if (section == 'Inputs') {
            expect(
              tester
                  .widget<FilterChip>(find.widgetWithText(FilterChip, 'Audio'))
                  .onSelected,
              isNull,
            );
          }
          if (section == 'Reasoning') {
            expect(
              tester
                  .widget<ChoiceChip>(find.widgetWithText(ChoiceChip, 'High'))
                  .onSelected,
              isNull,
            );
          }
          // Escape closes the popover and restores its invoking bubble focus,
          // without dismissing the encompassing setup or changing any model.
          await tester.sendKeyEvent(LogicalKeyboardKey.escape);
          await pumpSetupFrames(tester);
          expect(popover, findsNothing);
          final invoker = tester.widget<InkWell>(
            find.descendant(of: bubble, matching: find.byType(InkWell)),
          );
          expect(invoker.focusNode!.hasFocus, isTrue);
          expect(find.byType(ModelSetupDialog), findsOneWidget);
          expect(tester.takeException(), isNull);
        }
        expect(methods, ['GET']);
        expect(state.converseSession, isNull);
        expect(state.agentSession, isNull);
        expect(state.converseHistory, isEmpty);
        await tester.pumpWidget(const SizedBox.shrink());
        state.dispose();
        presentation.dispose();
      },
    );
  }

  testWidgets('failed local persistence stays visibly unconfirmed', (
    tester,
  ) async {
    final storage = SetupStorage()..failWrites = true;
    final presentation = PresentationController(storage: storage);
    final state = AppState();
    await tester.runAsync(presentation.initialize);
    await tester.pumpWidget(
      MaterialApp(
        home: ModelSetupDialog(state: state, presentation: presentation),
      ),
    );
    await pumpSetupFrames(tester);
    final toggle = find.byKey(const ValueKey('show-converse-view'));
    await tester.ensureVisible(toggle);
    await tester.pump();
    await tester.tap(toggle);
    final save = find.byKey(const ValueKey('save-model-setup'));
    await tester.ensureVisible(save);
    await tester.pump();
    await tester.tap(save);
    await pumpSetupFrames(tester);
    expect(presentation.preferences.showConverseView, isFalse);
    expect(find.byType(ModelSetupDialog), findsOneWidget);
    expect(
      find.text(
        'Presentation is active for this session, but saving locally is unconfirmed.',
      ),
      findsOneWidget,
    );
    expect(find.textContaining('private storage'), findsNothing);
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
    presentation.dispose();
  });

  testWidgets('late setup reply cannot replace disconnected state', (
    tester,
  ) async {
    final pending = Completer<http.Response>();
    final state = AppState();
    final presentation = PresentationController(storage: SetupStorage());
    state.client = KinaiyaClient(
      sessionBase: 'http://localhost:8760',
      agentBase: 'http://localhost:8761',
      converseBase: 'http://localhost:8762',
      ownerToken: 'synthetic-owner',
      httpClient: MockClient((_) => pending.future),
    );
    await tester.runAsync(presentation.initialize);
    await tester.pumpWidget(
      MaterialApp(
        home: ModelSetupDialog(state: state, presentation: presentation),
      ),
    );
    await tester.pump();
    state.disconnect();
    await tester.pump();
    pending.complete(http.Response(jsonEncode(setupFixture()), 200));
    await pumpSetupFrames(tester);
    expect(find.text('Synthetic conversation'), findsNothing);
    expect(
      find.text('Connect to your host to inspect configured models.'),
      findsOneWidget,
    );
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
    presentation.dispose();
  });
}
