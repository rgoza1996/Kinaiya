import 'dart:convert';

import 'package:crypto/crypto.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/artifact_client.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/artifacts/models.dart';
import 'package:kinaiya_flutter/artifacts/renderer.dart';

import 'support/intelligent_artifact_fixtures.dart';

final assetId = 'media_${List.filled(32, 'a').join()}';
final video = [0, 0, 0, 24, ...ascii.encode('ftypmp42'), ...List.filled(20, 0)];
Map<String, Object?> component({String? id}) =>
    artifactComponent(id ?? 'hands', 'media', {'asset_id': assetId});
IntelligentArtifact artifact() =>
    IntelligentArtifact.fromJson(artifactFixture(components: [component()]));
SessionInfo session() => SessionInfo(
  sessionId: 'session-test',
  threadId: 'thread-test',
  channel: 'converse',
  epoch: 1,
);

Map<String, Object?> receipt() => {
  'schema_version': 'artifact_media.v1',
  'asset_id': assetId,
  'binding': {
    'task_id': 'task-test',
    'objective_revision': 1,
    'session_id': 'session-test',
    'thread_id': 'thread-test',
    'epoch': 1,
  },
  'content_scope': 'public_synthetic',
  'publication_only': true,
  'source': {
    'sha256': sha256.convert(video).toString(),
    'bytes': video.length,
    'mime_type': 'video/mp4',
  },
  'highlighted': {
    'sha256': sha256.convert(video).toString(),
    'bytes': video.length,
    'mime_type': 'video/mp4',
  },
  'overlay_sha256': List.filled(64, 'b').join(),
  'width': 1280,
  'height': 720,
  'duration_seconds': 1.0,
  'fps_num': 30,
  'fps_den': 1,
  'frame_count': 30,
};

void main() {
  test('media is only an opaque native asset, never a URL or inline HTML', () {
    expect(artifact().components.single.type, 'media');
    final invalid = component();
    (invalid['data'] as Map)['url'] = 'https://untrusted.invalid/video.mp4';
    expect(
      () =>
          IntelligentArtifact.fromJson(artifactFixture(components: [invalid])),
      throwsFormatException,
    );
    expect(
      () => IntelligentArtifact.fromJson(
        artifactFixture(
          components: [
            component(),
            component(id: 'other'),
          ],
        ),
      ),
      throwsFormatException,
    );
    expect(artifact().components.single.acceptsAction, isFalse);
  });

  testWidgets(
    'render and plain fallback never automatically fetch or start media',
    (tester) async {
      var calls = 0;
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: IntelligentArtifactRenderer(
              artifact: artifact(),
              loadMedia: (_, _, _) async {
                calls++;
                throw StateError('must not load');
              },
            ),
          ),
        ),
      );
      expect(calls, 0);
      expect(
        find.text('Original video and SAM hand highlights'),
        findsOneWidget,
      );
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: IntelligentArtifactRenderer(
              artifact: artifact(),
              enabled: false,
            ),
          ),
        ),
      );
      expect(
        find.textContaining('Native video and SAM hand highlights.'),
        findsOneWidget,
      );
      expect(calls, 0);
    },
  );

  test(
    'authenticated scoped bytes are verified after original transport close',
    () async {
      var reads = 0;
      final clients = <_OwnedClient>[];
      final api = ArtifactApi(
        base: 'http://127.0.0.1:8761',
        ownerToken: 'synthetic-owner',
        clientFactory: () {
          final client = _OwnedClient((request) async {
            reads++;
            expect(request.headers['Authorization'], 'Bearer synthetic-owner');
            expect(request.url.toString(), isNot(contains('synthetic-owner')));
            expect(request.url.queryParameters['session_id'], 'session-test');
            expect(request.followRedirects, isFalse);
            if (request.url.path.endsWith('/content')) {
              return http.Response.bytes(
                video,
                200,
                headers: {'content-type': 'video/mp4'},
              );
            }
            return http.Response(jsonEncode(receipt()), 200);
          });
          clients.add(client);
          return client;
        },
      );
      final value = await api.media(
        session(),
        artifact(),
        assetId,
        'highlighted',
      );
      expect(value.bytes, video);
      expect(reads, 2);
      expect(clients.every((c) => c.closed), isTrue);
      await api.close();
    },
  );

  for (final failure in ['scope', 'digest', 'mime', 'cleanup']) {
    test('media refuses $failure without a fallback fetch or retry', () async {
      var reads = 0;
      final api = ArtifactApi(
        base: 'http://127.0.0.1:8761',
        ownerToken: 'synthetic-owner',
        clientFactory: () => _OwnedClient((request) async {
          reads++;
          if (request.url.path.endsWith('/content')) {
            return http.Response.bytes(
              failure == 'digest'
                  ? [...video.take(video.length - 1), 1]
                  : video,
              200,
              headers: {
                'content-type': failure == 'mime' ? 'text/html' : 'video/mp4',
              },
            );
          }
          final metadata = receipt();
          if (failure == 'scope') (metadata['binding'] as Map)['epoch'] = 2;
          return http.Response(jsonEncode(metadata), 200);
        }, failClose: failure == 'cleanup'),
      );
      await expectLater(
        api.media(session(), artifact(), assetId, 'source'),
        throwsA(isA<ArtifactFailure>()),
      );
      expect(reads, failure == 'scope' || failure == 'cleanup' ? 1 : 2);
      await api.close();
    });
  }
}

class _OwnedClient extends MockClient {
  _OwnedClient(super.handler, {this.failClose = false});
  final bool failClose;
  bool closed = false;
  @override
  void close() {
    closed = true;
    if (failClose) throw StateError('synthetic private cleanup detail');
    super.close();
  }
}
