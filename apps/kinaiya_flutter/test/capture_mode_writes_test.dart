import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/capture_modes.dart';
import 'package:kinaiya_flutter/api/capture_modes_client.dart';
import 'package:kinaiya_flutter/state/capture_mode_writes.dart';

import 'capture_modes_test.dart' show nativeCaptureFixture;

void main() {
  test(
    'matching values and an absent lookup cannot settle an unconfirmed original',
    () async {
      final fixture = nativeCaptureFixture(), writes = CaptureModeWrites();
      var mutations = 0, lookups = 0;
      final api = CaptureModeApi(
        baseUrl: 'https://converse.invalid',
        ownerToken: 'public-uncertain-capture',
        clientFactory: () => MockClient((request) async {
          final body = jsonDecode(request.body) as Map<String, Object?>;
          if (request.url.path.endsWith('/lookup')) {
            lookups++;
            return http.Response(
              jsonEncode({
                ...fixture['lookup_before'] as Map<String, Object?>,
                'request_sha256': CaptureModeChange.fromJson(body).digest,
              }),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          mutations++;
          throw http.ClientException('PUBLIC-UNCONFIRMED-ACK');
        }),
      );
      final before = CaptureModeSnapshot.fromJson(fixture['snapshot']);
      final scope = writes.open(api, before)!;
      writes.select(
        scope,
        const CaptureModeSelection.saved(CaptureMode.legacySilence),
      );
      final original = writes.begin(scope, api)!;
      await writes.send(original, api);
      expect(mutations, 1);
      expect(scope.pending, same(original));
      writes.open(
        api,
        CaptureModeSnapshot.fromJson({
          ...fixture['snapshot'] as Map<String, Object?>,
          'revision': 1,
          'state_token': 'e' * 64,
          'selection': original.request.selection.json,
          'selected_mode': 'legacy_silence',
        }),
      );
      expect(scope.pending, same(original));
      await writes.lookup(original, api);
      expect(lookups, 1);
      expect(original.complete, isFalse);
      expect(scope.pending, same(original));
      expect(writes.begin(scope, api), isNull);
      await writes.send(original, api);
      expect(mutations, 1);
      await api.close();
      writes.dispose();
    },
  );

  test(
    'a replacement controller observed before send prevents the original mutation',
    () async {
      final f = nativeCaptureFixture(), writes = CaptureModeWrites();
      var sends = 0;
      final api = CaptureModeApi(
        baseUrl: 'https://converse.invalid',
        ownerToken: 'public-retired-core',
        clientFactory: () => MockClient((_) async {
          sends++;
          throw StateError('A retired original must not send');
        }),
      );
      final snapshot = CaptureModeSnapshot.fromJson(f['snapshot']);
      final scope = writes.open(api, snapshot)!;
      writes.select(
        scope,
        const CaptureModeSelection.saved(CaptureMode.legacySilence),
      );
      final pending = writes.begin(scope, api)!;
      final retired = CaptureModeSnapshot.fromJson({
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
      final f = nativeCaptureFixture(), writes = CaptureModeWrites();
      var sends = 0;
      final api = CaptureModeApi(
        baseUrl: 'https://converse.invalid',
        ownerToken: 'public-replaced-core',
        clientFactory: () => MockClient((_) async {
          sends++;
          return http.Response(
            jsonEncode(f['lookup_before']),
            200,
            headers: {'content-type': 'application/json'},
          );
        }),
      );
      final original = writes.open(
        api,
        CaptureModeSnapshot.fromJson(f['snapshot']),
      )!;
      writes.select(
        original,
        const CaptureModeSelection.saved(CaptureMode.adaptiveLive),
      );
      final pending = writes.begin(original, api)!;
      final fresh = writes.open(
        api,
        CaptureModeSnapshot.fromJson({
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
      final f = nativeCaptureFixture(), writes = CaptureModeWrites();
      CaptureModeApi make(String token) => CaptureModeApi(
        baseUrl: 'https://converse.invalid',
        ownerToken: token,
        clientFactory: () => throw StateError('No request'),
      );
      final clients = <CaptureModeApi>[];
      final snapshot = CaptureModeSnapshot.fromJson(f['snapshot']);
      for (var i = 0; i < 16; i++) {
        final api = make('public-bound-$i');
        clients.add(api);
        final scope = writes.open(api, snapshot)!;
        writes.select(
          scope,
          const CaptureModeSelection.saved(CaptureMode.legacySilence),
        );
        final changed = CaptureModeSnapshot.fromJson({
          ...f['snapshot'] as Map<String, Object?>,
          'state_token': 'd' * 64,
          'default_mode': 'legacy_silence',
          'selected_mode': 'legacy_silence',
        });
        expect(writes.open(api, changed), same(scope));
        expect(scope.draft.base, same(snapshot));
        expect(writes.begin(scope, api), isNull);
        writes.reset(scope);
        writes.select(
          scope,
          const CaptureModeSelection.saved(CaptureMode.legacySilence),
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
