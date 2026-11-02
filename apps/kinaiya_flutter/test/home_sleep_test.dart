import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/character/package.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/character_view.dart';
import 'package:kinaiya_flutter/ui/anchored_moonlit_scene.dart';
import 'package:kinaiya_flutter/ui/home_sprite_sheet.dart';
import 'package:kinaiya_flutter/ui/moonlit_home.dart';
import 'package:kinaiya_flutter/ui/widgets.dart';

import 'concept_layout_test.dart' show LayoutAudio, task;
import 'presentation_integration_test.dart' show syntheticPackage;

Map<String, Object?> availability(
  bool owner,
  int revision, {
  bool resource = true,
}) => {
  'enabled': owner && resource,
  'owner_enabled': owner,
  'owner_intent_revision': revision,
  'resource_enabled': resource,
};

class SleepApi extends KinaiyaClient {
  SleepApi()
    : super(
        sessionBase: 'http://fixture',
        agentBase: 'http://fixture',
        converseBase: 'http://fixture',
        ownerToken: 'synthetic',
        artifactClientFactory: () => throw StateError(
          'UI artifacts are unavailable in this synthetic sleep fixture',
        ),
      );
  Map<String, Object?> health = availability(true, 0);
  Map<String, Object?> resources = {'configured': false};
  Completer<Map<String, Object?>>? pendingWrite, pendingHealth;
  bool failWrite = false, failRead = false;
  Object? writeError;
  final writes = <bool>[];
  int reads = 0;
  @override
  Future<Map<String, Object?>> converseHealth() async {
    reads++;
    if (failRead) throw StateError('Synthetic unavailable health');
    return pendingHealth?.future ?? health;
  }

  @override
  Future<Map<String, Object?>> resourceStatus() async => resources;
  @override
  Future<Map<String, Object?>> setConverseEnabled(bool enabled) async {
    writes.add(enabled);
    if (writeError != null) throw writeError!;
    if (failWrite) throw StateError('Synthetic lost acknowledgement');
    if (pendingWrite != null) return pendingWrite!.future;
    health = availability(
      enabled,
      (health['owner_intent_revision'] as int) + 1,
    );
    return health;
  }
}

class PendingArmAudio extends LayoutAudio {
  final arm = Completer<String>();
  @override
  Future<String> startArmed() {
    starts++;
    return arm.future;
  }
}

class BufferedSleepAudio extends LayoutAudio {
  final playback = Completer<void>();
  int waiters = 0;
  @override
  Future<void> awaitPlayback() {
    waiters++;
    return playback.future;
  }
}

class SpeechSleepApi extends SleepApi {
  final deliveries = <String>[];
  @override
  Future<List<JournalEntry>> transcript({
    required String sessionId,
    int after = 0,
    int limit = 200,
  }) async => after > 0
      ? []
      : [
          JournalEntry(
            eventId: 'synthetic-result',
            sequence: 1,
            sessionId: 'converse',
            threadId: 'thread',
            epoch: 1,
            kind: 'conversation.converse',
            origin: 'converse',
            payload: {
              'text': 'Synthetic spoken result',
              'generation_epoch': 1,
              'outcome_event_id': 'synthetic-outcome',
            },
          ),
        ];
  @override
  Stream<Map<String, Object?>> speech({
    required String eventId,
    required int generationEpoch,
  }) async* {
    yield {'audio': 'AAAAAA==', 'sample_rate': 24000};
  }

  @override
  Future<void> reportDelivery({
    required String eventId,
    required int generationEpoch,
    required String chunkId,
    required String status,
    double playedSeconds = 0,
  }) async {
    deliveries.add(status);
  }
}

AppState wired(SleepApi api) => AppState()
  ..connect(ConnectionProfile.defaults(), injected: api)
  ..applyConverseHealth(api.health);

Future<void> pumpHome(
  WidgetTester tester,
  AppState state,
  LayoutAudio audio, {
  bool design = true,
  bool showCharacter = true,
  CharacterSelection? character,
}) async {
  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(
        body: ListenableBuilder(
          listenable: state,
          builder: (_, _) => CharacterView(
            state: state,
            audio: audio,
            surface: CharacterSurface.home,
            useDesignScene: design,
            showCharacter: showCharacter,
            character: character,
          ),
        ),
      ),
    ),
  );
  await tester.pump(const Duration(milliseconds: 100));
}

