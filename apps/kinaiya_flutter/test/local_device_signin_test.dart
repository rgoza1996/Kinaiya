import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/setup_dialog.dart';

import 'provider_consent_test.dart' show localProvider, verifyClient;

void main() {
  final page = Uri.parse('http://localhost:9988$localSignInPath');
  final device = 'kny_refr_${'x' * 48}';
  final success = http.Response(
    jsonEncode({'owner_token': 'scoped-session'}),
    200,
  );

  test('new browser exchanges the launch handoff at its own origin', () async {
    final result = await exchangeLocalSignIn(
      endpoint: page,
      readProof: () async => null,
      httpClient: MockClient((request) async {
        expect(request.url, page);
        expect(request.headers[localSignInHeader], '1');
        expect(request.body, isEmpty);
        expect(request.followRedirects, isFalse);
        return success;
      }),
    );
    expect(result.ownerToken, 'scoped-session');
  });

  test(
    'a paired browser sends only its device proof to the same origin',
    () async {
      final result = await exchangeLocalSignIn(
        endpoint: page,
        readProof: () async => device,
        httpClient: MockClient((request) async {
          expect(request.url, page.resolve(localRefreshPath));
          expect(request.headers[localSignInHeader], '1');
          expect(jsonDecode(request.body), {'refresh_token': device});
          expect(request.followRedirects, isFalse);
          return success;
        }),
      );
      expect(result.ownerToken, 'scoped-session');
    },
  );

  test(
    'revocation permits one fresh handoff and an outage preserves the pairing',
    () async {
      for (final status in [403, 503]) {
        final urls = <Uri>[];
        final result = exchangeLocalSignIn(
          endpoint: page,
          readProof: () async => device,
          httpClient: MockClient((request) async {
            urls.add(request.url);
            if (request.url.path == localSignInPath) return success;
            return http.Response(
              jsonEncode({
                'code': status == 403
                    ? 'LocalSignInDenied'
                    : 'StorageUnavailable',
              }),
              status,
            );
          }),
        );
        if (status == 403) {
          expect((await result).ownerToken, 'scoped-session');
          expect(urls, [page.resolve(localRefreshPath), page]);
        } else {
          await expectLater(result, throwsA(isA<LocalSignInException>()));
          expect(urls, [page.resolve(localRefreshPath)]);
        }
      }
    },
  );

  test('refuses remote origins before reading browser proof', () async {
    for (final uri in [
      'https://example.org/',
      'file:///index.html',
      'http://owner@localhost:8780/',
    ]) {
      expect(isLocalBrokerPage(Uri.parse(uri)), isFalse);
      await expectLater(
        exchangeLocalSignIn(
          endpoint: Uri.parse(uri),
          readProof: () => throw StateError('should not read proof'),
        ),
        throwsA(isA<LocalSignInException>()),
      );
    }
  });
  testWidgets(
    'Recover access applies an available local session without a file',
    (tester) async {
      var attempts = 0;
      var healthReads = 0;
      final state =
          AppState(
              localSignInExchange: () async {
                if (++attempts == 1) {
                  throw const LocalSignInException(
                    'Expired',
                    code: 'LocalSignInExpired',
                    statusCode: 403,
                  );
                }
                return LocalSignInResult(
                  ownerToken: 'synthetic-scoped-session',
                );
              },
            )
            ..clientFactory = (_) => verifyClient(
              providerFor: (_) {
                healthReads++;
                return localProvider();
              },
            );
      addTearDown(state.dispose);
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(body: SetupDialog(state: state, embedded: true)),
        ),
      );
      await tester.pump();
      final recover = find.byKey(const Key('local-host-recover-access'));
      await tester.ensureVisible(recover);
      await tester.tap(recover);
      await tester.pumpAndSettle();
      expect(state.sessionAuthenticated, isTrue);
      expect(state.connection!.ownerToken, 'synthetic-scoped-session');
      expect(state.agentCanInfer, isTrue);
      expect(healthReads, 2);
      expect(attempts, 2);
      expect(find.text('Pair this browser'), findsNothing);
      state.disconnect();
    },
  );
}
