import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/companion/app.dart';
import 'package:kinaiya_flutter/companion/client.dart';
import 'package:kinaiya_flutter/companion/controller.dart';
import 'package:kinaiya_flutter/companion/models.dart';

// Public synthetic control fixtures; no models, microphones or real grants.
final fixtureDevice = <String, Object?>{
  'schema_version': '1.0',
  'device_id': 'device-1',
  'device_name': 'Test phone',
  'label': 'Synthetic host',
  'scope': 'companion_control',
  'session_id': 'session-1',
  'thread_id': 'thread-1',
  'epoch': 1,
  'created_at': 100,
  'expires_at': 86500,
  'revoked_at': null,
  'status': 'active',
};
String get fixtureToken => 't' * 43;
Map<String, Object?> fixtureCall({
  int revision = 1,
  String state = 'ringing',
  String note = 'Synthetic note',
  int sequence = 1,
  String id = 'call-1',
}) => {
  'schema_version': '1.0',
  'call_id': id,
  'sequence': sequence,
  'device_id': 'device-1',
  'session_id': 'session-1',
  'thread_id': 'thread-1',
  'epoch': 1,
  'title': 'Synthetic invitation',
  'state': state,
  'revision': revision,
  'created_at': 100,
  'ringing_deadline': 130,
  'media_deadline': null,
  'end_reason': state == 'ended' ? 'media_unavailable' : null,
  'note': note,
};
http.Response jsonResponse(Object value) =>
    http.Response(jsonEncode(value), 200);
http.Response pairingResponse() => jsonResponse({
  'schema_version': '1.0',
  'device_token': fixtureToken,
  'device': fixtureDevice,
});
http.Response pageResponse(List<Object> calls, {int? next}) =>
    jsonResponse({'schema_version': '1.0', 'calls': calls, 'next_after': next});

CompanionClient clientFor(
  FutureOr<http.Response> Function(http.Request) handler,
) => CompanionClient(
  'https://host.invalid',
  clientFactory: () => MockClient((request) async => await handler(request)),
);

class HeldCleanupClient extends http.BaseClient {
  final cancelled = Completer<void>();
  bool closed = false;
  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) async {
    late StreamController<List<int>> stream;
    stream = StreamController<List<int>>(
      onListen: () => stream.add(List.filled(262145, 120)),
      onCancel: () => cancelled.future,
    );
    return http.StreamedResponse(stream.stream, 200);
  }

  @override
  void close() {
    closed = true;
  }
}

class DisposeTrackedController extends CompanionController {
  bool disposed = false;
  @override
  void dispose() {
    disposed = true;
    super.dispose();
  }
}

