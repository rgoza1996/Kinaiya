import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/audio/speech_audio.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/character_view.dart';
import 'package:kinaiya_flutter/ui/moonlit_home.dart';

import 'multiframe_playback_test.dart'
    show CompletionAudio, NdjsonPlaybackTransport, frame, generation, pumpUntil;

class MouthAudio extends CompletionAudio implements SpeechMouthAudio {
  SpeechMouthFrame next = SpeechMouthFrame.closed;
  Completer<SpeechMouthFrame>? pending;
  final cues = <Object?>[];
  int reads = 0;

  @override
  Future<SpeechMouthFrame> mouthFrame() async {
    reads++;
    return pending?.future ?? next;
  }

  @override
  Future<bool> queuePcmWithVisemes(String pcm, int rate, Object? visemes) {
    cues.add(visemes);
    return queuePcm(pcm, rate);
  }
}

class _MouthFixture {
  _MouthFixture(this.tester) {
    final api = KinaiyaClient(
      sessionBase: 'http://fixture',
      agentBase: 'http://fixture',
      converseBase: 'http://fixture',
      ownerToken: 'synthetic-owner',
      httpClient: transport,
    );
    state = AppState()
      ..connect(ConnectionProfile.defaults(), injected: api)
      ..converseSession = SessionInfo(
        sessionId: 'converse',
        threadId: 'thread-converse',
        channel: 'converse',
        epoch: 1,
      );
  }

  final WidgetTester tester;
  final transport = NdjsonPlaybackTransport();
  final audio = MouthAudio();
  late final AppState state;

  ConversePortrait get portrait =>
      tester.widget<ConversePortrait>(find.byType(ConversePortrait));

  Future<void> mount({bool enabled = true}) async {
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: CharacterView(
            key: const ValueKey('lifecycle-voice-owner'),
            state: state,
            audio: audio,
            surface: CharacterSurface.interaction,
            speechMouthMotion: enabled,
          ),
        ),
      ),
    );
    await tester.pump();
  }

  Future<void> start() async {
    tester.view.physicalSize = const Size(1536, 1024);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    audio.next = const SpeechMouthFrame(openness: .8, active: true);
    await mount();
    await state.refreshNotices();
    await pumpUntil(tester, () => transport.speechRequests.isNotEmpty);
    transport.addFrames([frame(24000, 2400, 1, 0)]);
    await pumpUntil(tester, () => audio.queued.isNotEmpty);
    await tester.pump(const Duration(milliseconds: 40));
    await tester.pump();
    expect(portrait.speaking, isTrue);
    expect(portrait.mouthOpenness, .8);
  }

  Future<Completer<SpeechMouthFrame>> beginPendingSample() async {
    final before = audio.reads;
    final pending = audio.pending = Completer<SpeechMouthFrame>();
    await pumpUntil(tester, () => audio.reads > before);
    expect(pending.isCompleted, isFalse);
    return pending;
  }

  Future<void> dispose() async {
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
    await tester.pump();
  }
}

