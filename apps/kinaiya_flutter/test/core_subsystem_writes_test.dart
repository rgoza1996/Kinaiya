import 'package:flutter_test/flutter_test.dart';
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/core_subsystems.dart';
import 'package:kinaiya_flutter/api/core_subsystems_client.dart';
import 'package:kinaiya_flutter/state/core_subsystem_writes.dart';

import 'core_subsystems_options_test.dart' show reply;
import 'core_subsystems_test.dart' show nativeCoreFixture;

void main() {
  test(
    'a replacement controller observed before send prevents the original mutation',
    () async {
      final f = nativeCoreFixture(), writes = CoreSubsystemWrites();
      var sends = 0;
      final api = CoreSubsystemApi(
        baseUrl: 'https://core.invalid',
        ownerToken: 'public-retired-core',
        clientFactory: () => MockClient((_) async {
          sends++;
          throw StateError('A retired original must not send');
        }),
      );
      final snapshot = CoreSubsystemSnapshot.fromJson(f['snapshot']);
      final scope = writes.open(api, snapshot)!;
      writes.select(
        scope,
        const CoreSubsystemSelection.saved(CoreSubsystemMode.core),
      );
      final pending = writes.begin(scope, api)!;
      final retired = CoreSubsystemSnapshot.fromJson({
        ...f['snapshot'] as Map<String, Object?>,
        'authority_generation': snapshot.generation + 1,
        'authority_epoch': 'e' * 32,
      });
      expect(writes.open(api, retired), same(scope));
      await writes.send(pending, api);
      expect(sends, 0);
      expect(pending.complete, isTrue);
      expect(scope.pending, isNull);
      await writes.send(pending, api);
      expect(sends, 0);
      await api.close();
      writes.dispose();
    },
  );

  test(
    'known backend replacement prevents old saves and lookups without exposing the old draft',
    () async {
      final f = nativeCoreFixture(), writes = CoreSubsystemWrites();
      var sends = 0;
      final api = CoreSubsystemApi(
        baseUrl: 'https://core.invalid',
        ownerToken: 'public-replaced-core',
        clientFactory: () => MockClient((_) async {
          sends++;
          return reply(f['lookup_before']);
        }),
      );
      final original = writes.open(
        api,
        CoreSubsystemSnapshot.fromJson(f['snapshot']),
      )!;
      writes.select(
        original,
        const CoreSubsystemSelection.saved(CoreSubsystemMode.all),
      );
      final pending = writes.begin(original, api)!;
      final fresh = writes.open(
        api,
        CoreSubsystemSnapshot.fromJson({
          ...f['snapshot'] as Map<String, Object?>,
          'backend_id': 'b' * 32,
        }),
      )!;
      expect(fresh.pending, isNull);
      expect(fresh.draft.dirty, isFalse);
      await writes.lookup(pending, api);
      expect(sends, 0);
      await writes.send(pending, api);
      expect(sends, 0);
      await api.close();
      writes.dispose();
    },
  );

  test(
    'dirty drafts cannot rebase onto changed host state and retention stays bounded',
    () async {
      final f = nativeCoreFixture(), writes = CoreSubsystemWrites();
      CoreSubsystemApi make(String token) => CoreSubsystemApi(
        baseUrl: 'https://core.invalid',
        ownerToken: token,
        clientFactory: () => throw StateError('No request'),
      );
      final clients = <CoreSubsystemApi>[];
      final snapshot = CoreSubsystemSnapshot.fromJson(f['snapshot']);
      for (var i = 0; i < 16; i++) {
        final api = make('public-bound-$i');
        clients.add(api);
        final scope = writes.open(api, snapshot)!;
        writes.select(
          scope,
          const CoreSubsystemSelection.saved(CoreSubsystemMode.core),
        );
        final changed = CoreSubsystemSnapshot.fromJson({
          ...f['snapshot'] as Map<String, Object?>,
          'state_token': 'd' * 64,
          'host_mode': 'decisions',
          'selected_mode': 'decisions',
        });
        expect(writes.open(api, changed), same(scope));
        expect(scope.draft.base, same(snapshot));
        expect(writes.begin(scope, api), isNull);
        writes.reset(scope);
        writes.select(
          scope,
          const CoreSubsystemSelection.saved(CoreSubsystemMode.core),
        );
        expect(scope.draft.base, same(changed));
      }
      final full = make('public-bound-extra');
      clients.add(full);
      expect(writes.open(full, snapshot), isNull);
      for (final api in clients) {
        await api.close();
      }
      writes.dispose();
    },
  );
}