void main() {
  test(
    'a maximum escaped-note page fits the shared response allowance',
    () async {
      final rows = List.generate(
        20,
        (index) => fixtureCall(
          id: 'call-${index + 1}',
          sequence: index + 1,
          note: '"' * 4096,
        ),
      );
      final page = pageResponse(rows);
      expect(page.bodyBytes.length, greaterThan(131072));
      expect(page.bodyBytes.length, lessThanOrEqualTo(262144));
      final client = clientFor(
        (request) =>
            request.url.path.endsWith('/redeem') ? pairingResponse() : page,
      );
      final device = await client.pair('c' * 43, 'Test phone');
      final result = await client.calls(device);
      expect(result.length, 20);
      expect(result.every((call) => call.note == '"' * 4096), isTrue);
      client.close();
    },
  );
  test(
    'held stream cancellation closes transport and fails within a bound',
    () async {
      final transport = HeldCleanupClient();
      final client = CompanionClient(
        'https://host.invalid',
        clientFactory: () => transport,
      );
      await expectLater(
        client.pair('c' * 43, 'Test phone').timeout(const Duration(seconds: 2)),
        throwsA(
          isA<CompanionFailure>().having(
            (error) => error.disconnect,
            'disconnect',
            isTrue,
          ),
        ),
      );
      expect(transport.closed, isTrue);
      transport.cancelled.complete();
      client.close();
    },
  );

  test(
    'disposed and notification-disposed controllers never redeem a grant',
    () async {
      var factories = 0;
      CompanionClient factory(String host) {
        factories++;
        return clientFor((_) => pairingResponse());
      }

      final disposed = CompanionController(clientFactory: factory)..dispose();
      await disposed.pair('https://host.invalid', 'c' * 43, 'Test phone');
      final disposedByListener = CompanionController(clientFactory: factory);
      disposedByListener.addListener(disposedByListener.dispose);
      await disposedByListener.pair(
        'https://host.invalid',
        'c' * 43,
        'Test phone',
      );
      expect(factories, 0);
    },
  );

  test(
    'reentrant pairing notification cannot adopt a newer grant generation',
    () async {
      var factories = 0, switched = false;
      late CompanionController controller;
      controller = CompanionController(
        clientFactory: (_) {
          factories++;
          return clientFor(
            (request) => request.url.path.endsWith('/redeem')
                ? pairingResponse()
                : pageResponse([]),
          );
        },
      );
      Future<void>? replacement;
      controller.addListener(() {
        if (controller.pairing && !switched) {
          switched = true;
          replacement = controller.pair(
            'https://host.invalid',
            'b' * 43,
            'New phone',
          );
        }
      });
      await controller.pair('https://host.invalid', 'a' * 43, 'Old phone');
      await replacement;
      expect(factories, 1);
      expect(controller.paired, isTrue);
      controller.dispose();
    },
  );

  test(
    'nonadvancing or wrong-transition receipts retain original uncertain action',
    () async {
      for (final result in [
        fixtureCall(),
        fixtureCall(revision: 2, state: 'ended'),
      ]) {
        final controller = CompanionController(
          clientFactory: (_) => clientFor((request) {
            if (request.url.path.endsWith('/redeem')) return pairingResponse();
            if (request.method == 'GET') return pageResponse([fixtureCall()]);
            return jsonResponse(result);
          }),
        );
        await controller.pair('https://host.invalid', 'c' * 43, 'Test phone');
        await controller.action(controller.calls.single, 'answer');
        expect(controller.pending, isNotNull);
        final key = controller.pending!.key;
        await controller.retryAction();
        expect(controller.pending!.key, key);
        expect(controller.calls.single.state, CompanionCallState.ringing);
        controller.dispose();
      }
    },
  );

  testWidgets(
    'controller replacements dispose only locally owned controllers',
    (tester) async {
      final external = DisposeTrackedController();
      Widget home(CompanionController? controller) => MaterialApp(
        home: CompanionHome(
          key: const ValueKey('same-home'),
          controller: controller,
        ),
      );
      await tester.pumpWidget(home(external));
      await tester.pumpWidget(home(null));
      expect(external.disposed, isFalse);
      final owned =
          tester
                  .widget<ListenableBuilder>(
                    find.byType(ListenableBuilder).last,
                  )
                  .listenable
              as CompanionController;
      await tester.pumpWidget(home(external));
      expect(() => owned.addListener(() {}), throwsFlutterError);
      expect(external.disposed, isFalse);
      await tester.pumpWidget(const SizedBox.shrink());
      expect(external.disposed, isFalse);
      external.dispose();
    },
  );
  test(
    'pairing sends no bearer; subsequent calls use device bearer only in header',
    () async {
      final requests = <http.Request>[];
      final client = clientFor((request) {
        requests.add(request);
        return request.url.path.endsWith('/redeem')
            ? pairingResponse()
            : pageResponse([]);
      });
      final device = await client.pair('c' * 43, 'Test phone');
      await client.calls(device);
      expect(requests.first.headers.containsKey('authorization'), isFalse);
      expect(requests.first.url.toString(), isNot(contains('c' * 43)));
      expect(
        (jsonDecode(requests.first.body) as Map<String, Object?>)['code'],
        'c' * 43,
      );
      expect(requests.last.headers['authorization'], 'Bearer $fixtureToken');
      expect(requests.last.url.toString(), isNot(contains(fixtureToken)));
      expect(requests.every((request) => !request.followRedirects), isTrue);
      client.close();
    },
  );

  test(
    'remote plaintext, credentials, query and fragment hosts rejected before request',
    () {
      for (final host in [
        'http://remote.invalid',
        'https://user:secret@host.invalid',
        'https://host.invalid?token=secret',
        'https://host.invalid#secret',
        'file:///tmp',
      ]) {
        expect(() => CompanionClient(host), throwsA(isA<CompanionFailure>()));
      }
      CompanionClient('http://127.0.0.1:8760').close();
    },
  );

  test(
    'redirect and echoed host errors never disclose response content',
    () async {
      for (final status in [302, 401, 403, 409, 422, 500]) {
        final client = clientFor(
          (_) => http.Response('secret $fixtureToken', status),
        );
        try {
          await client.pair('c' * 43, 'Test phone');
          fail('Expected failure');
        } on CompanionFailure catch (error) {
          expect(error.toString(), isNot(contains(fixtureToken)));
          expect(error.status, status);
        }
        client.close();
      }
    },
  );

  test(
    'five bounded pages recover recent invitations after old retained records',
    () async {
      var pages = 0;
      final client = clientFor((request) {
        if (request.url.path.endsWith('/redeem')) return pairingResponse();
        pages++;
        final after = int.parse(request.url.queryParameters['after']!);
        final rows = List.generate(
          20,
          (index) => fixtureCall(
            sequence: after + index + 1,
            id: 'call-${after + index + 1}',
            state: 'ended',
          ),
        );
        return pageResponse(rows, next: pages < 5 ? after + 20 : null);
      });
      final device = await client.pair('c' * 43, 'Test phone');
      final calls = await client.calls(device);
      expect(calls.length, 100);
      expect(calls.last.callId, 'call-100');
      expect(pages, 5);
      client.close();
    },
  );

  test(
    'wrong scope, duplicate/nonadvancing cursor and oversized page fail closed',
    () async {
      final badPages = [
        {
          'schema_version': '1.0',
          'calls': [
            {...fixtureCall(), 'device_id': 'other'},
          ],
          'next_after': null,
        },
        {
          'schema_version': '1.0',
          'calls': [fixtureCall(), fixtureCall()],
          'next_after': null,
        },
        {'schema_version': '1.0', 'calls': [], 'next_after': 1},
        {
          'schema_version': '1.0',
          'calls': [fixtureCall()],
          'next_after': 2,
        },
        {
          'schema_version': '1.0',
          'calls': List.generate(21, (_) => fixtureCall()),
          'next_after': null,
        },
      ];
      for (final page in badPages) {
        final client = clientFor(
          (request) => request.url.path.endsWith('/redeem')
              ? pairingResponse()
              : jsonResponse(page),
        );
        final device = await client.pair('c' * 43, 'Test phone');
        await expectLater(
          client.calls(device),
          throwsA(isA<CompanionFailure>()),
        );
        client.close();
      }
    },
  );

  test(
    'response and UTF8 note bounds reject malformed or excessive host data',
    () async {
      final client = clientFor((_) => http.Response('x' * 262145, 200));
      await expectLater(
        client.pair('c' * 43, 'Test phone'),
        throwsA(isA<CompanionFailure>()),
      );
      client.close();
      expect(
        () => CompanionCall.fromJson(fixtureCall(note: '月' * 1366)),
        throwsFormatException,
      );
      expect(
        () => CompanionCall.fromJson({...fixtureCall(), 'revision': true}),
        throwsFormatException,
      );
      expect(
        () => CompanionCall.fromJson({
          ...fixtureCall(),
          'epoch': companionMaxInteger + 1,
        }),
        throwsFormatException,
      );
      expect(
        () => CompanionCall.fromJson({...fixtureCall(), 'state': 'active'}),
        throwsFormatException,
      );
    },
  );

  test('uncertain action retry preserves key and captured revision', () async {
    final bodies = <Map<String, dynamic>>[];
    final controller = CompanionController(
      clientFactory: (_) => clientFor((request) {
        if (request.url.path.endsWith('/redeem')) return pairingResponse();
        if (request.method == 'GET') return pageResponse([fixtureCall()]);
        bodies.add(jsonDecode(request.body) as Map<String, dynamic>);
        if (bodies.length == 1) {
          throw http.ClientException('Synthetic lost receipt');
        }
        return jsonResponse(
          fixtureCall(revision: 2, state: 'accepted_waiting_media'),
        );
      }),
    );
    await controller.pair('https://host.invalid', 'c' * 43, 'Test phone');
    await controller.action(controller.calls.single, 'answer');
    expect(controller.pending, isNotNull);
    await controller.retryAction();
    expect(bodies.length, 2);
    expect(bodies.last, bodies.first);
    expect(bodies.last['expected_revision'], 1);
    expect(controller.pending, isNull);
    expect(
      controller.calls.single.state,
      CompanionCallState.acceptedWaitingMedia,
    );
    controller.dispose();
  });

  test(
    '409 reconciles current ended state and preserves literal note',
    () async {
      var ended = false;
      final controller = CompanionController(
        clientFactory: (_) => clientFor((request) {
          if (request.url.path.endsWith('/redeem')) return pairingResponse();
          if (request.method == 'POST') {
            ended = true;
            return http.Response('Synthetic superseded receipt', 409);
          }
          return pageResponse([
            fixtureCall(
              revision: ended ? 3 : 1,
              state: ended ? 'ended' : 'ringing',
              note: '<literal> {"value":7} https://example.invalid',
            ),
          ]);
        }),
      );
      await controller.pair('https://host.invalid', 'c' * 43, 'Test phone');
      await controller.action(controller.calls.single, 'answer');
      expect(controller.pending, isNull);
      expect(controller.calls.single.state, CompanionCallState.ended);
      expect(controller.calls.single.note, contains('<literal>'));
      controller.dispose();
    },
  );

  test('revocation clears credential view and reports access loss', () async {
    var revoked = false;
    final controller = CompanionController(
      clientFactory: (_) => clientFor((request) {
        if (request.url.path.endsWith('/redeem')) return pairingResponse();
        return revoked
            ? http.Response('Secret echo', 401)
            : pageResponse([fixtureCall()]);
      }),
    );
    await controller.pair('https://host.invalid', 'c' * 43, 'Test phone');
    revoked = true;
    await controller.refresh();
    expect(controller.paired, isFalse);
    expect(controller.calls, isEmpty);
    expect(controller.message, contains('Pair again'));
    controller.dispose();
  });

  test(
    'late response cannot repopulate disconnected view; polls do not overlap',
    () async {
      final response = Completer<http.Response>();
      var reads = 0;
      final controller = CompanionController(
        clientFactory: (_) => clientFor((request) {
          if (request.url.path.endsWith('/redeem')) return pairingResponse();
          reads++;
          return response.future;
        }),
      );
      final pairing = controller.pair(
        'https://host.invalid',
        'c' * 43,
        'Test phone',
      );
      await Future<void>.delayed(Duration.zero);
      await controller.refresh();
      expect(reads, 1);
      controller.disconnect();
      response.complete(pageResponse([fixtureCall()]));
      await pairing;
      expect(controller.paired, isFalse);
      expect(controller.calls, isEmpty);
      controller.dispose();
    },
  );

  testWidgets(
    '320px doubled text remains scrollable and literal ended note survives',
    (tester) async {
      tester.view.physicalSize = const Size(320, 480);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final controller = CompanionController(
        clientFactory: (_) => clientFor(
          (request) => request.url.path.endsWith('/redeem')
              ? pairingResponse()
              : pageResponse([
                  fixtureCall(
                    revision: 3,
                    state: 'ended',
                    note: '<literal> https://example.invalid',
                  ),
                ]),
        ),
      );
      await tester.runAsync(
        () => controller.pair('https://host.invalid', 'c' * 43, 'Test phone'),
      );
      await tester.pumpWidget(
        MaterialApp(
          builder: (context, child) => MediaQuery(
            data: MediaQuery.of(
              context,
            ).copyWith(textScaler: TextScaler.linear(2)),
            child: child!,
          ),
          home: CompanionHome(controller: controller),
        ),
      );
      await tester.pump();
      await tester.scrollUntilVisible(
        find.byKey(const ValueKey('call-note-call-1')),
        150,
        scrollable: find.byType(Scrollable).first,
      );
      expect(find.text('<literal> https://example.invalid'), findsOneWidget);
      expect(find.text('Answer'), findsNothing);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox.shrink());
      controller.dispose();
    },
  );
}