void main() {
  testWidgets('natural source completion closes the mouth and stops sampling', (
    tester,
  ) async {
    final fixture = _MouthFixture(tester);
    await fixture.start();
    final closed = fixture.transport.frames.close();
    await pumpUntil(tester, () => fixture.audio.playbackWaits == 1);
    await closed;
    expect(fixture.portrait.speaking, isTrue);
    expect(
      fixture.transport.deliveries.where(
        (value) => value['status'] == 'played',
      ),
      isEmpty,
      reason: 'End of synthesis is not a source-ended receipt.',
    );
    fixture.audio.ended.complete();
    await pumpUntil(
      tester,
      () => fixture.transport.deliveries.any(
        (value) => value['status'] == 'played',
      ),
    );
    await tester.pump();
    expect(fixture.portrait.speaking, isFalse);
    expect(fixture.portrait.mouthOpenness, 0);
    final reads = fixture.audio.reads;
    await tester.pump(const Duration(milliseconds: 150));
    expect(fixture.audio.reads, reads);
    expect(fixture.audio.stops, 0);
    expect(fixture.transport.interruptions, isEmpty);
    expect(fixture.transport.unexpected, isEmpty);
    expect(tester.takeException(), isNull);
    await fixture.dispose();
  });

  testWidgets(
    'pending sample cannot reopen the mouth after preference opt-out',
    (tester) async {
      final fixture = _MouthFixture(tester);
      await fixture.start();
      final pending = await fixture.beginPendingSample();
      await fixture.mount(enabled: false);
      final reads = fixture.audio.reads;
      pending.complete(
        const SpeechMouthFrame(openness: 1, viseme: 'aa', active: true),
      );
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 150));
      expect(fixture.portrait.mouthOpenness, 0);
      expect(fixture.portrait.viseme, isNull);
      expect(fixture.portrait.speaking, isTrue);
      expect(fixture.audio.reads, reads);
      expect(fixture.audio.stops, 0);
      expect(fixture.transport.interruptions, isEmpty);
      expect(fixture.transport.speechRequests, hasLength(1));
      expect(fixture.state.speechEpoch, generation);
      expect(tester.takeException(), isNull);
      await fixture.dispose();
    },
  );

  testWidgets('disposal fences a pending mouth sample and cancels its timer', (
    tester,
  ) async {
    final fixture = _MouthFixture(tester);
    await fixture.start();
    final pending = await fixture.beginPendingSample();
    await tester.pumpWidget(const SizedBox.shrink());
    final reads = fixture.audio.reads;
    expect(fixture.audio.stops, greaterThan(0));
    pending.complete(
      const SpeechMouthFrame(openness: 1, viseme: 'aa', active: true),
    );
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 150));
    expect(find.byType(ConversePortrait), findsNothing);
    expect(fixture.audio.reads, reads);
    expect(tester.takeException(), isNull);
    fixture.state.dispose();
    await tester.pump();
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    'played mouth samples, silence, cues, preference and reduced motion share one voice owner',
    (tester) async {
      tester.view.physicalSize = const Size(1536, 1024);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final transport = NdjsonPlaybackTransport();
      final api = KinaiyaClient(
        sessionBase: 'http://fixture',
        agentBase: 'http://fixture',
        converseBase: 'http://fixture',
        ownerToken: 'synthetic-owner',
        httpClient: transport,
      );
      final state = AppState()
        ..connect(ConnectionProfile.defaults(), injected: api)
        ..converseSession = SessionInfo(
          sessionId: 'converse',
          threadId: 'thread-converse',
          channel: 'converse',
          epoch: 1,
        );
      final audio = MouthAudio();
      VoiceActivity? voice;
      Future<void> mount({
        bool enabled = true,
        bool reduced = false,
        bool visible = true,
        CharacterSurface surface = CharacterSurface.interaction,
      }) async {
        await tester.pumpWidget(
          MaterialApp(
            home: MediaQuery(
              data: MediaQueryData(disableAnimations: reduced),
              child: TickerMode(
                enabled: visible,
                child: Scaffold(
                  body: CharacterView(
                    key: const ValueKey('same-voice-owner'),
                    state: state,
                    audio: audio,
                    surface: surface,
                    speechMouthMotion: enabled,
                    onActivityChanged: (value) => voice = value,
                  ),
                ),
              ),
            ),
          ),
        );
        await tester.pump();
      }

      ConversePortrait portrait() =>
          tester.widget<ConversePortrait>(find.byType(ConversePortrait));
      await mount();
      await state.refreshNotices();
      await pumpUntil(tester, () => transport.speechRequests.isNotEmpty);
      const visemes = [
        {'start_ms': 0, 'end_ms': 20, 'viseme': 'oh'},
      ];
      transport.addFrames([
        {...frame(24000, 2400, 1, 0), 'visemes': visemes},
      ]);
      await pumpUntil(tester, () => audio.queued.isNotEmpty);
      await tester.pump(const Duration(milliseconds: 40));
      expect(audio.cues.single, visemes);
      expect(portrait().speaking, isTrue);
      expect(portrait().mouthOpenness, 0);

      audio.next = const SpeechMouthFrame(
        openness: .85,
        viseme: 'oh',
        active: true,
      );
      await tester.pump(const Duration(milliseconds: 40));
      await tester.pump();
      expect(portrait().mouthOpenness, .85);
      expect(portrait().viseme, 'oh');
      audio.next = const SpeechMouthFrame(openness: 0, active: true);
      await tester.pump(const Duration(milliseconds: 40));
      await tester.pump();
      expect(portrait().speaking, isTrue);
      expect(
        portrait().mouthOpenness,
        0,
        reason: 'Silence closes during a reply.',
      );

      audio.next = const SpeechMouthFrame(openness: .6, active: true);
      await mount(enabled: false);
      final stopped = audio.stops;
      final reads = audio.reads;
      await tester.pump(const Duration(milliseconds: 100));
      expect(audio.reads, reads);
      expect(portrait().mouthOpenness, 0);
      expect(portrait().speaking, isTrue);
      expect(
        audio.stops,
        stopped,
        reason: 'A presentation change leaves audio alone.',
      );
      expect(transport.speechRequests, hasLength(1));
      expect(state.speechEpoch, generation);

      await mount(reduced: true);
      await tester.pump(const Duration(milliseconds: 50));
      expect(portrait().mouthOpenness, 0);
      await mount();
      await tester.pump(const Duration(milliseconds: 40));
      await tester.pump();
      expect(portrait().mouthOpenness, .6);

      await mount(visible: false);
      final hiddenReads = audio.reads;
      await tester.pump(const Duration(milliseconds: 100));
      expect(audio.reads, hiddenReads);
      expect(portrait().mouthOpenness, 0);
      expect(audio.stops, stopped);
      await mount();
      await tester.pump(const Duration(milliseconds: 40));
      await tester.pump();
      expect(portrait().mouthOpenness, .6);

      // Switching Home/Converse retains the live audio owner, sampling only
      // while the portrait is shown.
      await mount(surface: CharacterSurface.home);
      final homeReads = audio.reads;
      await tester.pump(const Duration(milliseconds: 250));
      expect(audio.reads, homeReads);
      expect(audio.stops, stopped);
      await mount();
      await tester.pump(const Duration(milliseconds: 250));
      await tester.pump();
      expect(portrait().mouthOpenness, .6);

      // A late sampler completion cannot reopen the mouth after Stop speech.
      final pending = audio.pending = Completer<SpeechMouthFrame>();
      await tester.pump(const Duration(milliseconds: 34));
      voice!.stopSpeech!();
      await tester.pump();
      pending.complete(const SpeechMouthFrame(openness: 1, active: true));
      await tester.pump();
      expect(portrait().mouthOpenness, 0);
      expect(audio.stops, greaterThan(stopped));
      expect(
        transport.deliveries.map((value) => value['status']),
        isNot(contains('played')),
      );
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
      await tester.pump();
    },
  );
}
