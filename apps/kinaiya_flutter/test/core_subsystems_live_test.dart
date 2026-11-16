import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:kinaiya_flutter/api/core_subsystems.dart';
import 'package:kinaiya_flutter/api/core_subsystems_client.dart';
import 'package:kinaiya_flutter/api/owned_settings_client.dart';

void main() {
  final url = Platform.environment['KINAIYA_LIVE_CORE_URL'];
  final token = Platform.environment['KINAIYA_LIVE_CORE_TOKEN'];
  test('Core subsystem Dart transport against actual Session API', () async {
    final previous = HttpOverrides.current;
    HttpOverrides.global = null;
    addTearDown(() => HttpOverrides.global = previous);
    final api = CoreSubsystemApi(
      baseUrl: url!,
      ownerToken: token!,
      clientFactory: http.Client.new,
    );
    addTearDown(api.close);
    final first = await api.inspect().result;
    final stale = CoreSubsystemChange.create(
      first,
      const CoreSubsystemSelection.saved(CoreSubsystemMode.core),
      'live-original-not-sent',
    );
    var count = 0;
    for (final mode in CoreSubsystemMode.values) {
      final before = await api.inspect().result;
      final request = CoreSubsystemChange.create(
        before,
        CoreSubsystemSelection.saved(mode),
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
      expect(after.startupMode, CoreSubsystemMode.agent);
      count++;
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
    final beforeReset = await api.inspect().result;
    final reset = CoreSubsystemChange.create(
      beforeReset,
      const CoreSubsystemSelection.host(),
      'live-dart-host',
    );
    await api.change(reset).result;
    final afterReset = await api.inspect().result;
    expect(afterReset.selection.isHost, isTrue);
    expect(afterReset.selectedMode, CoreSubsystemMode.agent);
    expect(afterReset.revision, count + 1);
    final forbidden = CoreSubsystemApi(
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
          'Agent is not Core owner',
          403,
        ),
      ),
    );
  }, skip: url == null || token == null);
}
