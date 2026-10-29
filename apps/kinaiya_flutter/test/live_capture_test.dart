import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/audio/speech_audio.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/character_view.dart';

class ControlledApi extends KinaiyaClient {
  ControlledApi()
    : super(
        sessionBase: 'http://fixture',
        agentBase: 'http://fixture',
        converseBase: 'http://fixture',
        ownerToken: 'synthetic',
      );
  final transcription = Completer<Map<String, Object?>>();
  int interrupts = 0;
  @override
  Future<Map<String, Object?>> converseHealth() async => {'enabled': true};
  @override
  Future<Map<String, Object?>> transcribe(
    List<int> bytes,
    String filename, {
    ContentScope contentScope = ContentScope.localOnly,
  }) => transcription.future;
  @override
  Future<void> interruptSpeech(String id, {int? expectedGeneration}) async {
    interrupts++;
  }
}

class CaptureAudio implements SpeechAudio {
  int clips = 0, stops = 0, epoch = 0;
  final events = <Map<String, Object?>>[];
  @override
  Future<bool> get availableMic async => true;
  @override
  Future<bool> get availablePlayback async => true;
  @override
  Future<String> startArmed() async => 'armed';
  @override
  Future<void> stopArmed() async {}
  @override
  Future<String> beginClip() async {
    clips++;
    epoch++;
    return 'clip-recording';
  }

  @override
  Future<int> get captureEpoch async => epoch;
  @override
  Future<RecordedAudio?> finishClip() async =>
      RecordedAudio('audio/webm', [1, 2, 3]);
  @override
  Future<void> configureVad({required int endSilenceMs}) async {}
  @override
  Future<List<Map<String, Object?>>> pollBridgeEvents() async {
    final result = List<Map<String, Object?>>.of(events);
    events.clear();
    return result;
  }

  @override
  Future<void> stopPlayback() async {
    stops++;
  }

  @override
  Future<bool> queuePcm(String value, int rate) async => true;
  @override
  Future<void> awaitPlayback() async {}
  @override
  Future<bool> get isPlaying async => true;
  @override
  Future<RecordedAudio?> chooseRecording() async => null;
  @override
  Future<String> startRecording() async => 'recording';
  @override
  Future<RecordedAudio?> stopRecording() async => null;
  @override
  Future<void> cancelRecording() async {}
  @override
  Future<void> dispose() async {}
}

void main() {
  testWidgets('pending STT does not pause capture or barge handling', (
    tester,
  ) async {
    final api = ControlledApi();
    final audio = CaptureAudio();
    final state = AppState();
    state.connect(ConnectionProfile.defaults(), injected: api);
    state.converseSession = SessionInfo(
      sessionId: 'converse',
      threadId: 'thread',
      channel: 'converse',
      epoch: 1,
    );
    state.speechEpoch = 3;
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: CharacterView(state: state, audio: audio),
        ),
      ),
    );
    await tester.pump(const Duration(milliseconds: 100));
    await tester.tap(find.byTooltip('Start Live listen (armed mic)'));
    await tester.pump(const Duration(milliseconds: 100));
    expect(audio.clips, 1);
    audio.events.add({'type': 'speech-end', 'epoch': audio.epoch});
    await tester.pump(const Duration(milliseconds: 150));
    expect(
      audio.clips,
      2,
      reason: 'New complete recording begins before transcription resolves',
    );
    expect(api.transcription.isCompleted, isFalse);
    final nonce = state.turnNonce;
    audio.events.add({'type': 'barge', 'epoch': audio.epoch});
    await tester.pump(const Duration(milliseconds: 150));
    expect(audio.stops, greaterThan(0));
    expect(state.turnNonce, greaterThan(nonce));
    expect(api.interrupts, 1);
    api.transcription.complete({'text': ''});
    await tester.pump();
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
  });
}
