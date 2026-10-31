import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/artifacts/models.dart';
import 'package:kinaiya_flutter/artifacts/renderer.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/visual_theme.dart';

import 'support/intelligent_artifact_fixtures.dart';

Map<String, Object?> component(
  String id,
  String type,
  Map<String, Object?> data,
) => {'id': id, 'type': type, 'title': '$id panel', 'data': data};

List<Map<String, Object?>> allComponents() => [
  component('copy', 'text', {'text': 'Synthetic generated explanation'}),
  component('state', 'status', {
    'text': 'Synthetic result ready',
    'tone': 'success',
  }),
  component('records', 'table', {
    'columns': ['Name', 'Count'],
    'rows': [
      ['Beta', 20],
      ['Alpha', 3],
      ['Gamma', null],
    ],
    'selectable': true,
  }),
  component('series', 'chart', {
    'labels': ['January', 'February', 'March'],
    'values': [-3, 0, 6],
    'unit': 'items',
    'style': 'line',
  }),
  component('flow', 'diagram', {
    'nodes': [
      {'id': 'prepare', 'label': 'Prepare', 'detail': 'Preparation detail'},
      {'id': 'finish', 'label': 'Finish', 'detail': 'Completion detail'},
    ],
    'edges': [
      {'from': 'prepare', 'to': 'finish', 'label': 'Then'},
    ],
  }),
  component('reference', 'source', {
    'label': 'Synthetic source',
    'url': 'https://example.com/data',
    'freshness': 'simulated',
    'observed_at': 1793404800,
  }),
  component('amount', 'number', {
    'label': 'Requested count',
    'value': 4,
    'min': -10,
    'max': 20,
    'step': 1,
  }),
  component('mode', 'select', {
    'label': 'View mode',
    'options': ['Overview', 'Details'],
    'value': 'Overview',
  }),
  component('question', 'ask', {
    'label': 'Explain this result',
    'message': 'Explain the synthetic result.',
  }),
];

Map<String, Object?> envelope({List<Map<String, Object?>>? components}) => {
  'schema_version': '1.0',
  'artifact_id': 'artifact-test',
  'session_id': 'session-test',
  'thread_id': 'thread-test',
  'epoch': 1,
  'task_id': 'task-test',
  'objective_revision': 1,
  'revision': 1,
  'title': 'Generated synthetic result',
  'summary': 'Synthetic result summary',
  'layout': 'stack',
  'components': components ?? allComponents(),
  'created_at': 1793404800,
  'updated_at': 1793404801,
  'content_sha256': List.filled(64, 'a').join(),
  'content_scope': 'public_synthetic',
  'view_state': <String, Object?>{},
  'view_revision': 0,
};

Future<void> render(
  WidgetTester tester, {
  Map<String, Object?>? raw,
  IntelligentArtifact? artifact,
  bool enabled = true,
  ArtifactAction? onAction,
  ValueChanged<String>? onAsk,
  ValueChanged<bool>? onEditingChanged,
  Size size = const Size(900, 1000),
  double scale = 1,
  bool reducedMotion = false,
}) async {
  tester.view.devicePixelRatio = 1;
  tester.view.physicalSize = size;
  addTearDown(tester.view.resetDevicePixelRatio);
  addTearDown(tester.view.resetPhysicalSize);
  await tester.pumpWidget(
    MaterialApp(
      theme: kinaiyaTheme(designSystem: true, brightness: Brightness.dark),
      home: MediaQuery(
        data: MediaQueryData(
          size: size,
          textScaler: TextScaler.linear(scale),
          disableAnimations: reducedMotion,
        ),
        child: Scaffold(
          body: SingleChildScrollView(
            child: Padding(
              padding: const EdgeInsets.all(12),
              child: artifact == null
                  ? IntelligentArtifactRenderer.fromRaw(
                      raw: raw ?? envelope(),
                      enabled: enabled,
                      onAction: onAction,
                      onAsk: onAsk,
                      onEditingChanged: onEditingChanged,
                    )
                  : IntelligentArtifactRenderer(
                      artifact: artifact,
                      enabled: enabled,
                      onAction: onAction,
                      onAsk: onAsk,
                      onEditingChanged: onEditingChanged,
                    ),
            ),
          ),
        ),
      ),
    ),
  );
  await tester.pump();
}

