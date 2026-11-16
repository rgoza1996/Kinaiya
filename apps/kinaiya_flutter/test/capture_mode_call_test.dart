import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/capture_modes.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/state/capture_mode_writes.dart';
import 'package:kinaiya_flutter/ui/character_view.dart';

import 'capture_modes_test.dart' show nativeCaptureFixture;
import 'live_capture_test.dart' show CaptureAudio, ControlledApi;

class _Audio extends CaptureAudio {
  int arms = 0;
  @override
  Future<String> startArmed() async {
    arms++;
    return 'armed';
  }
}

int _backend = 100;
Map<String, Object?> _snapshot(String? mode, {bool enabled = false}) => {
  ...nativeCaptureFixture()['snapshot'] as Map<String, Object?>,
  'backend_id': (++_backend).toRadixString(16).padLeft(32, '0'),
  'default_mode': mode,
  'selected_mode': mode,
  'adaptive_live_enabled': enabled,
};

http.Response _reply(Object? value) => http.Response(
  jsonEncode(value),
  200,
  headers: {'content-type': 'application/json'},
);

Future<AppState> _mount(
  WidgetTester tester,
  ControlledApi api,
  _Audio audio,
) async {
  final state = AppState()
    ..connect(ConnectionProfile.defaults(), injected: api)
    ..converseSession = SessionInfo(
      sessionId: 'public-converse',
      threadId: 'public-thread',
      channel: 'converse',
      epoch: 1,
    )
    ..converseDraft = 'Retain the original draft';
  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(
        body: CharacterView(state: state, audio: audio),
      ),
    ),
  );
  await tester.pump(const Duration(milliseconds: 100));
  return state;
}

void main() {
  for (final scenario in [
    (mode: 'adaptive_live', enabled: false, unavailable: false),
    (mode: 'adaptive_live', enabled: true, unavailable: false),
    (mode: 'rolling_windows', enabled: false, unavailable: false),
    (mode: null, enabled: false, unavailable: false),
    (mode: 'legacy_silence', enabled: false, unavailable: true),
  ]) {
    testWidgets(
      'Call refuses unsupported or unobserved mode $scenario without fallback',
      (tester) async {
        final snapshot = _snapshot(scenario.mode, enabled: scenario.enabled);
        final requests = <http.Request>[];
        final api = ControlledApi(
          captureFactory: () => MockClient((request) async {
            requests.add(request);
            expect(request.method, 'GET');
            return scenario.unavailable
                ? http.Response('unavailable', 503)
                : _reply(snapshot);
          }),
        );
        final audio = _Audio();
        final state = await _mount(tester, api, audio);
        await tester.tap(find.byTooltip('Start Live listen (armed mic)'));
        await tester.pump(const Duration(milliseconds: 100));
        expect(requests.length, 1);
        expect(state.converseHistory.last.role, 'system');
        expect(state.converseHistory.last.text, isNotEmpty);
        expect(state.converseDraft, 'Retain the original draft');
        expect(audio.arms, 0);
        expect(audio.clips, 0);
        await tester.pumpWidget(const SizedBox.shrink());
        state.dispose();
      },
    );
  }

  testWidgets('late mode inspection cannot arm a changed audio input owner', (
    tester,
  ) async {
    final snapshot = _snapshot('legacy_silence');
    final inspected = Completer<http.Response>();
    var reads = 0;
    final api = ControlledApi(
      captureFactory: () => MockClient((request) {
        expect(request.method, 'GET');
        reads++;
        return inspected.future;
      }),
    );
    final audio = _Audio();
    final state = await _mount(tester, api, audio);
    await tester.tap(find.byTooltip('Start Live listen (armed mic)'));
    await tester.pump();
    expect(reads, 1);
    expect(audio.arms, 0);
    expect(audio.clips, 0);
    state.nativeAudioSourceGeneration++;
    inspected.complete(_reply(snapshot));
    await tester.pump(const Duration(milliseconds: 100));
    expect(reads, 1);
    expect(audio.arms, 0);
    expect(audio.clips, 0);
    expect(
      state.converseHistory.last.text,
      contains('Audio input configuration changed'),
    );
    expect(state.converseDraft, 'Retain the original draft');
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
  });

  testWidgets('an unconfirmed mode save blocks a new Call until exact proof', (
    tester,
  ) async {
    final fixture = nativeCaptureFixture(),
        snapshot = _snapshot('rolling_windows');
    CaptureModeChange? original;
    var mutations = 0;
    final api = ControlledApi(
      captureFactory: () => MockClient((request) async {
        if (request.method == 'GET') return _reply(snapshot);
        if (request.url.path.endsWith('/lookup')) {
          return _reply({
            ...fixture['lookup_saved'] as Map<String, Object?>,
            'backend_id': original!.backendId,
            'request_sha256': original!.digest,
            'receipt': {
              ...fixture['receipt'] as Map<String, Object?>,
              'backend_id': original!.backendId,
              'idempotency_key': original!.key,
            },
          });
        }
        mutations++;
        original = CaptureModeChange.fromJson(jsonDecode(request.body));
        throw http.ClientException('PUBLIC-ACK-LOSS');
      }),
    );
    final scope = captureModeWrites.open(
      api.captureModes,
      CaptureModeSnapshot.fromJson(snapshot),
    )!;
    captureModeWrites.select(
      scope,
      const CaptureModeSelection.saved(CaptureMode.legacySilence),
    );
    final pending = captureModeWrites.begin(scope, api.captureModes)!;
    await captureModeWrites.send(pending, api.captureModes);
    final audio = _Audio();
    final state = await _mount(tester, api, audio);
    await tester.tap(find.byTooltip('Start Live listen (armed mic)'));
    await tester.pump(const Duration(milliseconds: 100));
    expect(state.converseHistory.last.text, contains('unconfirmed save'));
    expect(audio.arms, 0);
    expect(audio.clips, 0);
    expect(mutations, 1);
    expect(scope.pending, same(pending));
    await captureModeWrites.lookup(pending, api.captureModes);
    expect(pending.complete, isTrue);
    expect(scope.pending, isNull);
    expect(mutations, 1);
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
  });
}
