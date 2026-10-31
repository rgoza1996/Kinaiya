import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/artifacts/renderer.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/converse_interaction.dart';
import 'package:kinaiya_flutter/ui/intelligent_converse_stage.dart';
import 'package:kinaiya_flutter/ui/visual_theme.dart';

import 'support/intelligent_artifact_fixtures.dart';

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
  List<Map<String, Object?>> items = [];
  final requests = <http.Request>[];
  final portrait = GlobalKey(),
      transcript = GlobalKey(),
      composer = GlobalKey();
  final harness = GlobalKey<_HarnessState>();
  int disposals = 0, histories = 0, stops = 0, calls = 0;

  Future<void> publish(
    WidgetTester tester,
    List<Map<String, Object?>> next,
  ) async {
    items = next;
    await state.refreshArtifacts();
    await tester.pump();
  }
}

class _Probe extends StatefulWidget {
  const _Probe({super.key, required this.child, required this.onDispose});
  final Widget child;
  final VoidCallback onDispose;
  @override
  State<_Probe> createState() => _ProbeState();
}

class _ProbeState extends State<_Probe> {
  @override
  void dispose() {
    widget.onDispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => SizedBox.expand(child: widget.child);
}

class _Harness extends StatefulWidget {
  const _Harness({super.key, required this.host, required this.animate});
  final _Host host;
  final bool animate;
  @override
  State<_Harness> createState() => _HarnessState();
}

class _HarnessState extends State<_Harness> {
  final controller = TextEditingController();
  bool rich = true;
  @override
  void dispose() {
    controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final host = widget.host;
    return ListenableBuilder(
      listenable: host.state,
      builder: (_, _) => IntelligentConverseStage(
        state: host.state,
        animate: widget.animate,
        richEnabled: rich,
        onRichEnabled: (value) => setState(() => rich = value),
        onAsk: (message) {
          controller.text = message;
        },
        onHistory: () => host.histories++,
        character: _Probe(
          key: host.portrait,
          onDispose: () => host.disposals++,
          child: const ColoredBox(
            color: Color(0xFF102742),
            child: Center(child: Text('Synthetic portrait')),
          ),
        ),
        transcript: _Probe(
          key: host.transcript,
          onDispose: () => host.disposals++,
          child: ListView(
            children: [
              for (final message in host.state.converseHistory)
                Text(message.text),
            ],
          ),
        ),
        composer: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            TextField(
              key: host.composer,
              controller: controller,
              decoration: const InputDecoration(labelText: 'Message Kinaiya'),
            ),
            Wrap(
              spacing: 12,
              children: [
                FilledButton(onPressed: () {}, child: const Text('Send')),
                TextButton(
                  key: const ValueKey('stage-owner-stop'),
                  onPressed: () => host.stops++,
                  child: const Text('Stop speech'),
                ),
              ],
            ),
          ],
        ),
        controls: FilledButton(
          key: const ValueKey('stage-owner-call'),
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
          onHistory: () => host.histories++,
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
  Size size = const Size(1440, 1100),
  double scale = 1,
  bool reducedMotion = false,
  bool animate = true,
}) async {
  tester.view.devicePixelRatio = 1;
  tester.view.physicalSize = size;
  tester.platformDispatcher.textScaleFactorTestValue = scale;
  addTearDown(tester.view.resetDevicePixelRatio);
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetViewInsets);
  addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
  await tester.pumpWidget(
    MaterialApp(
      theme: kinaiyaTheme(designSystem: true, brightness: Brightness.dark),
      builder: (context, child) => MediaQuery(
        data: MediaQuery.of(context).copyWith(disableAnimations: reducedMotion),
        child: child!,
      ),
      home: Scaffold(
        backgroundColor: Colors.white,
        body: Padding(
          padding: const EdgeInsets.fromLTRB(17, 37, 23, 19),
          child: _Harness(key: host.harness, host: host, animate: animate),
        ),
      ),
    ),
  );
  await tester.pump();
}

void _nearRect(Rect actual, Rect expected) {
  expect(actual.left, closeTo(expected.left, .01));
  expect(actual.top, closeTo(expected.top, .01));
  expect(actual.width, closeTo(expected.width, .01));
  expect(actual.height, closeTo(expected.height, .01));
}

Future<void> _click(WidgetTester tester, Finder finder) async {
  await tester.ensureVisible(finder);
  await tester.pump();
  await tester.tap(finder);
  await tester.pump();
}

void main() {
  for (final size in [const Size(1440, 1100), const Size(320, 568)]) {
    testWidgets('stage surface covers a white scaffold at $size', (
      tester,
    ) async {
      final host = _Host();
      addTearDown(host.state.dispose);
      await _pump(tester, host, size: size);
      final background = find.byKey(
        const ValueKey('converse-stage-background'),
      );
      final colors = Theme.of(tester.element(background)).colorScheme;
      expect(tester.widget<ColoredBox>(background).color, colors.surface);
      expect(colors.surface, isNot(Colors.white));
      await host.publish(tester, [artifactFixture()]);
      await tester.pumpAndSettle();
      expect(tester.widget<ColoredBox>(background).color, colors.surface);
      expect(
        tester.getRect(background),
        tester.getRect(find.byType(IntelligentConverseStage)),
      );
      expect(
        find.ancestor(of: find.byKey(host.composer), matching: background),
        findsOneWidget,
      );
      expect(
        find.ancestor(
          of: find.byKey(const ValueKey('converse-intelligent-artifact')),
          matching: background,
        ),
        findsOneWidget,
      );
      expect(tester.takeException(), isNull);
    });
  }

  for (final changed in ['artifact', 'revision', 'connection']) {
    testWidgets(
      'captured callbacks reject a stale $changed before its next frame',
      (tester) async {
        final host = _Host();
        addTearDown(host.state.dispose);
        await _pump(tester, host);
        final first = artifactFixture(
          components: [
            artifactComponent('choice', 'select', {
              'label': 'Choice',
              'options': ['A', 'B'],
              'value': 'A',
            }),
            artifactComponent('question', 'ask', {
              'label': 'Question',
              'message': 'Private old question',
            }),
          ],
        )..['content_scope'] = 'local_only';
        await host.publish(tester, [first]);
        await tester.pumpAndSettle();
        final old = tester.widget<IntelligentArtifactRenderer>(
          find.byType(IntelligentArtifactRenderer),
        );
        final next = {
          ...first,
          'artifact_id': changed == 'artifact'
              ? 'next-artifact'
              : 'artifact-test',
          'revision': changed == 'revision' ? 2 : 1,
          'task_submit_sequence': 2,
          'content_scope': 'public_synthetic',
        };
        var newPosts = 0;
        if (changed == 'connection') {
          host.state.connect(
            ConnectionProfile.defaults(),
            injected: KinaiyaClient.mock(
              sessionBase: 'http://synthetic',
              agentBase: 'http://synthetic',
              converseBase: 'http://synthetic',
              mockTransport: MockClient(
                (_) async => http.Response('unexpected', 500),
              ),
              artifactClientFactory: () => MockClient((request) async {
                if (request.method != 'GET') newPosts++;
                return http.Response(
                  jsonEncode({
                    'items': [next],
                    'next_after': null,
                  }),
                  200,
                );
              }),
            ),
          );
          host.state.converseSession = SessionInfo(
            sessionId: 'session-test',
            threadId: 'thread-test',
            epoch: 1,
            channel: 'converse',
          );
        } else {
          host.items = [next];
        }
        await host.state.refreshArtifacts();
        // Invoke the real callbacks before pumping the new owner frame, when
        // a rendered pointer/keyboard event can still target the earlier result.
        await old.onAction!('choice', 'B');
        old.onAsk!('Private old question');
        old.onEditingChanged!(true);
        expect(host.state.artifactEditing, isFalse);
        expect(host.harness.currentState!.controller.text, isEmpty);
        expect(newPosts, 0);
        expect(
          host.requests.every((request) => request.method == 'GET'),
          isTrue,
        );
        await tester.pumpAndSettle();
        expect(tester.takeException(), isNull);
      },
    );
  }

  testWidgets(
    'feature control precedes artifacts without a duplicate heading',
    (tester) async {
      final host = _Host();
      addTearDown(host.state.dispose);
      await _pump(tester, host);
      expect(find.text('Converse'), findsOneWidget);
      expect(
        find.byKey(const ValueKey('converse-rich-artifacts')),
        findsOneWidget,
      );
      expect(
        find.byKey(const ValueKey('converse-artifact-picker')),
        findsNothing,
      );
      await _click(
        tester,
        find.byKey(const ValueKey('converse-rich-artifacts')),
      );
      expect(host.harness.currentState!.rich, isFalse);
      expect(host.requests, isEmpty);
    },
  );

  testWidgets('portrait starts at its exact global bounds and reverses there', (
    tester,
  ) async {
    final host = _Host();
    addTearDown(host.state.dispose);
    await _pump(tester, host);
    final normal = tester.getRect(find.byKey(host.portrait));
    final portraitState = tester.state(find.byKey(host.portrait));
    final transcriptState = tester.state(find.byKey(host.transcript));
    final composerState = tester.state(find.byKey(host.composer));
    await host.publish(tester, [artifactFixture()]);
    _nearRect(tester.getRect(find.byKey(host.portrait)), normal);
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 160));
    final midway = tester.getRect(find.byKey(host.portrait));
    expect(midway, isNot(normal));
    await tester.pump(const Duration(milliseconds: 160));
    final corner = tester.getRect(find.byKey(host.portrait));
    expect(corner.width, lessThan(normal.width));
    expect(corner.left, greaterThan(normal.left));
    expect(corner.top, greaterThan(normal.top));
    expect(tester.state(find.byKey(host.portrait)), same(portraitState));
    expect(tester.state(find.byKey(host.transcript)), same(transcriptState));
    expect(tester.state(find.byKey(host.composer)), same(composerState));
    await _click(tester, find.byKey(const ValueKey('converse-artifact-close')));
    _nearRect(tester.getRect(find.byKey(host.portrait)), corner);
    await tester.pump(const Duration(milliseconds: 160));
    expect(tester.getRect(find.byKey(host.portrait)), isNot(corner));
    await tester.pump(const Duration(milliseconds: 161));
    _nearRect(tester.getRect(find.byKey(host.portrait)), normal);
    expect(tester.state(find.byKey(host.portrait)), same(portraitState));
    expect(tester.state(find.byKey(host.transcript)), same(transcriptState));
    expect(tester.state(find.byKey(host.composer)), same(composerState));
    expect(host.disposals, 0);
    expect(tester.takeException(), isNull);
  });

  testWidgets('pin, close and reopen are presentation actions only', (
    tester,
  ) async {
    final host = _Host();
    addTearDown(host.state.dispose);
    await _pump(tester, host);
    final first = artifactFixture(id: 'first');
    final second = artifactFixture(
      id: 'second',
      submitSequence: 2,
      taskId: 'task-second',
    );
    await host.publish(tester, [first]);
    await tester.pumpAndSettle();
    await _click(tester, find.byKey(const ValueKey('converse-artifact-pin')));
    await host.publish(tester, [first, second]);
    expect(host.state.activeArtifact!.artifactId, 'first');
    expect(find.text('Generated result first'), findsOneWidget);
    await _click(tester, find.byKey(const ValueKey('converse-artifact-close')));
    await tester.pumpAndSettle();
    expect(host.state.activeArtifact, isNull);
    await _click(
      tester,
      find.byKey(const ValueKey('converse-artifact-picker')),
    );
    await tester.pumpAndSettle();
    await _click(tester, find.text('Generated result first'));
    await tester.pumpAndSettle();
    expect(host.state.activeArtifact!.artifactId, 'first');
    expect(find.text('Generated result first'), findsOneWidget);
    expect(host.requests.every((request) => request.method == 'GET'), isTrue);
    expect(host.calls, 0);
    expect(host.stops, 0);
  });

  testWidgets('collapse keeps transcript state and history uses its owner', (
    tester,
  ) async {
    final host = _Host();
    addTearDown(host.state.dispose);
    host.state.appendHistory(
      AppView.character,
      ChatMsg(role: 'user', text: 'Earlier literal line'),
    );
    await _pump(tester, host);
    final transcriptState = tester.state(find.byKey(host.transcript));
    await host.publish(tester, [artifactFixture()]);
    await tester.pumpAndSettle();
    await _click(
      tester,
      find.byKey(const ValueKey('artifact-transcript-collapse')),
    );
    expect(
      tester.state(find.byKey(host.transcript, skipOffstage: false)),
      same(transcriptState),
    );
    host.state.appendHistory(
      AppView.character,
      ChatMsg(role: 'kinaiya', text: 'Latest literal transcript'),
    );
    await tester.pump();
    expect(find.text('Latest literal transcript'), findsOneWidget);
    await _click(tester, find.byTooltip('Conversation history'));
    expect(host.histories, 1);
    await _click(
      tester,
      find.byKey(const ValueKey('artifact-transcript-collapse')),
    );
    expect(tester.state(find.byKey(host.transcript)), same(transcriptState));
    expect(find.text('Latest literal transcript'), findsOneWidget);
    expect(host.disposals, 0);
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    'composer retains real keyboard focus, draft and state through layout changes',
    (tester) async {
      final host = _Host();
      addTearDown(host.state.dispose);
      await _pump(tester, host);
      final field = find.byKey(host.composer);
      await tester.ensureVisible(field);
      await tester.enterText(field, 'Unsent draft');
      await tester.pump();
      final editable = tester.widget<EditableText>(
        find.descendant(of: field, matching: find.byType(EditableText)),
      );
      final focus = editable.focusNode;
      final composerState = tester.state(field);
      expect(focus.hasFocus, isTrue);
      await host.publish(tester, [artifactFixture()]);
      await tester.pump(const Duration(milliseconds: 400));
      expect(focus.hasFocus, isTrue);
      expect(tester.state(field), same(composerState));
      tester.view.viewInsets = const FakeViewPadding(bottom: 420);
      await tester.pump();
      await tester.ensureVisible(field);
      await tester.pump();
      expect(focus.hasFocus, isTrue);
      expect(tester.state(field), same(composerState));
      expect(host.harness.currentState!.controller.text, 'Unsent draft');
      expect(tester.takeException(), isNull);
      tester.view.viewInsets = FakeViewPadding.zero;
      await tester.pump();
      host.state.closeArtifact();
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));
      expect(focus.hasFocus, isTrue);
      expect(tester.state(field), same(composerState));
      expect(host.harness.currentState!.controller.text, 'Unsent draft');
      expect(host.disposals, 0);
    },
  );

  testWidgets(
    'closing before the first corner frame always restores normal view',
    (tester) async {
      final host = _Host();
      addTearDown(host.state.dispose);
      host.items = [artifactFixture()];
      await host.state.refreshArtifacts();
      await _pump(tester, host);
      final identity = tester.state(find.byKey(host.portrait));
      host.state.closeArtifact();
      await tester.pump();
      for (var i = 0; i < 5; i++) {
        host.state.appendHistory(
          AppView.character,
          ChatMsg(role: 'user', text: 'Live update $i'),
        );
        await tester.pump(const Duration(milliseconds: 80));
      }
      expect(
        find.byKey(const ValueKey('converse-intelligent-artifact')),
        findsNothing,
      );
      expect(tester.state(find.byKey(host.portrait)), same(identity));
      expect(find.text('Converse'), findsOneWidget);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'new result during return cancels the old transition without remounting',
    (tester) async {
      final host = _Host();
      addTearDown(host.state.dispose);
      await _pump(tester, host);
      final identity = tester.state(find.byKey(host.portrait));
      await host.publish(tester, [artifactFixture(id: 'first')]);
      await tester.pumpAndSettle();
      host.state.closeArtifact();
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 80));
      await host.publish(tester, [
        artifactFixture(id: 'second', submitSequence: 2),
      ]);
      await tester.pumpAndSettle();
      expect(find.text('Generated result second'), findsOneWidget);
      expect(tester.state(find.byKey(host.portrait)), same(identity));
      expect(host.disposals, 0);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'disconnect immediately purges mounted content without remounting portrait',
    (tester) async {
      final host = _Host();
      addTearDown(host.state.dispose);
      await _pump(tester, host);
      final identity = tester.state(find.byKey(host.portrait));
      await host.publish(tester, [
        artifactFixture(
          components: [
            artifactComponent('privateCopy', 'text', {
              'text': 'Prior owner private result',
            }),
          ],
        ),
      ]);
      await tester.pumpAndSettle();
      expect(find.text('Prior owner private result'), findsOneWidget);
      host.state.disconnect();
      await tester.pump();
      expect(find.text('Prior owner private result'), findsNothing);
      expect(
        find.byKey(const ValueKey('converse-intelligent-artifact')),
        findsNothing,
      );
      expect(
        find.byKey(const ValueKey('converse-artifact-picker')),
        findsNothing,
      );
      expect(tester.state(find.byKey(host.portrait)), same(identity));
      expect(host.disposals, 0);
      expect(tester.takeException(), isNull);
    },
  );

  for (final changed in ['session', 'thread', 'epoch']) {
    testWidgets(
      'mounted artifact is purged immediately when $changed changes',
      (tester) async {
        final host = _Host();
        addTearDown(host.state.dispose);
        await _pump(tester, host);
        final identity = tester.state(find.byKey(host.portrait));
        await host.publish(tester, [
          artifactFixture(
            components: [
              artifactComponent('privateCopy', 'text', {
                'text': 'Old scoped artifact',
              }),
            ],
          ),
        ]);
        await tester.pumpAndSettle();
        host.state.converseSession = SessionInfo(
          sessionId: changed == 'session' ? 'next-session' : 'session-test',
          threadId: changed == 'thread' ? 'next-thread' : 'thread-test',
          epoch: changed == 'epoch' ? 2 : 1,
          channel: 'converse',
        );
        host.state.touch();
        await tester.pump();
        // Simulate the frame before the owner store's next scoped refresh: the
        // stage must reject its own cached data and the old result chooser too.
        expect(host.state.activeArtifact, isNotNull);
        expect(find.text('Old scoped artifact'), findsNothing);
        expect(
          find.byKey(const ValueKey('converse-intelligent-artifact')),
          findsNothing,
        );
        expect(
          find.byKey(const ValueKey('converse-artifact-picker')),
          findsNothing,
        );
        expect(tester.state(find.byKey(host.portrait)), same(identity));
        expect(host.disposals, 0);
        expect(tester.takeException(), isNull);
      },
    );
  }

  for (final sample in [
    (const Size(320, 568), 2.0),
    (const Size(1440, 500), 1.0),
    (const Size(1200, 760), 2.0),
  ]) {
    testWidgets('stacked view keeps owner controls reachable at $sample', (
      tester,
    ) async {
      final host = _Host();
      addTearDown(host.state.dispose);
      await _pump(
        tester,
        host,
        size: sample.$1,
        scale: sample.$2,
        reducedMotion: true,
      );
      await host.publish(tester, [artifactFixture()]);
      await tester.pumpAndSettle();
      await _click(tester, find.byKey(const ValueKey('stage-owner-call')));
      await _click(tester, find.byKey(const ValueKey('stage-owner-stop')));
      expect(host.calls, 1);
      expect(host.stops, 1);
      expect(tester.takeException(), isNull);
      await _click(
        tester,
        find.byKey(const ValueKey('converse-artifact-close')),
      );
      await tester.pumpAndSettle();
      expect(
        find.byKey(const ValueKey('converse-intelligent-artifact')),
        findsNothing,
      );
      expect(host.disposals, 0);
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets(
    'reduced motion returns immediately and resize keeps valid portrait bounds',
    (tester) async {
      final host = _Host();
      addTearDown(host.state.dispose);
      await _pump(tester, host, reducedMotion: true);
      await host.publish(tester, [artifactFixture()]);
      await tester.pump();
      expect(
        tester
            .widget<AnimatedPositioned>(
              find.byKey(const ValueKey('artifact-portrait-position')),
            )
            .duration,
        Duration.zero,
      );
      tester.view.physicalSize = const Size(1040, 900);
      await tester.pump();
      await tester.pump();
      final portrait = tester.getRect(find.byKey(host.portrait));
      expect(portrait.left, greaterThanOrEqualTo(0));
      expect(portrait.top, greaterThanOrEqualTo(0));
      expect(portrait.right, lessThanOrEqualTo(1040));
      expect(portrait.bottom, lessThanOrEqualTo(900));
      host.state.closeArtifact();
      await tester.pump();
      await tester.pump();
      expect(
        find.byKey(const ValueKey('converse-intelligent-artifact')),
        findsNothing,
      );
      expect(tester.takeException(), isNull);
    },
  );
}