Future<void> click(WidgetTester tester, Key key) async {
  final finder = find.byKey(key);
  await tester.ensureVisible(finder);
  await tester.pump();
  await tester.tap(finder);
  await tester.pump();
}

void main() {
  test('valid native data is detached, immutable, and roundtrips', () {
    final raw = envelope();
    final artifact = IntelligentArtifact.fromJson(raw);
    expect(artifact.components.length, 9);
    expect(artifact.supported, isTrue);
    expect(artifact.schemaVersion, '1.0');
    expect(artifact.contentScope, 'public_synthetic');
    expect(artifact.taskSubmitSequence, 0);
    final first = ((raw['components']! as List)[0] as Map)['data'] as Map;
    first['text'] = 'Mutated after validation';
    expect(
      artifact.components.first.data['text'],
      'Synthetic generated explanation',
    );
    expect(
      () => artifact.components.first.data['text'] = 'changed',
      throwsUnsupportedError,
    );
    final table = artifact.components[2].data['rows']! as List;
    expect(() => (table[0] as List)[0] = 'changed', throwsUnsupportedError);
    final again = IntelligentArtifact.fromJson(artifact.toJson());
    expect(again.toJson(), artifact.toJson());
  });

  test('unknown, malformed and unsafe data never reaches a component', () {
    final invalid = <Map<String, Object?>>[
      envelope()..['schema_version'] = '2.0',
      envelope()..['executable'] = 'do something',
      envelope(
        components: [
          component('unknown', 'html', {'html': '<script/>'}),
        ],
      ),
      envelope(
        components: [
          component('duplicate', 'text', {'text': 'a'}),
          component('duplicate', 'text', {'text': 'b'}),
        ],
      ),
      envelope(
        components: [
          component('bad\n', 'text', {'text': 'a'}),
        ],
      ),
      envelope(
        components: [
          component('table', 'table', {
            'columns': ['a', 'b'],
            'rows': [
              ['a'],
            ],
          }),
        ],
      ),
      envelope(
        components: [
          component('chart', 'chart', {
            'labels': ['a'],
            'values': [double.nan],
          }),
        ],
      ),
      envelope(
        components: [
          component('chart', 'chart', {
            'labels': ['a'],
            'values': [1e12 + 1],
          }),
        ],
      ),
      envelope(
        components: [
          component('source', 'source', {
            'label': 'Source',
            'url': 'http://example.com',
            'freshness': 'static',
          }),
        ],
      ),
      envelope(
        components: [
          component('source', 'source', {
            'label': 'Source',
            'url': 'https://user:secret@example.com',
            'freshness': 'static',
          }),
        ],
      ),
      envelope(
        components: [
          component('diagram', 'diagram', {
            'nodes': [
              {'id': 'a', 'label': 'a'},
            ],
            'edges': [
              {'from': 'a', 'to': 'missing'},
            ],
          }),
        ],
      ),
      envelope(
        components: [
          component('diagram', 'diagram', {
            'nodes': [
              {'id': 'a', 'label': 'a'},
              {'id': 'a', 'label': 'b'},
            ],
            'edges': [],
          }),
        ],
      ),
      envelope(
        components: [
          component('number', 'number', {
            'label': 'N',
            'value': 21,
            'min': 0,
            'max': 20,
          }),
        ],
      ),
      envelope(
        components: [
          component('select', 'select', {
            'label': 'S',
            'options': ['a', 'a'],
            'value': 'a',
          }),
        ],
      ),
      envelope()..['view_state'] = {'question': 'not an action'},
      envelope()..['view_state'] = {'records': 100},
      envelope()..['view_state'] = {'amount': double.infinity},
      envelope()..['view_state'] = {'missing': 0},
      envelope()..['task_submit_sequence'] = -1,
      envelope()..['task_submit_sequence'] = '1',
      envelope()..['task_submit_sequence'] = 1.5,
    ];
    for (var i = 0; i < invalid.length; i++) {
      expect(
        () => IntelligentArtifact.fromJson(invalid[i]),
        throwsFormatException,
        reason: 'invalid fixture $i',
      );
      expect(IntelligentArtifact.tryParse(invalid[i]), isNull);
    }
  });

  test('native task submit sequence is optional and bounded', () {
    expect(IntelligentArtifact.fromJson(envelope()).taskSubmitSequence, 0);
    expect(
      IntelligentArtifact.fromJson(
        envelope()..['task_submit_sequence'] = 7,
      ).taskSubmitSequence,
      7,
    );
  });

  test(
    'typed fallback validates native metadata and discards untrusted content',
    () {
      final raw = envelope()..['schema_version'] = '2.0';
      raw['components'] = raw;
      raw['view_state'] = {'unexpected': List.filled(100000, 'ignored')};
      final fallback = IntelligentArtifact.readableFallback(raw);
      expect(fallback.supported, isFalse);
      expect(fallback.schemaVersion, '2.0');
      expect(fallback.toJson()['schema_version'], '2.0');
      expect(fallback.artifactId, 'artifact-test');
      expect(fallback.sessionId, 'session-test');
      expect(fallback.components, isEmpty);
      expect(fallback.viewState, isEmpty);
      final invalidMetadata = <Map<String, Object?>>[
        envelope()..['epoch'] = 0,
        envelope()..['content_scope'] = 'invented',
        envelope()..['session_id'] = List.filled(129, 'a').join(),
        envelope()..['revision'] = -1,
        envelope()..['content_sha256'] = 'not a digest',
        envelope()..['created_at'] = double.infinity,
        envelope()..['task_submit_sequence'] = -1,
        envelope()..['summary'] = List.filled(2001, 'a').join(),
        envelope()..['schema_version'] = 2,
        envelope()..['unknown'] = 'field',
      ];
      for (var i = 0; i < invalidMetadata.length; i++) {
        expect(
          () => IntelligentArtifact.readableFallback(invalidMetadata[i]),
          throwsFormatException,
          reason: 'invalid fallback metadata $i',
        );
      }
    },
  );

  testWidgets(
    'typed unsupported artifact renders its summary with no controls',
    (tester) async {
      final raw = envelope()..['schema_version'] = '2.0';
      final actions = <Object?>[];
      final drafts = <String>[];
      await render(
        tester,
        artifact: IntelligentArtifact.readableFallback(raw),
        onAction: (_, value) => actions.add(value),
        onAsk: drafts.add,
      );
      expect(find.text('Generated synthetic result'), findsOneWidget);
      expect(find.text('Synthetic result summary'), findsOneWidget);
      expect(find.textContaining('unsupported or invalid'), findsOneWidget);
      expect(find.byType(TextField), findsNothing);
      expect(find.byType(ChoiceChip), findsNothing);
      expect(find.byKey(const ValueKey('artifact-ask-question')), findsNothing);
      expect(actions, isEmpty);
      expect(drafts, isEmpty);
      expect(tester.takeException(), isNull);
    },
  );

  test('bounds include aggregate JSON, nesting, rows and Unicode', () {
    expect(
      () => IntelligentArtifact.fromJson(
        envelope(
          components: [
            for (var i = 0; i < 25; i++)
              component('text$i', 'text', {'text': 'a'}),
          ],
        ),
      ),
      throwsFormatException,
    );
    expect(
      () => IntelligentArtifact.fromJson(
        envelope(
          components: [
            component('records', 'table', {
              'columns': ['value'],
              'rows': List.generate(101, (_) => ['a']),
            }),
          ],
        ),
      ),
      throwsFormatException,
    );
    expect(
      () => IntelligentArtifact.fromJson(
        envelope(
          components: [
            for (var i = 0; i < 7; i++)
              component('copy$i', 'text', {
                'text': List.filled(7999, 'a').join(),
              }),
          ],
        ),
      ),
      throwsFormatException,
    );
    final cyclic = envelope();
    cyclic['view_state'] = cyclic;
    expect(() => IntelligentArtifact.fromJson(cyclic), throwsFormatException);
    var nested = <Object?>[];
    for (var i = 0; i < 14; i++) {
      nested = [nested];
    }
    expect(
      () => IntelligentArtifact.fromJson(envelope()..['view_state'] = nested),
      throwsFormatException,
    );
    final unicode = envelope()..['title'] = List.filled(160, '🌙').join();
    expect(IntelligentArtifact.fromJson(unicode).title.runes.length, 160);
    unicode['title'] = List.filled(161, '🌙').join();
    expect(() => IntelligentArtifact.fromJson(unicode), throwsFormatException);
    unicode['title'] = String.fromCharCode(0xd800);
    expect(() => IntelligentArtifact.fromJson(unicode), throwsFormatException);
    unicode['title'] = 'Hidden\u0000control';
    expect(() => IntelligentArtifact.fromJson(unicode), throwsFormatException);
  });

  testWidgets(
    'unknown version shows bounded literal summary with no controls',
    (tester) async {
      await render(
        tester,
        raw: envelope()
          ..['schema_version'] = '2.0'
          ..['title'] = '<b>Untrusted title</b>'
          ..['components'] = [
            {'type': 'html', 'code': 'NOT_RENDERED'},
          ],
      );
      expect(find.text('<b>Untrusted title</b>'), findsOneWidget);
      expect(find.text('Synthetic result summary'), findsOneWidget);
      expect(find.textContaining('unsupported or invalid'), findsOneWidget);
      expect(find.byType(TextField), findsNothing);
      expect(find.textContaining('NOT_RENDERED'), findsNothing);
      await render(
        tester,
        raw: {
          'title': List.filled(100000, 'A').join(),
          'summary': 'Safe\u0000 summary',
        },
      );
      expect(find.text(List.filled(160, 'A').join()), findsOneWidget);
      expect(find.text('Safe summary'), findsOneWidget);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('disabled rendering retains data and source provenance', (
    tester,
  ) async {
    final actions = <Object?>[];
    await render(
      tester,
      enabled: false,
      onAction: (id, value) => actions.add(value),
    );
    expect(find.textContaining('Rich rendering is off'), findsOneWidget);
    expect(find.textContaining('Beta'), findsOneWidget);
    expect(find.textContaining('January: -3 items'), findsOneWidget);
    expect(find.textContaining('Prepare → Finish: Then'), findsOneWidget);
    expect(find.textContaining('Freshness: simulated'), findsOneWidget);
    expect(find.textContaining('2026-10-31T00:00:00.000Z'), findsOneWidget);
    expect(find.byType(TextField), findsNothing);
    expect(find.byType(ChoiceChip), findsNothing);
    expect(actions, isEmpty);
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    'table filtering and numeric sorting preserve source row indices',
    (tester) async {
      final actions = <(String, Object?)>[];
      final editing = <bool>[];
      await render(
        tester,
        raw: envelope(components: [allComponents()[2]]),
        onAction: (id, value) => actions.add((id, value)),
        onEditingChanged: editing.add,
      );
      await click(tester, const ValueKey('artifact-sort-records-1'));
      final beta = find.byKey(const ValueKey('artifact-row-records-0'));
      final alpha = find.byKey(const ValueKey('artifact-row-records-1'));
      expect(tester.getTopLeft(alpha).dy, lessThan(tester.getTopLeft(beta).dy));
      await click(tester, const ValueKey('artifact-row-records-1'));
      expect(actions, [('records', 1)]);
      final filter = find.byKey(
        const ValueKey('artifact-table-filter-records'),
      );
      await tester.ensureVisible(filter);
      await tester.enterText(filter, 'beta');
      await tester.pump();
      expect(
        find.byKey(const ValueKey('artifact-row-records-1')),
        findsNothing,
      );
      expect(find.text('1 of 3 rows'), findsOneWidget);
      expect(actions, [('records', 1)]);
      expect(editing, contains(true));
      await click(tester, const ValueKey('artifact-row-records-0'));
      expect(actions.last, ('records', 0));
      await tester.enterText(filter, List.filled(200, 'x').join());
      final field = tester.widget<TextField>(filter);
      expect(
        field.controller?.text.length ??
            tester
                .widget<EditableText>(
                  find.descendant(
                    of: filter,
                    matching: find.byType(EditableText),
                  ),
                )
                .controller
                .text
                .length,
        160,
      );
      expect(actions.length, 2);
    },
  );

  testWidgets('number edits are bounded and apply only after a valid gesture', (
    tester,
  ) async {
    final actions = <(String, Object?)>[];
    await render(
      tester,
      raw: envelope(components: [allComponents()[6]]),
      onAction: (id, value) => actions.add((id, value)),
    );
    final field = find.byKey(const ValueKey('artifact-number-amount'));
    for (final invalid in [
      '21',
      'NaN',
      'Infinity',
      '-Infinity',
      'not a number',
    ]) {
      await tester.enterText(field, invalid);
      await click(tester, const ValueKey('artifact-number-apply-amount'));
      expect(actions, isEmpty);
      expect(find.textContaining('Enter a finite number'), findsOneWidget);
    }
    await tester.enterText(field, '12.5');
    expect(actions, isEmpty);
    await click(tester, const ValueKey('artifact-number-apply-amount'));
    expect(actions, [('amount', 12.5)]);
    await tester.enterText(field, List.filled(100, '1').join());
    expect(tester.widget<TextField>(field).controller!.text.length, 48);
    await click(tester, const ValueKey('artifact-number-apply-amount'));
    expect(actions.length, 1);
  });

  testWidgets('select and diagram emit only declared presentation values', (
    tester,
  ) async {
    final actions = <(String, Object?)>[];
    await render(
      tester,
      raw: envelope(components: [allComponents()[4], allComponents()[7]]),
      onAction: (id, value) => actions.add((id, value)),
    );
    await click(tester, const ValueKey('artifact-option-mode-Details'));
    await click(tester, const ValueKey('artifact-node-flow-finish'));
    expect(actions, [('mode', 'Details'), ('flow', 'finish')]);
    expect(find.text('Finish\nCompletion detail'), findsOneWidget);
    final updated =
        envelope(components: [allComponents()[4], allComponents()[7]])
          ..['view_revision'] = 1
          ..['view_state'] = {'mode': 'Details', 'flow': 'finish'};
    await render(
      tester,
      raw: updated,
      onAction: (id, value) => actions.add((id, value)),
    );
    expect(find.text('Finish\nCompletion detail'), findsOneWidget);
    expect(
      tester
          .widget<ChoiceChip>(
            find.byKey(const ValueKey('artifact-option-mode-Details')),
          )
          .selected,
      isTrue,
    );
    expect(actions.length, 2);
  });

  testWidgets('ask is an explicit draft callback and sources remain inert', (
    tester,
  ) async {
    final actions = <Object?>[];
    final drafts = <String>[];
    await render(
      tester,
      raw: envelope(components: [allComponents()[5], allComponents()[8]]),
      onAction: (id, value) => actions.add(value),
      onAsk: drafts.add,
    );
    expect(actions, isEmpty);
    expect(drafts, isEmpty);
    expect(find.textContaining('https://example.com/data'), findsOneWidget);
    expect(find.byType(Image), findsNothing);
    await click(tester, const ValueKey('artifact-ask-question'));
    expect(drafts, ['Explain the synthetic result.']);
    expect(actions, isEmpty);
  });

  testWidgets(
    'rejected native selections reconcile without replacing local edits',
    (tester) async {
      final raw = artifactFixture(
        components: [
          allComponents()[2],
          allComponents()[4],
          allComponents()[6],
          allComponents()[7],
        ],
      )..['view_state'] = {'records': 0, 'flow': 'prepare', 'mode': 'Overview'};
      Completer<http.Response>? actionResponse;
      var posts = 0;
      final state = AppState()
        ..client = KinaiyaClient.mock(
          sessionBase: 'http://synthetic',
          agentBase: 'http://synthetic',
          converseBase: 'http://synthetic',
          mockTransport: MockClient(
            (_) async => http.Response('unexpected', 500),
          ),
          artifactClientFactory: () => MockClient((request) async {
            if (request.method == 'POST') {
              posts++;
              return actionResponse!.future;
            }
            return http.Response(
              jsonEncode(
                request.url.path == '/v1/artifacts'
                    ? {
                        'items': [raw],
                        'next_after': null,
                      }
                    : raw,
              ),
              200,
            );
          }),
        )
        ..sessionAuthenticated = true
        ..converseSession = SessionInfo(
          sessionId: 'session-test',
          threadId: 'thread-test',
          channel: 'converse',
          epoch: 1,
        );
      addTearDown(state.dispose);
      await state.refreshArtifacts();
      await tester.pumpWidget(
        MaterialApp(
          theme: kinaiyaTheme(),
          home: Scaffold(
            body: SingleChildScrollView(
              child: ListenableBuilder(
                listenable: state,
                builder: (_, _) => IntelligentArtifactRenderer(
                  artifact: state.activeArtifact!,
                  onAction: state.interactWithArtifact,
                  onEditingChanged: state.editArtifact,
                ),
              ),
            ),
          ),
        ),
      );
      final filter = find.byKey(
        const ValueKey('artifact-table-filter-records'),
      );
      final number = find.byKey(const ValueKey('artifact-number-amount'));
      await tester.enterText(filter, 'a');
      FocusManager.instance.primaryFocus?.unfocus();
      await tester.pumpAndSettle();
      await click(tester, const ValueKey('artifact-sort-records-1'));
      await click(tester, const ValueKey('artifact-sort-records-1'));
      expect(find.text('Count ↓'), findsOneWidget);
      await tester.ensureVisible(number);
      await tester.enterText(number, '7');
      await tester.pump();

      bool selected(String key) =>
          tester.widget<ChoiceChip>(find.byKey(ValueKey(key))).selected;
      for (final control in ['mode', 'flow', 'records']) {
        actionResponse = Completer<http.Response>();
        final key = switch (control) {
          'mode' => const ValueKey('artifact-option-mode-Details'),
          'flow' => const ValueKey('artifact-node-flow-finish'),
          _ => const ValueKey('artifact-row-records-1'),
        };
        await click(tester, key);
        expect(state.artifactActionPending, isTrue);
        if (control == 'mode') {
          expect(selected('artifact-option-mode-Details'), isTrue);
        }
        if (control == 'flow') {
          expect(selected('artifact-node-flow-finish'), isTrue);
        }
        if (control == 'records') {
          expect(find.text('Row 2 · Selected'), findsOneWidget);
        }
        actionResponse.complete(
          http.Response('Synthetic quota rejection', 409),
        );
        await tester.pump();
        await tester.pump();
        expect(state.artifactActionPending, isFalse);
        expect(state.artifactError, isNotNull);
        expect(selected('artifact-option-mode-Overview'), isTrue);
        expect(selected('artifact-node-flow-prepare'), isTrue);
        expect(find.text('Row 1 · Selected'), findsOneWidget);
        // The subsequent native refresh still returns exactly A, with unchanged
        // content/view revision. B must not survive that reconciliation either.
        await state.refreshArtifacts();
        await tester.pump();
        expect(selected('artifact-option-mode-Overview'), isTrue);
        expect(selected('artifact-node-flow-prepare'), isTrue);
        expect(find.text('Row 1 · Selected'), findsOneWidget);
        expect(tester.widget<TextField>(number).controller!.text, '7');
        expect(
          tester
              .widget<EditableText>(
                find.descendant(
                  of: filter,
                  matching: find.byType(EditableText),
                ),
              )
              .controller
              .text,
          'a',
        );
        expect(
          tester
              .getTopLeft(find.byKey(const ValueKey('artifact-row-records-0')))
              .dy,
          lessThan(
            tester
                .getTopLeft(
                  find.byKey(const ValueKey('artifact-row-records-1')),
                )
                .dy,
          ),
        );
      }
      expect(posts, 3);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('fresh native snapshot reconciles an unchanged selected scalar', (
    tester,
  ) async {
    await render(
      tester,
      raw: envelope(components: [allComponents()[7]]),
      onAction: (_, _) {},
    );
    await click(tester, const ValueKey('artifact-option-mode-Details'));
    expect(
      tester
          .widget<ChoiceChip>(
            find.byKey(const ValueKey('artifact-option-mode-Details')),
          )
          .selected,
      isTrue,
    );
    await render(
      tester,
      raw: envelope(components: [allComponents()[7]]),
      onAction: (_, _) {},
    );
    expect(
      tester
          .widget<ChoiceChip>(
            find.byKey(const ValueKey('artifact-option-mode-Overview')),
          )
          .selected,
      isTrue,
    );
  });

  testWidgets('view state survives readable mode and poll preserves editing', (
    tester,
  ) async {
    final editing = <bool>[];
    final raw = envelope(
      components: [allComponents()[2], allComponents()[6], allComponents()[7]],
    )..['view_state'] = {'amount': 12, 'mode': 'Details'};
    await render(
      tester,
      raw: raw,
      onEditingChanged: editing.add,
      onAction: (_, _) {},
    );
    final filter = find.byKey(const ValueKey('artifact-table-filter-records'));
    await tester.enterText(filter, 'Beta');
    await tester.pump();
    expect(editing, [true]);
    await render(
      tester,
      raw: raw,
      onEditingChanged: editing.add,
      onAction: (_, _) {},
    );
    expect(editing, [true]);
    expect(find.text('1 of 3 rows'), findsOneWidget);
    await render(
      tester,
      raw: raw,
      enabled: false,
      onEditingChanged: editing.add,
    );
    expect(find.textContaining('Requested count: 12'), findsOneWidget);
    expect(find.textContaining('View mode: Details'), findsOneWidget);
    expect(editing.last, isFalse);
  });

  testWidgets('native actions expose accessible labels and touch targets', (
    tester,
  ) async {
    final semantics = tester.ensureSemantics();
    await render(
      tester,
      raw: envelope(components: [allComponents()[7], allComponents()[8]]),
      onAction: (_, _) {},
      onAsk: (_) {},
    );
    await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
    await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
    semantics.dispose();
  });

  testWidgets('chart has accessible exact data and local inspection', (
    tester,
  ) async {
    final semantics = tester.ensureSemantics();
    final actions = <Object?>[];
    await render(
      tester,
      raw: envelope(components: [allComponents()[3]]),
      onAction: (id, value) => actions.add(value),
    );
    expect(
      find.bySemanticsLabel(RegExp('Chart with 3 data points')),
      findsOneWidget,
    );
    await click(tester, const ValueKey('artifact-chart-data-series'));
    expect(find.text('January: -3 items'), findsOneWidget);
    await click(tester, const ValueKey('artifact-chart-point-series-2'));
    expect(
      find.byKey(const ValueKey('artifact-chart-selected-series')),
      findsOneWidget,
    );
    expect(actions, isEmpty);
    expect(tester.takeException(), isNull);
    semantics.dispose();
  });

  for (final size in [const Size(320, 568), const Size(1200, 900)]) {
    testWidgets(
      'generated controls fit $size at large text with reduced motion',
      (tester) async {
        final raw = envelope()..['layout'] = 'grid';
        final components = raw['components']! as List;
        (components[7] as Map)['data'] = {
          'label': 'A longer generated selection label for a narrow display',
          'options': [List.filled(160, 'A').join(), 'Short'],
          'value': 'Short',
        };
        await render(
          tester,
          raw: raw,
          size: size,
          scale: 2,
          reducedMotion: true,
          onAction: (_, _) {},
          onAsk: (_) {},
        );
        expect(find.text('Generated synthetic result'), findsOneWidget);
        for (final chip in tester.widgetList<ChoiceChip>(
          find.byType(ChoiceChip),
        )) {
          expect(
            chip.chipAnimationStyle?.selectAnimation?.duration,
            Duration.zero,
          );
        }
        expect(tester.binding.transientCallbackCount, 0);
        expect(tester.takeException(), isNull);
        await tester.ensureVisible(
          find.byKey(const ValueKey('artifact-ask-question')),
        );
        await tester.pump();
        expect(tester.takeException(), isNull);
      },
    );
  }

  test('publication size is measured in UTF-8 bytes', () {
    final raw = envelope(
      components: [
        for (var i = 0; i < 3; i++)
          component('copy$i', 'text', {'text': List.filled(6000, '🌙').join()}),
      ],
    );
    expect(utf8.encode(jsonEncode(raw)).length, greaterThan(64 * 1024));
    expect(() => IntelligentArtifact.fromJson(raw), throwsFormatException);
  });
}
