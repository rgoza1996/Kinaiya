import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:kinaiya_flutter/api/capture_modes.dart';
import 'package:kinaiya_flutter/api/capture_modes_client.dart';
import 'package:kinaiya_flutter/api/owned_settings_client.dart';

void main() {
  final url = Platform.environment['KINAIYA_LIVE_CAPTURE_URL'];
  final token = Platform.environment['KINAIYA_LIVE_CAPTURE_TOKEN'];
  test('Capture Mode Dart transport against actual Converse owner', () async {
    final previous = HttpOverrides.current;
    HttpOverrides.global = null;
    addTearDown(() => HttpOverrides.global = previous);
    final api = CaptureModeApi(
      baseUrl: url!,
      ownerToken: token!,
      clientFactory: http.Client.new,
    );
    addTearDown(api.close);
    final first = await api.inspect().result;
    final stale = CaptureModeChange.create(
      first,
      const CaptureModeSelection.saved(CaptureMode.legacySilence),
      'live-original-not-sent',
    );
    for (final mode in CaptureMode.values) {
      final before = await api.inspect().result;
      final request = CaptureModeChange.create(
        before,
        CaptureModeSelection.saved(mode),
        'live-dart-${mode.wire}',
      );
      final receipt = await api.change(request).result;
      expect(receipt.revision, before.revision + 1);
      expect(receipt.selection.mode, mode);
      final replay = await api.change(request).result;
      expect(replay.revision, receipt.revision);
      final proof = await api.lookup(request).result;
      expect(proof.saved, isTrue);
      expect(proof.fenced, isFalse);
      final after = await api.inspect().result;
      expect(after.selectedMode, mode);
      expect(after.adaptiveLiveEnabled, isFalse);
    }
    await expectLater(
      api.change(stale).result,
      throwsA(
        isA<OwnedSettingsFailure>().having((e) => e.status, 'stale CAS', 409),
      ),
    );
    final staleProof = await api.lookup(stale).result;
    expect(staleProof.saved, isFalse);
    expect(staleProof.fenced, isTrue);
    final reset = CaptureModeChange.create(
      await api.inspect().result,
      const CaptureModeSelection.host(),
      'live-dart-host',
    );
    await api.change(reset).result;
    final afterReset = await api.inspect().result;
    expect(afterReset.selection.isHost, isTrue);
    expect(afterReset.selectedMode, CaptureMode.rollingWindows);
    expect(afterReset.revision, 4);
    final forbidden = CaptureModeApi(
      baseUrl: url,
      ownerToken: 'a' * 32,
      clientFactory: http.Client.new,
    );
    addTearDown(forbidden.close);
    await expectLater(
      forbidden.inspect().result,
      throwsA(
        isA<OwnedSettingsFailure>().having(
          (e) => e.status,
          'Agent is not Converse owner',
          403,
        ),
      ),
    );
  }, skip: url == null || token == null);
}
