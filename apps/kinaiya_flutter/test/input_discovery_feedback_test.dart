// Requirement-first typed input feedback and native timing presentation.
// These are UI/protocol fixtures, not actual device permission or microphone proof.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/audio/speech_audio.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/character_view.dart';
import 'package:kinaiya_flutter/ui/voice_options.dart';

import 'live_capture_test.dart' show CaptureAudio;

class _IdentifiersUnavailableAudio extends CaptureAudio
    implements AudioInputSelection {
  int discoveries = 0, arms = 0, recordings = 0, finishes = 0;
  int imports = 0, queues = 0, selections = 0;

  @override
  Future<List<AudioInputDevice>> audioInputs() async {
    discoveries++;
    throw const AudioInputDiscoveryUnavailable(
      AudioInputDiscoveryReason.identifiersUnavailable,
    );
  }

  @override
  Future<String> selectAudioInput(String opaqueId) async {
    selections++;
    return 'selected';
  }

  @override
  Future<String> useDefaultAudioInput() async {
    selections++;
    return 'default';
  }

  @override
  Future<String> startArmed() async {
    arms++;
    return 'armed';
  }

  @override
  Future<String> startRecording() async {
    recordings++;
    return 'recording';
  }

  @override
  Future<RecordedAudio?> finishClip() async {
    finishes++;
    return null;
  }

  @override
  Future<RecordedAudio?> chooseRecording() async {
    imports++;
    return null;
  }

  @override
  Future<bool> queuePcm(String value, int rate) async {
    queues++;
    return true;
  }

  @override
  Future<bool> get isPlaying async => false;
}

String _visibleSheetText(WidgetTester tester) => tester
    .widgetList<Text>(find.byType(Text))
    .map((widget) => widget.data ?? widget.textSpan?.toPlainText() ?? '')
    .join('\n');

void main() {
  testWidgets(
    'real Refresh reports unavailable identifiers without warming capture',
    (tester) async {
      final audio = _IdentifiersUnavailableAudio();
      final state = AppState();
      try {
        await tester.pumpWidget(
          MaterialApp(
            home: Scaffold(
              body: CharacterView(state: state, audio: audio),
            ),
          ),
        );
        await tester.pump(const Duration(milliseconds: 100));
        final options = find.byKey(const ValueKey('voice-options'));
        await tester.ensureVisible(options);
        await tester.tap(options);
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 300));
        final refresh = find.byKey(const ValueKey('audio-input-refresh'));
        await tester.ensureVisible(refresh);
        await tester.pump();
        expect(refresh.hitTestable(), findsOneWidget);
        final beforeRefresh = audio.discoveries;
        await tester.tap(refresh);
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 100));

        expect(audio.discoveries, greaterThan(beforeRefresh));
        expect(
          find.byKey(const ValueKey('audio-input-notice')),
          findsOneWidget,
        );
        final notice = find.textContaining(
          RegExp(
            r'browser[\s\S]*identif|identif[\s\S]*browser',
            caseSensitive: false,
          ),
        );
        expect(notice, findsOneWidget);
        final message = tester.widget<Text>(notice).data ?? '';
        expect(
          message,
          matches(
            RegExp(r'check[\s\S]*site[\s\S]*permission', caseSensitive: false),
          ),
        );
        expect(message, matches(RegExp(r'refresh', caseSensitive: false)));
        expect(
          message,
          matches(
            RegExp(
              r'no (microphone|capture)[\s\S]*(activat|start)[\s\S]*refresh|'
              r'refresh[\s\S]*(did not|does not|has not|never)[\s\S]*'
              r'(activat|start|request)[\s\S]*(microphone|capture)',
              caseSensitive: false,
            ),
          ),
        );
        expect(
          message,
          isNot(
            matches(
              RegExp(
                r'permission (is |was |has been )?(granted|denied)',
                caseSensitive: false,
              ),
            ),
          ),
        );
        expect(audio.arms, 0);
        expect(audio.clips, 0);
        expect(audio.recordings, 0);
        expect(audio.finishes, 0);
        expect(audio.imports, 0);
        expect(audio.queues, 0);
        expect(audio.selections, 0);
        expect(state.client, isNull);
        expect(state.converseSession, isNull);
        expect(state.converseHistory, isEmpty);
        expect(tester.takeException(), isNull);
      } finally {
        await tester.pumpWidget(const SizedBox.shrink());
        state.dispose();
      }
    },
  );

  for (final mode in [
    (nativeAudio: true, windowMode: true),
    (nativeAudio: false, windowMode: true),
    (nativeAudio: false, windowMode: false),
  ]) {
    testWidgets(
      'timing presentation native=${mode.nativeAudio} window=${mode.windowMode}',
      (tester) async {
        var changes = 0;
        await tester.pumpWidget(
          MaterialApp(
            home: Scaffold(
              body: SingleChildScrollView(
                child: VoiceOptionsSheet(
                  endSilenceMs: 1500,
                  silenceChoices: const [800, 1500, 2500],
                  onSilenceChanged: (_) => changes++,
                  nativeAudio: mode.nativeAudio,
                  windowMode: mode.windowMode,
                ),
              ),
            ),
          ),
        );
        await tester.pump();
        const liveClaim =
            'Live audio streams continuously. Decisions chooses when to wait, respond, or interrupt; no silence timer ends the turn.';
        const fallbackClaim =
            'Fallback clip capture: silence ends the clip. Not live model timing.';
        if (mode.nativeAudio) {
          final text = _visibleSheetText(tester);
          expect(text, matches(RegExp(r'buffered', caseSensitive: false)));
          expect(
            text,
            matches(
              RegExp(
                r'whole.?utterance|complete (spoken )?utterance|full utterance',
                caseSensitive: false,
              ),
            ),
          );
          expect(
            text,
            matches(RegExp(r'30\s*(seconds?|s\b)', caseSensitive: false)),
          );
          expect(
            text,
            matches(
              RegExp(
                r'end[\s\S]*detect|detect[\s\S]*end|speech.?end|silence',
                caseSensitive: false,
              ),
            ),
          );
          expect(find.text(liveClaim), findsNothing);
          expect(
            find.textContaining('Decisions chooses when to'),
            findsNothing,
          );
          expect(find.text(fallbackClaim), findsNothing);
          expect(find.text('Live'), findsNothing);
        } else if (mode.windowMode) {
          expect(find.text('Live'), findsOneWidget);
          expect(find.text(liveClaim), findsOneWidget);
          expect(find.text(fallbackClaim), findsNothing);
        } else {
          expect(find.text(fallbackClaim), findsOneWidget);
          expect(find.text(liveClaim), findsNothing);
        }
        expect(changes, 0);
        expect(tester.takeException(), isNull);
        await tester.pumpWidget(const SizedBox.shrink());
      },
    );
  }
}
