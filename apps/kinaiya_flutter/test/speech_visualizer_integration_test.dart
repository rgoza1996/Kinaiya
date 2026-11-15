import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/audio/speech_audio.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/state/native_audio_clip.dart';
import 'package:kinaiya_flutter/ui/character_view.dart';
import 'package:kinaiya_flutter/ui/converse_interaction.dart';

import 'multiframe_playback_test.dart'
    show CompletionAudio, NdjsonPlaybackTransport, frame, pumpUntil;
import 'native_audio_clip_test.dart' show NativeClipApi, NativeClipAudio, wav;
import 'input_disclosure_test.dart' show withClient;

class _LevelAudio extends CompletionAudio implements SpeechPlaybackAudio {
  SpeechPlaybackFrame next = const SpeechPlaybackFrame(rms: .04, active: true);
  Completer<SpeechPlaybackFrame>? pending;
  int reads = 0;
  Completer<bool>? playing;

  @override
  Future<bool> get isPlaying async => playing?.future ?? super.isPlaying;

  @override
  Future<SpeechPlaybackFrame> playbackFrame() async {
    reads++;
    return pending?.future ?? next;
  }
}

class _NativeLevelAudio extends NativeClipAudio implements SpeechPlaybackAudio {
  int reads = 0;
  @override
  Future<SpeechPlaybackFrame> playbackFrame() async {
    reads++;
    return const SpeechPlaybackFrame(rms: .12, active: true);
  }
}

