import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/core_subsystems.dart';
import 'package:kinaiya_flutter/api/core_subsystems_client.dart';
import 'package:kinaiya_flutter/api/owned_settings_client.dart';

import 'agent_model_transport_test.dart'
    show ModelTestClient, modelResponse, sentBody;
import 'core_subsystems_test.dart' show nativeCoreFixture;

void main() {
  test(
    'inspect, save and exact lookup use independent bounded Core transports',
    () async {
      final f = nativeCoreFixture(),
          clients = <ModelTestClient>[],
          paths = <String>[];
      final original = CoreSubsystemChange.fromJson(f['request']);
      final api = CoreSubsystemApi(
        baseUrl: 'https://core.invalid/session',
        ownerToken: 'public-core-transport',
        clientFactory: () {
          final client = ModelTestClient((request) async {
            paths.add(request.url.path);
            expect(request.url.host, 'core.invalid');
            expect(request.followRedirects, isFalse);
            expect(
              request.headers['Authorization'],
              'Bearer public-core-transport',
            );
            expect(request.headers['Accept-Encoding'], 'identity');
            if (request.method == 'GET') {
              expect((request as http.Request).bodyBytes, isEmpty);
              return modelResponse(f['snapshot']);
            }
            expect(sentBody(request), original.body);
            expect((request as http.Request).body, jsonEncode(original.body));
            return modelResponse(
              request.url.path.endsWith('/lookup')
                  ? f['lookup_saved']
                  : f['receipt'],
            );
          });
          clients.add(client);
          return client;
        },
      );
      final inspected = api.inspect();
      expect((await inspected.result).selectedMode, CoreSubsystemMode.agent);
      await inspected.settled;
      final changed = api.change(original);
      expect((await changed.result).revision, original.revision + 1);
      await changed.settled;
      final lookup = api.lookup(original);
      expect((await lookup.result).saved, isTrue);
      await lookup.settled;
      expect(paths, [
        '/session/v1/core/options/subsystems',
        '/session/v1/core/options/subsystems',
        '/session/v1/core/options/subsystem-changes/lookup',
      ]);
      await api.close();
      expect(clients.every((c) => c.sends == 1 && c.closes == 1), isTrue);
    },
  );

  test(
    'malformed, redirected, private and over-bound results stay unconfirmed',
    () async {
      final f = nativeCoreFixture(),
          original = CoreSubsystemChange.fromJson(f['request']);
      final good = jsonEncode(f['receipt']);
      http.StreamedResponse raw(
        List<int> bytes, [
        Map<String, String>? headers,
      ]) => http.StreamedResponse(
        Stream.value(bytes),
        200,
        headers: headers ?? {'content-type': 'application/json'},
      );
      final cases = <http.StreamedResponse Function()>[
        () => modelResponse({'private': 'PRIVATE BODY'}, status: 409),
        () => http.StreamedResponse(
          Stream.value(utf8.encode('PRIVATE REDIRECT')),
          302,
          headers: {'location': 'https://foreign.invalid'},
        ),
        () => raw([255]),
        () => raw(
          utf8.encode(
            good.replaceFirst(
              '"status":"saved"',
              '"status":"saved","status":"saved"',
            ),
          ),
        ),
        () => raw(utf8.encode('$good{}')),
        () => raw(
          utf8.encode(good.replaceFirst('"revision":1', '"revision":1.0')),
        ),
        () => raw(
          utf8.encode(
            good.replaceFirst('"revision":1', '"revision":9007199254740992'),
          ),
        ),
        () => raw(List.filled(16385, 32)),
        () => raw(utf8.encode(good), {
          'content-type': 'application/json',
          'content-encoding': 'gzip',
        }),
        () => raw(utf8.encode(good), {'content-type': 'text/plain'}),
        () => modelResponse({
          ...f['receipt'] as Map<String, Object?>,
          'backend_id': 'f' * 32,
        }),
      ];
      for (final response in cases) {
        final client = ModelTestClient((_) async => response());
        final api = CoreSubsystemApi(
          baseUrl: 'https://core.invalid',
          ownerToken: 'public-core-errors',
          clientFactory: () => client,
        );
        final attempt = api.change(original);
        await expectLater(
          attempt.result,
          throwsA(
            isA<OwnedSettingsFailure>()
                .having((e) => e.unconfirmed, 'unconfirmed', isTrue)
                .having(
                  (e) => e.message,
                  'redacted diagnostic',
                  isNot(contains('PRIVATE')),
                ),
          ),
        );
        await attempt.settled;
        await api.close();
        expect(client.sends, 1);
        expect(client.closes, 1);
      }
    },
  );

  test(
    'normal and mock facades require dedicated Core injection and never use Agent',
    () async {
      final f = nativeCoreFixture();
      for (final mock in [false, true]) {
        final shared = ModelTestClient(
          (_) => throw StateError('shared HTTP must not be used'),
        );
        final absent = mock
            ? KinaiyaClient.mock(
                sessionBase: 'https://core.invalid',
                agentBase: 'https://agent.invalid',
                converseBase: 'https://converse.invalid',
                mockTransport: shared,
              )
            : KinaiyaClient(
                sessionBase: 'https://core.invalid',
                agentBase: 'https://agent.invalid',
                converseBase: 'https://converse.invalid',
                ownerToken: 'public-owner',
                httpClient: shared,
              );
        await expectLater(
          absent.coreSubsystems.inspect().result,
          throwsA(isA<OwnedSettingsFailure>()),
        );
        expect(shared.sends, 0);
        absent.close();
        final dedicated = <ModelTestClient>[];
        http.Client factory() {
          final client = ModelTestClient((request) async {
            expect(request.url.host, 'core.invalid');
            return modelResponse(f['snapshot']);
          });
          dedicated.add(client);
          return client;
        }

        final bound = mock
            ? KinaiyaClient.mock(
                sessionBase: 'https://core.invalid',
                agentBase: 'https://agent.invalid',
                converseBase: 'https://converse.invalid',
                mockTransport: shared,
                coreSubsystemClientFactory: factory,
              )
            : KinaiyaClient(
                sessionBase: 'https://core.invalid',
                agentBase: 'https://agent.invalid',
                converseBase: 'https://converse.invalid',
                ownerToken: 'public-owner',
                httpClient: shared,
                coreSubsystemClientFactory: factory,
              );
        final attempt = bound.coreSubsystems.inspect();
        expect((await attempt.result).selectedMode, CoreSubsystemMode.agent);
        await attempt.settled;
        bound.close();
        expect(shared.sends, 0);
        expect(dedicated.single.sends, 1);
        expect(dedicated.single.closes, 1);
      }
    },
  );
}