void main() {
  testWidgets('sleeping sprite opens setup and respects reduced motion', (
    tester,
  ) async {
    var opened = 0;
    await tester.pumpWidget(
      MaterialApp(
        home: MediaQuery(
          data: const MediaQueryData(disableAnimations: true),
          child: Scaffold(
            body: MoonlitHome(
              character: const SizedBox(),
              status: 'Unavailable',
              callLabel: 'Talk',
              onSetup: () => opened++,
              offlineResting: true,
            ),
          ),
        ),
      ),
    );
    await tester.pump();
    final sprite = find.byKey(const ValueKey('home-sleeping-artwork'));
    expect(sprite, findsOneWidget);
    await tester.tap(sprite);
    await tester.pump(const Duration(seconds: 2));
    expect(opened, 1);
    expect(find.byKey(const ValueKey('home-sleep-transition')), findsNothing);
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('home pose follows Converse independently of Agent', (
    tester,
  ) async {
    final api = SleepApi();
    final state = wired(api);
    final audio = LayoutAudio();
    state.agentOnline = false;
    state.converseOnline = true;
    await pumpHome(tester, state, audio);
    expect(find.byKey(const ValueKey('home-sleeping-artwork')), findsNothing);

    state.agentOnline = true;
    state.converseOnline = false;
    await pumpHome(tester, state, audio);
    expect(find.byKey(const ValueKey('home-sleep-transition')), findsOneWidget);
    await tester.pump(const Duration(milliseconds: 900));
    expect(
      tester
          .widget<HomeSpriteSheet>(
            find.byKey(const ValueKey('home-sleep-transition')),
          )
          .frame,
      greaterThan(0),
    );
    await tester.pump(const Duration(milliseconds: 400));
    expect(
      tester
          .widget<HomeSpriteSheet>(
            find.byKey(const ValueKey('home-sleep-transition')),
          )
          .asset,
      'assets/character/kinaiya-sleeping-idle-v3.png',
    );
    await tester.pump(const Duration(milliseconds: 1400));
    expect(find.byKey(const ValueKey('home-sleeping-artwork')), findsOneWidget);
    expect(find.text('Converse unavailable'), findsOneWidget);
    expect(state.converseSleeping, isFalse);
    expect(api.writes, isEmpty);

    state.converseOnline = true;
    state.agentOnline = false;
    await pumpHome(tester, state, audio);
    expect(find.byKey(const ValueKey('home-sleep-transition')), findsOneWidget);
    await tester.pump(const Duration(milliseconds: 900));
    expect(
      tester
          .widget<HomeSpriteSheet>(
            find.byKey(const ValueKey('home-sleep-transition')),
          )
          .frame,
      lessThan(3),
    );
    await tester.pump(const Duration(milliseconds: 1800));
    expect(find.byKey(const ValueKey('home-sleeping-artwork')), findsNothing);
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
  });

  test(
    'owner off differs from handoff, legacy effective off, and disconnect',
    () async {
      final api = SleepApi();
      final state = wired(api);
      state.applyConverseHealth(availability(true, 1, resource: false));
      expect(state.converseEnabled, isFalse);
      expect(state.converseSleeping, isFalse);
      expect(state.converseOwnerEnabled, isTrue);
      state.applyConverseHealth(availability(false, 2));
      expect(state.converseSleeping, isTrue);
      state.applyConverseHealth({'enabled': false});
      expect(state.converseSleeping, isFalse);
      expect(state.converseOwnerControlAvailable, isFalse);
      state.applyConverseHealth(availability(false, 2));
      expect(state.converseSleeping, isTrue);
      state.disconnect();
      expect(state.converseSleeping, isFalse);
      expect(state.converseOwnerEnabled, isNull);
      state.dispose();
    },
  );

  testWidgets(
    'timeout plus opposite read remains unconfirmed until original POST completes',
    (tester) async {
      final api = SleepApi()..pendingWrite = Completer<Map<String, Object?>>();
      final state = wired(api);
      final operation = state.setConverseEnabled(false);
      await tester.pump(const Duration(seconds: 9));
      await operation;
      expect(state.converseControlUncertain, isTrue);
      expect(state.converseOwnerEnabled, isTrue);
      expect(state.converseControlError, contains('unconfirmed'));
      await state.setConverseEnabled(false);
      expect(api.writes, [false]);
      api.pendingWrite!.complete(availability(false, 1));
      await tester.pump();
      expect(state.converseControlUncertain, isFalse);
      expect(state.converseSleeping, isTrue);
      expect(api.writes, [false]);
      state.dispose();
    },
  );

  testWidgets(
    'late explicit rejection after timeout revalidates without another POST',
    (tester) async {
      final api = SleepApi()..pendingWrite = Completer<Map<String, Object?>>();
      final state = wired(api);
      final operation = state.setConverseEnabled(false);
      await tester.pump(const Duration(seconds: 9));
      await operation;
      expect(state.converseControlUncertain, isTrue);
      expect(api.reads, 1);
      api.pendingWrite!.completeError(
        ApiException(403, 'Synthetic explicit rejection'),
      );
      await tester.pump();
      expect(api.reads, 2);
      expect(state.converseControlUncertain, isFalse);
      expect(state.converseOwnerControlAvailable, isTrue);
      expect(state.converseOwnerEnabled, isTrue);
      expect(state.converseEnabled, isTrue);
      expect(state.converseSleeping, isFalse);
      expect(api.writes, [false]);
      state.dispose();
    },
  );

  testWidgets(
    'Sleep invalidates a pending microphone start without late rearm',
    (tester) async {
      final api = SleepApi();
      final state = wired(api);
      final audio = PendingArmAudio();
      await pumpHome(tester, state, audio);
      await tester.ensureVisible(find.byKey(const ValueKey('call-kinaiya')));
      await tester.tap(find.byKey(const ValueKey('call-kinaiya')));
      await tester.pump();
      expect(audio.starts, 1);
      await tester.ensureVisible(
        find.byKey(const ValueKey('home-owner-control')),
      );
      await tester.tap(find.byKey(const ValueKey('home-owner-control')));
      await tester.pump(const Duration(milliseconds: 100));
      audio.arm.complete('armed');
      await tester.pump(const Duration(milliseconds: 200));
      expect(audio.clips, 0);
      expect(find.text('Kinaiya is sleeping'), findsOneWidget);
      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
    },
  );

  testWidgets(
    'Sleep stops TTS and a late playback completion cannot report played after Wake',
    (tester) async {
      final api = SpeechSleepApi();
      final state = wired(api)
        ..converseSession = SessionInfo(
          sessionId: 'converse',
          threadId: 'thread',
          channel: 'converse',
          epoch: 1,
        );
      final audio = BufferedSleepAudio();
      await pumpHome(tester, state, audio);
      await state.refreshNotices();
      await tester.pump();
      expect(audio.waiters, 1);
      expect(api.deliveries, ['playing']);
      final stops = audio.stops;
      await tester.ensureVisible(
        find.byKey(const ValueKey('home-owner-control')),
      );
      await tester.tap(find.byKey(const ValueKey('home-owner-control')));
      await tester.pump(const Duration(milliseconds: 100));
      expect(audio.stops, greaterThan(stops));
      expect(
        find.text('Synthetic spoken result'),
        findsNothing,
        reason: 'sleeping panel suppresses captions',
      );
      await tester.ensureVisible(find.text('Wake Kinaiya'));
      await tester.tap(find.text('Wake Kinaiya'));
      await tester.pump();
      audio.playback.complete();
      await tester.pump();
      expect(api.deliveries, isNot(contains('played')));
      expect(api.deliveries, contains('interrupted'));
      expect(audio.starts, 0);
      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
    },
  );

  testWidgets(
    'sleep respects custom scenery and hidden-character preferences',
    (tester) async {
      final api = SleepApi()..health = availability(false, 1);
      final state = wired(api);
      final audio = LayoutAudio();
      final custom = await tester.runAsync(
        () => inspectCharacterPackage(syntheticPackage()),
      );
      await pumpHome(tester, state, audio, design: false, character: custom);
      expect(find.byKey(const ValueKey('home-sleeping-artwork')), findsNothing);
      expect(find.byType(MascotWidget), findsOneWidget);
      await pumpHome(
        tester,
        state,
        audio,
        showCharacter: false,
        character: custom,
      );
      expect(find.byKey(const ValueKey('home-sleeping-artwork')), findsNothing);
      expect(find.byType(MascotWidget), findsNothing);
      expect(find.text('Wake Kinaiya'), findsOneWidget);
      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
    },
  );

  test(
    'authenticated resource intent requires its owner revision and cannot retreat',
    () async {
      final api = SleepApi();
      final state = AppState()
        ..connect(ConnectionProfile.defaults(), injected: api);
      Map<String, Object?> resources(
        Object? ownerRevision,
        bool desired,
        int revision,
      ) => {
        'controller_id': 'synthetic-controller',
        'policy': {
          'phase': 'off',
          'revision': revision,
          'desired_converse': desired,
          if (ownerRevision != null) 'owner_intent_revision': ownerRevision,
        },
      };
      api.resources = resources(null, false, 1);
      await state.refreshResources();
      expect(state.converseSleeping, isFalse);
      api.resources = resources(3, false, 2);
      await state.refreshResources();
      expect(state.converseSleeping, isTrue);
      api.resources = resources(2, true, 3);
      await state.refreshResources();
      expect(state.resourceStatusStale, isTrue);
      expect(state.converseSleeping, isFalse);
      api.resources = resources(4, true, 4);
      await state.refreshResources();
      expect(state.converseOwnerEnabled, isTrue);
      expect(
        state.converseEnabled,
        isFalse,
        reason: 'owner on does not establish worker readiness',
      );
      state.applyConverseHealth(availability(false, 3));
      expect(state.converseOwnerEnabled, isTrue);
      state.dispose();
    },
  );

  test(
    'ready resource snapshot without owner identity cannot override confirmed off',
    () async {
      final api = SleepApi()..health = availability(false, 3);
      final state = wired(api);
      api.resources = {
        'controller_id': 'new',
        'policy': {'revision': 1, 'phase': 'ready', 'desired_converse': true},
      };
      await state.refreshResources();
      expect(state.converseEnabled, isFalse);
      expect(state.converseSleeping, isTrue);
      expect(state.converseOwnerEnabled, isFalse);
      state.dispose();
    },
  );

  test(
    'restoration needs fresh native health and rechecks unchanged ready resources',
    () async {
      final api = SleepApi()..health = availability(true, 3, resource: false);
      final state = wired(api);
      api.resources = {
        'controller_id': 'new',
        'policy': {
          'revision': 1,
          'phase': 'ready',
          'desired_converse': true,
          'owner_intent_revision': 3,
        },
      };
      await state.refreshResources();
      expect(state.converseEnabled, isFalse);
      expect(api.reads, 1);
      api.health = availability(true, 3);
      await state.refreshResources();
      expect(api.reads, 2);
      expect(state.converseEnabled, isTrue);
      expect(state.converseSleeping, isFalse);
      state.dispose();
    },
  );

  test(
    'malformed owner health fails closed without treating effective off as sleep',
    () {
      final state = wired(SleepApi());
      expect(
        () => state.applyConverseHealth({
          'enabled': false,
          'owner_enabled': false,
          'owner_intent_revision': 'invalid',
          'resource_enabled': true,
        }),
        throwsFormatException,
      );
      expect(state.converseEnabled, isFalse);
      expect(state.converseSleeping, isFalse);
      expect(state.converseOwnerControlAvailable, isFalse);
      state.dispose();
    },
  );

  test(
    'native owner POST retains intent when effective availability remains off',
    () async {
      final requests = <http.Request>[];
      final api = KinaiyaClient(
        sessionBase: 'http://fixture',
        agentBase: 'http://fixture',
        converseBase: 'http://fixture',
        ownerToken: 'synthetic-owner',
        httpClient: MockClient((request) async {
          requests.add(request);
          return http.Response(
            jsonEncode(
              availability(
                (jsonDecode(request.body) as Map)['enabled'] as bool,
                requests.length,
                resource: false,
              ),
            ),
            200,
          );
        }),
      );
      final state = AppState()
        ..connect(ConnectionProfile.defaults(), injected: api)
        ..applyConverseHealth(availability(true, 0));
      await state.setConverseEnabled(false);
      expect(state.converseSleeping, isTrue);
      await state.setConverseEnabled(true);
      expect(requests.length, 2);
      for (final request in requests) {
        expect(request.method, 'POST');
        expect(request.url.path, '/v1/converse/enabled');
        expect(request.headers['Authorization'], 'Bearer synthetic-owner');
      }
      expect(jsonDecode(requests.first.body), {'enabled': false});
      expect(jsonDecode(requests.last.body), {'enabled': true});
      expect(state.converseOwnerEnabled, isTrue);
      expect(state.converseEnabled, isFalse);
      expect(state.converseSleeping, isFalse);
      state.dispose();
    },
  );

  test(
    'one pending mutation, failed acknowledgement reconciles without another POST',
    () async {
      final api = SleepApi()..pendingWrite = Completer<Map<String, Object?>>();
      final state = wired(api);
      final operation = state.setConverseEnabled(false);
      expect(state.converseControlPending, isTrue);
      expect(state.converseSleeping, isFalse);
      await state.setConverseEnabled(false);
      expect(api.writes, [false]);
      api.health = availability(false, 1);
      api.pendingWrite!.completeError(StateError('Synthetic lost response'));
      await operation;
      expect(api.reads, 1);
      expect(api.writes, [false]);
      expect(state.converseSleeping, isTrue);
      expect(state.converseControlError, isNull);
      state.dispose();
    },
  );

  test(
    'unresolved write blocks retries until explicit read succeeds',
    () async {
      final api = SleepApi()
        ..failWrite = true
        ..failRead = true;
      final state = wired(api);
      await state.setConverseEnabled(false);
      expect(state.converseControlUncertain, isTrue);
      expect(state.converseControlError, contains('unconfirmed'));
      expect(state.converseSleeping, isFalse);
      await state.setConverseEnabled(false);
      expect(api.writes, [false]);
      api.failRead = false;
      await state.refreshConverseAvailability();
      expect(
        state.converseControlUncertain,
        isTrue,
        reason: 'opposite snapshot cannot rule out late application',
      );
      api.health = availability(false, 1);
      await state.refreshConverseAvailability();
      expect(state.converseControlUncertain, isFalse);
      expect(state.converseControlError, isNull);
      expect(state.converseOwnerControlAvailable, isTrue);
      expect(api.writes, [false]);
      state.dispose();
    },
  );

  test(
    'a rejected write reports unchanged owner state instead of sleep',
    () async {
      final api = SleepApi()
        ..writeError = ApiException(403, 'Synthetic rejected');
      final state = wired(api);
      await state.setConverseEnabled(false);
      expect(state.converseSleeping, isFalse);
      expect(state.converseOwnerEnabled, isTrue);
      expect(state.converseControlError, contains('was not applied'));
      expect(state.converseEnabled, isTrue);
      expect(api.writes, [false]);
      state.dispose();
    },
  );

  test(
    'late write and read cannot overwrite a replacement connection',
    () async {
      final oldApi = SleepApi()
        ..pendingWrite = Completer<Map<String, Object?>>();
      final state = wired(oldApi);
      final operation = state.setConverseEnabled(false);
      final next = SleepApi();
      state.connect(ConnectionProfile.defaults(), injected: next);
      state.applyConverseHealth(next.health);
      oldApi.pendingWrite!.complete(availability(false, 1));
      await operation;
      expect(state.converseOwnerEnabled, isTrue);
      expect(state.converseControlPending, isFalse);
      next.pendingHealth = Completer<Map<String, Object?>>();
      final read = state.refreshConverseAvailability();
      state.disconnect();
      next.pendingHealth!.complete(availability(false, 9));
      await read;
      expect(state.converseOwnerEnabled, isNull);
      expect(state.converseSleeping, isFalse);
      state.dispose();
    },
  );

  testWidgets(
    'Sleep cancels current mic and playback; Wake never rearms or changes Agent',
    (tester) async {
      final api = SleepApi();
      final state = wired(api)
        ..agentSession = SessionInfo(
          sessionId: 'agent',
          threadId: 'agent-thread',
          channel: 'agent',
          epoch: 1,
        )
        ..agentDraft = 'Synthetic Agent draft'
        ..converseDraft = 'Synthetic Converse draft'
        ..tasks = [task('work', 'running')]
        ..busyAgent = true;
      final history = ChatMsg(
        role: 'kinaiya',
        text: 'Synthetic retained history',
      );
      state.converseHistory.add(history);
      final audio = LayoutAudio();
      await pumpHome(tester, state, audio);
      await tester.ensureVisible(find.byKey(const ValueKey('call-kinaiya')));
      await tester.tap(find.byKey(const ValueKey('call-kinaiya')));
      await tester.pump(const Duration(milliseconds: 100));
      expect(audio.starts, 1);
      final stops = audio.stops;
      await tester.ensureVisible(
        find.byKey(const ValueKey('home-owner-control')),
      );
      await tester.tap(find.byKey(const ValueKey('home-owner-control')));
      await tester.pump(const Duration(milliseconds: 100));
      expect(api.writes, [false]);
      expect(audio.micStops, greaterThan(0));
      expect(audio.recordCancels, greaterThan(0));
      expect(audio.stops, greaterThan(stops));
      expect(find.text('Kinaiya is sleeping'), findsOneWidget);
      expect(find.text('Converse is offline'), findsOneWidget);
      expect(find.byType(MascotWidget), findsNothing);
      expect(state.converseHistory, contains(same(history)));
      await tester.ensureVisible(find.text('Wake Kinaiya'));
      await tester.tap(find.text('Wake Kinaiya'));
      await tester.pump(const Duration(milliseconds: 100));
      expect(api.writes, [false, true]);
      expect(audio.starts, 1);
      expect(state.agentSession!.sessionId, 'agent');
      expect(state.agentDraft, 'Synthetic Agent draft');
      expect(state.converseDraft, 'Synthetic Converse draft');
      expect(state.tasks.single.state, 'running');
      expect(state.busyAgent, isTrue);
      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
    },
  );

  testWidgets(
    'unconfirmed local stop never paints sleeping or automatically starts capture',
    (tester) async {
      final api = SleepApi();
      final state = wired(api);
      final audio = LayoutAudio()..rejectStop = true;
      await pumpHome(tester, state, audio);
      await tester.ensureVisible(
        find.byKey(const ValueKey('home-owner-control')),
      );
      await tester.tap(find.byKey(const ValueKey('home-owner-control')));
      await tester.pump(const Duration(milliseconds: 100));
      expect(
        state.converseSleeping,
        isTrue,
        reason: 'server owner-off is confirmed',
      );
      expect(find.byKey(const ValueKey('home-sleeping-artwork')), findsNothing);
      expect(find.text('Microphone stop unconfirmed'), findsOneWidget);
      expect(audio.starts, 0);
      audio.rejectStop = false;
      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
    },
  );

  for (final size in [
    const Size(1204, 656),
    const Size(1280, 720),
    const Size(1536, 1024),
    const Size(320, 500),
    const Size(320, 320),
  ]) {
    testWidgets(
      'sleeping scene contains whole artwork and reachable controls at $size',
      (tester) async {
        tester.view.physicalSize = size;
        tester.view.devicePixelRatio = 1;
        addTearDown(tester.view.resetPhysicalSize);
        addTearDown(tester.view.resetDevicePixelRatio);
        await tester.pumpWidget(
          MaterialApp(
            home: MediaQuery(
              data: MediaQueryData(
                size: size,
                textScaler: TextScaler.linear(size.width == 320 ? 2 : 1),
              ),
              child: Scaffold(
                body: MoonlitHome(
                  character: const Text('No duplicate character'),
                  status: 'Offline · Sleeping',
                  callLabel: 'Talk with Kinaiya',
                  onSetup: () {},
                  sleeping: true,
                  ownerControl: FilledButton(
                    onPressed: () {},
                    child: const Text('Wake Kinaiya'),
                  ),
                ),
              ),
            ),
          ),
        );
        await tester.pump(const Duration(milliseconds: 100));
        final artwork = find.byKey(const ValueKey('home-sleeping-artwork'));
        expect(
          tester.widget<HomeSpriteSheet>(artwork).asset,
          'assets/character/kinaiya-sleeping-idle-v3.png',
        );
        expect(tester.getSize(artwork).width.isFinite, isTrue);
        expect(tester.getSize(artwork).height, greaterThan(0));
        expect(find.byType(AnchoredMoonlitScene), findsOneWidget);
        expect(
          find.byKey(const ValueKey('moonlit-illustration')),
          findsOneWidget,
        );
        expect(find.text('No duplicate character'), findsNothing);
        expect(find.byIcon(Icons.graphic_eq_rounded), findsNothing);
        await tester.ensureVisible(find.text('Wake Kinaiya'));
        await tester.pump();
        expect(find.text('Wake Kinaiya').hitTestable(), findsOneWidget);
        expect(tester.takeException(), isNull);
        await tester.pumpWidget(const SizedBox.shrink());
      },
    );
  }
}