class _Fixture {
  _Fixture(this.tester) {
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
  final audio = _LevelAudio();
  late final AppState state;
  VoiceActivity? voice;

  List<double> get levels => tester
      .widget<ConverseInteraction>(find.byType(ConverseInteraction))
      .speechLevels!
      .value;

  Future<void> mount({
    bool mouth = false,
    bool motion = true,
    bool visible = true,
    bool reduced = false,
    CharacterSurface surface = CharacterSurface.interaction,
  }) async {
    await tester.pumpWidget(
      MaterialApp(
        builder: (context, child) => MediaQuery(
          data: MediaQuery.of(context).copyWith(disableAnimations: reduced),
          child: child!,
        ),
        home: Scaffold(
          body: TickerMode(
            enabled: visible,
            child: CharacterView(
              key: const ValueKey('speech-level-owner'),
              state: state,
              audio: audio,
              surface: surface,
              showCharacter: false,
              speechMouthMotion: mouth,
              animateTransitions: motion,
              onActivityChanged: (value) => voice = value,
            ),
          ),
        ),
      ),
    );
    await tester.pump();
  }

  Future<void> start() async {
    tester.view.physicalSize = const Size(1280, 900);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await mount();
    await state.refreshNotices();
    await pumpUntil(tester, () => transport.speechRequests.isNotEmpty);
    expect(
      audio.reads,
      0,
      reason: 'Requested/queued text is not played audio.',
    );
    transport.addFrames([frame(24000, 2400, 1, 0)]);
    await pumpUntil(tester, () => audio.queued.isNotEmpty);
    await tester.pump(const Duration(milliseconds: 40));
    await tester.pump();
    expect(voice?.speaking, isTrue);
    expect(levels.last, .04);
  }

  Future<Completer<SpeechPlaybackFrame>> pendingSample() async {
    final before = audio.reads;
    final result = audio.pending = Completer<SpeechPlaybackFrame>();
    await pumpUntil(tester, () => audio.reads > before);
    return result;
  }

  Future<void> dispose() async {
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
    await tester.pump();
    expect(tester.takeException(), isNull);
  }
}

void main() {
  testWidgets(
    'a delayed playing check cannot adopt a replacement source identity',
    (tester) async {
      final f = _Fixture(tester);
      f.audio.playing = Completer<bool>();
      await f.mount();
      await f.state.refreshNotices();
      await pumpUntil(tester, () => f.transport.speechRequests.isNotEmpty);
      f.transport.addFrames([frame(24000, 2400, 1, 0)]);
      await pumpUntil(tester, () => f.audio.queued.isNotEmpty);
      f.state.nativeAudioSourceGeneration++;
      f.audio.playing!.complete(true);
      await tester.pump(const Duration(milliseconds: 100));
      expect(f.audio.reads, 0);
      expect(f.levels, everyElement(0));
      await f.dispose();
    },
  );
  testWidgets(
    'admitted native PCM shares output levels without arming the microphone',
    (tester) async {
      tester.view.physicalSize = const Size(1280, 900);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final api = NativeClipApi();
      final audio = _NativeLevelAudio();
      final state = withClient(api, BackendProfile.clean);
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: AnimatedBuilder(
              animation: state,
              builder: (_, _) => CharacterView(
                state: state,
                audio: audio,
                surface: CharacterSurface.interaction,
                showCharacter: false,
                speechMouthMotion: false,
              ),
            ),
          ),
        ),
      );
      await tester.pump(const Duration(milliseconds: 100));
      final import = find.byTooltip(nativeAudioImportHint);
      await tester.ensureVisible(import);
      await tester.tap(import);
      audio.chosen.complete(
        RecordedAudio('audio/wav', wav(), filename: 'public-synthetic.wav'),
      );
      await tester.pump(const Duration(milliseconds: 100));
      api.exposeEvents = true;
      await state.refreshNotices();
      await tester.pump(const Duration(milliseconds: 50));
      expect(audio.reads, 0);
      expect(
        audio.queued,
        0,
        reason: 'Unconfirmed native admission cannot produce output.',
      );
      api.admission.complete(NativeAudioClipAdmission(api.original!, false));
      await tester.pump(const Duration(milliseconds: 100));
      expect(audio.queued, 1);
      expect(audio.armed, 0);
      final levels = tester
          .widget<ConverseInteraction>(find.byType(ConverseInteraction))
          .speechLevels!;
      expect(levels.value.last, .12);
      expect(api.delivery, contains('playing'));
      expect(api.delivery, isNot(contains('played')));
      audio.playback.complete();
      await tester.pump(const Duration(milliseconds: 100));
      expect(levels.value, everyElement(0));
      expect(api.delivery, contains('played'));
      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
      await tester.pump();
      expect(tester.takeException(), isNull);
    },
  );
  testWidgets(
    'PCM level history is bounded, varies, and quiets in silence with mouth disabled',
    (tester) async {
      final f = _Fixture(tester);
      await f.start();
      for (var i = 0; i < 20; i++) {
        f.audio.next = SpeechPlaybackFrame(
          rms: i.isEven ? .08 : .2,
          active: true,
        );
        await tester.pump(const Duration(milliseconds: 34));
      }
      expect(f.levels, hasLength(13));
      expect(f.levels.toSet(), containsAll([.08, .2]));
      f.audio.next = const SpeechPlaybackFrame(rms: 0, active: true);
      await tester.pump(const Duration(milliseconds: 34));
      expect(f.levels, everyElement(0));
      expect(f.voice?.speaking, isTrue);
      expect(f.audio.clips, 0);
      expect(f.audio.stops, 0);
      expect(f.transport.speechRequests, hasLength(1));
      expect(
        f.transport.deliveries.where((d) => d['status'] == 'played'),
        isEmpty,
      );
      await f.dispose();
    },
  );

  for (final lateError in [false, true]) {
    testWidgets(
      'timed-out level read keeps one owner until late error=$lateError settles',
      (tester) async {
        final f = _Fixture(tester);
        await f.start();
        final pending = await f.pendingSample();
        final reads = f.audio.reads;
        for (var i = 0; i < 30; i++) {
          await tester.pump(const Duration(milliseconds: 34));
        }
        expect(f.audio.reads, reads);
        expect(f.levels, everyElement(0));
        f.audio.pending = null;
        f.audio.next = const SpeechPlaybackFrame(rms: .1, active: true);
        if (lateError) {
          pending.completeError(
            StateError('synthetic late output meter failure'),
          );
        } else {
          pending.complete(const SpeechPlaybackFrame(rms: 1, active: true));
        }
        await tester.pump();
        expect(f.levels, everyElement(0));
        await tester.pump(const Duration(milliseconds: 34));
        expect(f.levels.last, .1);
        expect(f.audio.stops, 0);
        await f.dispose();
      },
    );
  }

  testWidgets(
    'reduced motion and hidden presentation stop reads without stopping speech',
    (tester) async {
      final f = _Fixture(tester);
      await f.start();
      for (final change in [
        () => f.mount(motion: false),
        () => f.mount(visible: false),
        () => f.mount(reduced: true),
      ]) {
        await change();
        final reads = f.audio.reads;
        await tester.pump(const Duration(milliseconds: 250));
        expect(f.audio.reads, reads);
        expect(f.levels, everyElement(0));
        expect(f.voice?.speaking, isTrue);
        expect(f.audio.stops, 0);
        await f.mount(mouth: true);
        await tester.pump(const Duration(milliseconds: 40));
        expect(f.levels.last, .04);
      }
      await f.mount(surface: CharacterSurface.home);
      final reads = f.audio.reads;
      await tester.pump(const Duration(milliseconds: 250));
      expect(f.audio.reads, reads);
      expect(f.audio.stops, 0);
      await f.dispose();
    },
  );

  for (final identity in ['turn', 'speech', 'session', 'source']) {
    testWidgets(
      '$identity invalidation quiets pending and future old-owner samples',
      (tester) async {
        final f = _Fixture(tester);
        await f.start();
        final pending = await f.pendingSample();
        switch (identity) {
          case 'turn':
            f.state.turnNonce++;
          case 'speech':
            f.state.speechEpoch++;
          case 'session':
            f.state.converseSession = SessionInfo(
              sessionId: 'next-session',
              threadId: 'next-thread',
              channel: 'converse',
              epoch: 2,
            );
          case 'source':
            f.state.nativeAudioSourceGeneration++;
        }
        await tester.pump(const Duration(milliseconds: 40));
        pending.complete(const SpeechPlaybackFrame(rms: 1, active: true));
        await tester.pump();
        final reads = f.audio.reads;
        await tester.pump(const Duration(milliseconds: 250));
        expect(f.audio.reads, reads);
        expect(f.levels, everyElement(0));
        await f.dispose();
      },
    );
  }

  testWidgets('Stop speech quiets pending levels without inventing delivery', (
    tester,
  ) async {
    final f = _Fixture(tester);
    await f.start();
    final pending = await f.pendingSample();
    f.voice!.stopSpeech!();
    await tester.pump();
    pending.complete(const SpeechPlaybackFrame(rms: 1, active: true));
    await tester.pump(const Duration(milliseconds: 150));
    expect(f.levels, everyElement(0));
    expect(f.audio.stops, greaterThan(0));
    expect(
      f.transport.deliveries.where((d) => d['status'] == 'played'),
      isEmpty,
    );
    await f.dispose();
  });

  testWidgets('natural source end quiets levels and stops sampling', (
    tester,
  ) async {
    final f = _Fixture(tester);
    await f.start();
    final closed = f.transport.frames.close();
    await pumpUntil(tester, () => f.audio.playbackWaits == 1);
    await closed;
    expect(f.voice?.speaking, isTrue);
    f.audio.ended.complete();
    await pumpUntil(tester, () => f.voice?.speaking == false);
    expect(f.levels, everyElement(0));
    final reads = f.audio.reads;
    await tester.pump(const Duration(milliseconds: 150));
    expect(f.audio.reads, reads);
    await f.dispose();
  });

  testWidgets('disposing the playback owner rejects a late level read', (
    tester,
  ) async {
    final f = _Fixture(tester);
    await f.start();
    final pending = await f.pendingSample();
    await f.dispose();
    final reads = f.audio.reads;
    pending.complete(const SpeechPlaybackFrame(rms: 1, active: true));
    await tester.pump(const Duration(milliseconds: 150));
    expect(f.audio.reads, reads);
    expect(tester.takeException(), isNull);
  });
}
