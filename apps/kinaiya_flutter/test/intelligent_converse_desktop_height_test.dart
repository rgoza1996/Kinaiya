// Independent presentation regression: public synthetic artifacts only.
// Authored without reading the pending desktop-height implementation change.
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/converse_interaction.dart';
import 'package:kinaiya_flutter/ui/intelligent_converse_stage.dart';
import 'package:kinaiya_flutter/ui/visual_theme.dart';

import 'support/intelligent_artifact_fixtures.dart';

const _header = ValueKey('synthetic-application-header');
const _position = ValueKey('artifact-portrait-position');
const _artifact = ValueKey('converse-intelligent-artifact');
const _close = ValueKey('converse-artifact-close');
const _call = ValueKey('synthetic-owner-call');
const _stop = ValueKey('synthetic-owner-stop');
const _draft = ValueKey('synthetic-converse-draft');

class _Host {
  _Host() {
    state = AppState()
      ..client = KinaiyaClient.mock(
        sessionBase: 'http://synthetic',
        agentBase: 'http://synthetic',
        converseBase: 'http://synthetic',
        mockTransport: MockClient(
          (_) async => http.Response('unexpected', 500),
        ),
        artifactClientFactory: () => MockClient((request) async {
          requests.add(request);
          if (request.method != 'GET' || request.url.path != '/v1/artifacts') {
            return http.Response('unexpected', 500);
          }
          return http.Response(
            jsonEncode({'items': items, 'next_after': null}),
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
  }

  late final AppState state;
  final portrait = GlobalKey(),
      transcript = GlobalKey(),
      composer = GlobalKey();
  final harness = GlobalKey<_HarnessState>();
  final requests = <http.Request>[];
  List<Map<String, Object?>> items = [];
  int disposals = 0, calls = 0, stops = 0;

  Future<void> publish(WidgetTester tester) async {
    items = [
      artifactFixture(
        id: 'public-long-checklist',
        components: [
          artifactComponent('long-checklist', 'text', {
            'text': List.generate(
              70,
              (index) =>
                  'Public checklist item $index: inspect the synthetic result and record its outcome.',
            ).join('\n'),
          }),
        ],
      ),
    ];
    await state.refreshArtifacts();
    expect(state.activeArtifact?.artifactId, 'public-long-checklist');
    await tester.pump();
  }
}

class _Preserved extends StatefulWidget {
  const _Preserved({super.key, required this.child, required this.onDispose});
  final Widget child;
  final VoidCallback onDispose;

  @override
  State<_Preserved> createState() => _PreservedState();
}

class _PreservedState extends State<_Preserved> {
  @override
  void dispose() {
    widget.onDispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => widget.child;
}

class _Harness extends StatefulWidget {
  const _Harness({super.key, required this.host});
  final _Host host;

  @override
  State<_Harness> createState() => _HarnessState();
}

class _HarnessState extends State<_Harness> {
  final input = TextEditingController();

  @override
  void dispose() {
    input.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final host = widget.host;
    return ListenableBuilder(
      listenable: host.state,
      builder: (_, _) => IntelligentConverseStage(
        state: host.state,
        animate: true,
        richEnabled: true,
        onRichEnabled: (_) {},
        onHistory: () {},
        onAsk: (value) => input.text = value,
        character: _Preserved(
          key: host.portrait,
          onDispose: () => host.disposals++,
          child: const SizedBox.expand(
            child: ColoredBox(
              color: Color(0xFF173481),
              child: Center(child: Text('Public character fixture')),
            ),
          ),
        ),
        transcript: _Preserved(
          key: host.transcript,
          onDispose: () => host.disposals++,
          child: ListView(
            children: [
              for (var index = 0; index < 30; index++)
                Text('Public transcript line $index.'),
            ],
          ),
        ),
        composer: _Preserved(
          key: host.composer,
          onDispose: () => host.disposals++,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              TextField(
                key: _draft,
                controller: input,
                minLines: 1,
                maxLines: 4,
                decoration: const InputDecoration(
                  labelText: 'Talk with Kinaiya',
                ),
              ),
              const SizedBox(height: 8),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  TextButton(
                    key: _stop,
                    onPressed: () => host.stops++,
                    child: const Text('Stop speech'),
                  ),
                  FilledButton(onPressed: () {}, child: const Text('Send')),
                ],
              ),
              const Text(
                'Native audio import: 16 kHz mono PCM16 WAV, 30 seconds maximum.',
              ),
            ],
          ),
        ),
        controls: FilledButton(
          key: _call,
          onPressed: () => host.calls++,
          child: const Text('End conversation'),
        ),
        normalBuilder: (character, transcript, composer) => ConverseInteraction(
          character: character,
          transcript: transcript,
          composer: composer,
          modelSummary: const Text('Synthetic local model'),
          status: 'Ready',
          callLabel: 'Talk with Kinaiya',
          microphoneLabel: 'Unmute',
          onCall: () => host.calls++,
          onMicrophone: () {},
          onModelSettings: () {},
          onHistory: () {},
          onVoiceOptions: () {},
          onMemory: () {},
        ),
      ),
    );
  }
}

Future<void> _pump(
  WidgetTester tester,
  _Host host, {
  Size size = const Size(1280, 720),
  double scale = 1,
}) async {
  tester.view.devicePixelRatio = 1;
  tester.view.physicalSize = size;
  tester.platformDispatcher.textScaleFactorTestValue = scale;
  addTearDown(tester.view.resetDevicePixelRatio);
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
  await tester.pumpWidget(
    MaterialApp(
      theme: kinaiyaTheme(designSystem: true, brightness: Brightness.dark),
      home: Scaffold(
        body: Column(
          children: [
            const SizedBox(height: 64, width: double.infinity, key: _header),
            Expanded(
              child: _Harness(key: host.harness, host: host),
            ),
          ],
        ),
      ),
    ),
  );
  await tester.pump();
}

void _near(Rect actual, Rect expected) {
  expect(actual.left, closeTo(expected.left, .01));
  expect(actual.top, closeTo(expected.top, .01));
  expect(actual.width, closeTo(expected.width, .01));
  expect(actual.height, closeTo(expected.height, .01));
}

Rect _inside(WidgetTester tester, Finder finder, Rect viewport) {
  final rect = tester.getRect(finder);
  expect(rect.width, greaterThan(0));
  expect(rect.height, greaterThan(0));
  expect(rect.left, greaterThanOrEqualTo(viewport.left - .01));
  expect(rect.top, greaterThanOrEqualTo(viewport.top - .01));
  expect(rect.right, lessThanOrEqualTo(viewport.right + .01));
  expect(rect.bottom, lessThanOrEqualTo(viewport.bottom + .01));
  return rect;
}

void main() {
  testWidgets(
    '720px desktop reserves a visible corner and animates there and back without losing state',
    (tester) async {
      final host = _Host();
      addTearDown(host.state.dispose);
      await _pump(tester, host);
      final viewport = tester.getRect(find.byType(IntelligentConverseStage));
      expect(tester.getRect(find.byKey(_header)).height, 64);
      expect(viewport, const Rect.fromLTWH(0, 64, 1280, 656));
      final normal = tester.getRect(find.byKey(host.portrait));
      final portraitState = tester.state(find.byKey(host.portrait));
      final transcriptState = tester.state(find.byKey(host.transcript));
      final composerState = tester.state(find.byKey(host.composer));
      host.harness.currentState!.input.text = 'Preserve this public draft.';
      await host.publish(tester);
      _near(tester.getRect(find.byKey(host.portrait)), normal);
      expect(find.byKey(_position), findsOneWidget);
      final animation = tester.widget<AnimatedPositioned>(
        find.byKey(_position),
      );
      expect(animation.duration, const Duration(milliseconds: 320));
      expect(animation.curve, Curves.easeInOutCubic);
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 160));
      final midway = tester.getRect(find.byKey(host.portrait));
      expect(midway, isNot(normal));
      await tester.pump(const Duration(milliseconds: 160));
      final corner = _inside(tester, find.byKey(host.portrait), viewport);
      expect(midway, isNot(corner));
      expect(corner.width, lessThan(normal.width));
      expect(corner.left, greaterThan(normal.left));
      final result = _inside(tester, find.byKey(_artifact), viewport);
      expect(
        result.overlaps(corner),
        false,
        reason: 'Character must not cover the result',
      );
      expect(result.right, lessThanOrEqualTo(corner.left));
      _inside(tester, find.byKey(host.transcript), viewport);
      _inside(tester, find.byKey(host.composer), viewport);
      _inside(tester, find.byKey(_call), viewport);
      _inside(tester, find.byKey(_stop), viewport);
      expect(find.byKey(host.portrait).hitTestable(), findsOneWidget);
      expect(find.byKey(host.transcript).hitTestable(), findsOneWidget);
      expect(tester.state(find.byKey(host.portrait)), same(portraitState));
      expect(tester.state(find.byKey(host.transcript)), same(transcriptState));
      expect(tester.state(find.byKey(host.composer)), same(composerState));
      expect(host.disposals, 0);
      expect(tester.takeException(), isNull);

      // A tall result scrolls inside its allotted viewport, without moving
      // the character or the existing transcript/composer owners offscreen.
      final resultScroll = find.descendant(
        of: find.byKey(_artifact),
        matching: find.byType(SingleChildScrollView),
      );
      expect(resultScroll, findsOneWidget);
      await tester.drag(resultScroll, const Offset(0, -600));
      await tester.pumpAndSettle();
      _near(tester.getRect(find.byKey(host.portrait)), corner);
      _inside(tester, find.byKey(host.transcript), viewport);
      _inside(tester, find.byKey(host.composer), viewport);
      expect(
        host.harness.currentState!.input.text,
        'Preserve this public draft.',
      );

      await tester.tap(find.byKey(_close));
      await tester.pump();
      _near(tester.getRect(find.byKey(host.portrait)), corner);
      await tester.pump(const Duration(milliseconds: 160));
      final returning = tester.getRect(find.byKey(host.portrait));
      expect(returning, isNot(corner));
      expect(returning, isNot(normal));
      await tester.pump(const Duration(milliseconds: 161));
      _near(tester.getRect(find.byKey(host.portrait)), normal);
      expect(find.byKey(_artifact), findsNothing);
      expect(tester.state(find.byKey(host.portrait)), same(portraitState));
      expect(tester.state(find.byKey(host.transcript)), same(transcriptState));
      expect(tester.state(find.byKey(host.composer)), same(composerState));
      expect(
        host.harness.currentState!.input.text,
        'Preserve this public draft.',
      );
      expect(host.disposals, 0);
      expect(host.requests.every((request) => request.method == 'GET'), true);
      expect(tester.takeException(), isNull);
    },
  );

  for (final sample in [
    (const Size(1440, 500), 1.0),
    (const Size(1280, 720), 2.0),
  ]) {
    testWidgets(
      'short or large-text desktop retains reachable fallback at $sample',
      (tester) async {
        final host = _Host();
        addTearDown(host.state.dispose);
        await _pump(tester, host, size: sample.$1, scale: sample.$2);
        final identities = [
          tester.state(find.byKey(host.portrait)),
          tester.state(find.byKey(host.transcript)),
          tester.state(find.byKey(host.composer)),
        ];
        await host.publish(tester);
        await tester.pumpAndSettle();
        expect(
          find.byKey(const ValueKey('converse-artifact-scroll')),
          findsOneWidget,
        );
        expect(find.byKey(_position), findsNothing);
        final viewport = tester.getRect(find.byType(IntelligentConverseStage));
        for (final key in [host.transcript, host.portrait, host.composer]) {
          await tester.ensureVisible(find.byKey(key));
          await tester.pumpAndSettle();
          final visible = tester.getRect(find.byKey(key)).intersect(viewport);
          expect(visible.width, greaterThan(0));
          expect(visible.height, greaterThan(0));
        }
        for (final key in [_call, _stop]) {
          await tester.ensureVisible(find.byKey(key));
          await tester.pumpAndSettle();
          await tester.tap(find.byKey(key));
          await tester.pump();
        }
        expect(host.calls, 1);
        expect(host.stops, 1);
        await tester.ensureVisible(find.byKey(_close));
        await tester.pumpAndSettle();
        await tester.tap(find.byKey(_close));
        await tester.pumpAndSettle();
        expect(find.byKey(_artifact), findsNothing);
        expect(tester.state(find.byKey(host.portrait)), same(identities[0]));
        expect(tester.state(find.byKey(host.transcript)), same(identities[1]));
        expect(tester.state(find.byKey(host.composer)), same(identities[2]));
        expect(host.disposals, 0);
        expect(tester.takeException(), isNull);
      },
    );
  }
}
