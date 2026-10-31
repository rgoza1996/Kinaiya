/// Actual loopback WebSocket transport only; no Kinaiya services or providers.
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/events_socket.dart';

void main() {
  test(
    'event upgrades preserve service prefixes and authenticate only in the first frame',
    () async {
      const token = 'synthetic-loopback-owner';
      const session = 'synthetic-session';
      final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
      final requests = <Uri>[];
      final authorizations = <String?>[];
      final hellos = <Object?>[];
      final serverSockets = <WebSocket>[];
      final subscriptions = <StreamSubscription<dynamic>>[];
      final failures = <Object>[];
      final clients = <EventsSocket>[];

      Map<String, Object?> event(int sequence, {String source = session}) => {
        'schema_version': '1.0',
        'event_id': 'synthetic-$sequence',
        'sequence': sequence,
        'session_id': source,
        'thread_id': 'synthetic-thread',
        'epoch': 1,
        'kind': 'conversation.user',
        'payload': {'text': 'literal replay $sequence'},
        'origin': 'authenticated-user',
      };

      final listener = server.listen((request) async {
        requests.add(request.uri);
        authorizations.add(
          request.headers.value(HttpHeaders.authorizationHeader),
        );
        try {
          final socket = await WebSocketTransformer.upgrade(request);
          serverSockets.add(socket);
          var receivedHello = false;
          subscriptions.add(
            socket.listen((raw) {
              if (receivedHello) {
                failures.add(
                  StateError('Unexpected second authentication frame'),
                );
                return;
              }
              receivedHello = true;
              try {
                hellos.add(jsonDecode(raw as String));
                // Native session/cursor filtering must survive URL normalization.
                socket.add(jsonEncode(event(100, source: 'another-session')));
                socket.add(jsonEncode(event(7)));
                socket.add(jsonEncode(event(8)));
                socket.add(jsonEncode(event(8)));
                socket.add(jsonEncode(event(9)));
              } catch (error) {
                failures.add(error);
              }
            }, onError: (Object error) => failures.add(error)),
          );
        } catch (error) {
          failures.add(error);
        }
      });

      try {
        for (final prefix in ['', '/proxy']) {
          for (final suffix in ['', '/']) {
            final api = EventsSocket(
              sessionBase: ' \thttp://127.0.0.1:${server.port}$prefix$suffix\n',
              ownerToken: token,
            );
            clients.add(api);
            final batches = await api
                .connect(sessionId: session, after: 7)
                .take(2)
                .toList()
                .timeout(const Duration(seconds: 2));
            expect(requests.last.toString(), '$prefix/v1/events');
            expect(requests.last.hasQuery, isFalse);
            expect(requests.last.hasFragment, isFalse);
            expect(requests.last.userInfo, isEmpty);
            expect(requests.last.toString(), isNot(contains(token)));
            expect(authorizations.last, isNull);
            expect(hellos.last, {
              'token': token,
              'after': 7,
              'session_id': session,
            });
            expect(batches.map((b) => b.cursor), [8, 9]);
            expect(batches.map((b) => b.entries.single.sessionId), [
              session,
              session,
            ]);
            expect(batches.last.entries.single.threadId, 'synthetic-thread');
            expect(batches.last.entries.single.text, 'literal replay 9');
            await api.close().timeout(const Duration(seconds: 1));
          }
        }
        expect(requests.length, 4);
        expect(hellos.length, 4);
        expect(failures, isEmpty);
      } finally {
        for (final api in clients) {
          await api.close().timeout(const Duration(seconds: 1));
        }
        for (final socket in serverSockets) {
          await socket.close().timeout(const Duration(seconds: 1));
        }
        for (final subscription in subscriptions) {
          await subscription.cancel();
        }
        await server.close(force: true);
        await listener.cancel();
      }
    },
    timeout: const Timeout(Duration(seconds: 5)),
  );
}
