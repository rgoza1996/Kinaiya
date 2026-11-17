import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/live_stream_client.dart';
import 'package:kinaiya_flutter/api/live_streams.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/owned_settings_client.dart';
import 'package:kinaiya_flutter/api/provider_settings.dart';

Map<String, Object?> fixture() =>
    jsonDecode(File('test/fixtures/live_stream_native.json').readAsStringSync())
        as Map<String, Object?>;
Map<String, Object?> object(Object? value) => value as Map<String, Object?>;
Map<String, Object?> copy(Object? value) =>
    object(jsonDecode(jsonEncode(value)));
Map<String, Object?> operation(int index) =>
    object((fixture()['operations'] as List)[index]);
LiveStreamOperation original(int index) {
  final row = operation(index);
  return LiveStreamOperation.fromJson(
    LiveStreamPhase.values.byName(row['phase'] as String),
    row['request'],
  );
}

LiveStreamApi api(
  http.Client Function() factory, {
  Duration timeout = const Duration(seconds: 10),
}) => LiveStreamApi(
  baseUrl: 'http://127.0.0.1:8762',
  ownerToken: 'synthetic-owner',
  clientFactory: factory,
  resultTimeout: timeout,
  cleanupTimeout: const Duration(milliseconds: 20),
);
http.Response response(Object? body) => http.Response(
  jsonEncode(body),
  200,
  headers: {'content-type': 'application/json'},
);

void main() {
  test(
    'actual native utterance original has a distinct route and exact begin-phase recovery',
    () {
      final native = object(
        jsonDecode(
          File('test/fixtures/live_utterance_native.json').readAsStringSync(),
        ),
      );
      final request = LiveStreamOperation.nextUtterance(native['request']);
      expect(request.route, 'next-utterance');
      expect(request.phase, LiveStreamPhase.begin);
      expect(request.lookup['phase'], 'begin');
      expect(request.isUtteranceBoundary, isTrue);
      expect(
        () => LiveStreamOperation.fromJson(LiveStreamPhase.begin, request.body),
        throwsFormatException,
      );
      final finish = LiveStreamOperation.fromJson(
        LiveStreamPhase.finish,
        native['previous_finish_request'],
      );
      final previous = LiveStreamProof.fromJson(
        native['previous_finish_proof'],
        finish,
      );
      expect(LiveStreamOperation.advanceUtterance(previous).body, request.body);
      final proof = LiveStreamProof.fromJson(native['proof'], request);
      expect(proof.confirmed, isTrue);
      expect(proof.capture!.captureOpen, isTrue);
      expect(proof.body['capture'], previous.body['capture']);
      for (final patch in [
        {'segment_index': 0},
        {'segment_index': 1.0},
        {'boundary_protocol': 'native_live_utterance_v2'},
        {'previous_finish_sha256': 'X' * 64},
        {'utterance_id': 'reused'},
        {'previous_utterance_id': request.body['utterance_id']},
        {'extra': true},
      ]) {
        expect(
          () => LiveStreamOperation.nextUtterance({...request.body, ...patch}),
          throwsFormatException,
        );
      }
    },
  );

  test(
    'new utterance dispatch and unknown-result lookup never become ordinary begin replay',
    () async {
      final native = object(
        jsonDecode(
          File('test/fixtures/live_utterance_native.json').readAsStringSync(),
        ),
      );
      final request = LiveStreamOperation.nextUtterance(native['request']);
      final paths = <String>[];
      final owner = api(
        () => MockClient((call) async {
          paths.add(call.url.path.split('/').last);
          expect(
            jsonDecode(call.body),
            paths.last == 'next-utterance' ? request.body : request.lookup,
          );
          return response(native['proof']);
        }),
      );
      expect((await owner.dispatch(request).result).confirmed, isTrue);
      expect((await owner.lookup(request).result).confirmed, isTrue);
      expect((await owner.reconcile(request).result).confirmed, isTrue);
      expect(paths, ['next-utterance', 'lookup', 'reconcile']);
      await owner.close();
    },
  );

  test(
    'normal and labeled mock clients use their independent live transport',
    () async {
      for (final mock in [false, true]) {
        var requests = 0;
        http.Client liveFactory() => MockClient((_) async {
          requests++;
          return response(fixture()['capability']);
        });
        final unrelated = MockClient(
          (_) async =>
              throw StateError('Live capture used an unrelated transport'),
        );
        final owner = mock
            ? KinaiyaClient.mock(
                sessionBase: 'http://fixture',
                agentBase: 'http://fixture',
                converseBase: 'http://127.0.0.1:8762',
                mockTransport: unrelated,
                liveStreamClientFactory: liveFactory,
              )
            : KinaiyaClient(
                sessionBase: 'http://fixture',
                agentBase: 'http://fixture',
                converseBase: 'http://127.0.0.1:8762',
                ownerToken: 'synthetic-owner',
                httpClient: unrelated,
                liveStreamClientFactory: liveFactory,
              );
        expect(requests, 0);
        expect((await owner.liveStreams.inspect().result).available, isTrue);
        expect(requests, 1);
        owner.close();
      }
    },
  );
  test(
    'native begin PCM finish and same-mic rollover bind exact original bodies',
    () {
      final capability = LiveStreamCapability.fromJson(fixture()['capability']);
      expect(capability.available, isTrue);
      final proofs = <LiveStreamProof>[];
      for (var i = 0; i < 4; i++) {
        final proof = LiveStreamProof.fromJson(
          operation(i)['proof'],
          original(i),
        );
        expect(proof.confirmed, isTrue);
        expect(proof.capture!.captureOpen, isTrue);
        expect(
          proof.segmentRetired,
          isFalse,
          reason: 'Decoder result is not actual process/HTTP retirement',
        );
        expect(
          object(proof.body['native'])['request_sha256'],
          original(i).digest,
        );
        proofs.add(proof);
      }
      expect(proofs[3].segmentSampleOffset, 160);
      expect(proofs[3].streamId, isNot(proofs[0].streamId));
      expect(proofs[3].nativeOwner, proofs[0].nativeOwner);
      expect(proofs[3].capture!.body, proofs[0].capture!.body);
      final stopped = LiveStreamCancellation.fromJson(
        object(fixture()['cancel'])['proof'],
        original(0).identity,
      );
      expect(stopped.capture!.captureOpen, isFalse);
      expect(stopped.cleanupConfirmed, isTrue);
    },
  );

  test(
    'requests detach caller mutation and hash UTF8 field order consistently',
    () {
      final raw = copy(operation(1)['request']);
      final request = LiveStreamOperation.fromJson(LiveStreamPhase.pcm, raw);
      final digest = request.digest;
      raw['pcm16_base64'] = 'AAAA';
      raw['epoch'] = 999;
      expect(request.digest, digest);
      expect(request.body['epoch'], isNot(999));
      expect(() => request.body['epoch'] = 2, throwsUnsupportedError);
      expect(
        LiveStreamOperation.fromJson(LiveStreamPhase.pcm, request.body).digest,
        digest,
      );
      final proofRaw = copy(operation(1)['proof']);
      final proof = LiveStreamProof.fromJson(proofRaw, request);
      object(proofRaw['native'])['outcome'] = 'unknown';
      expect(proof.confirmed, isTrue);
      expect(
        () => object(proof.body['native'])['outcome'] = 'unknown',
        throwsUnsupportedError,
      );
    },
  );

  test(
    'unconfirmed native operation can retain paired source without acknowledging PCM',
    () {
      final raw = copy(operation(1)['proof']);
      object(
        raw['native'],
      ).addAll({'outcome': 'publication_pending', 'result': null});
      final proof = LiveStreamProof.fromJson(raw, original(1));
      expect(proof.confirmed, isFalse);
      expect(proof.streamId, isNull);
      expect(proof.body['publication'], isNotNull);
    },
  );

  test(
    'only an exact final original proves decoder rollover, without releasing the mic',
    () {
      for (final index in [0, 1, 2]) {
        final raw = copy(operation(index)['proof']);
        object(raw['native']).addAll({
          'decoder_finished': true,
          'ownership_retired': true,
          'process_released': false,
        });
        final proof = LiveStreamProof.fromJson(raw, original(index));
        expect(proof.segmentRetired, index == 2);
        expect(proof.processReleased, isFalse);
      }
    },
  );

  test(
    'proofs reject swapped owners captures phases digests and decoder originals',
    () {
      final valid = copy(operation(1)['proof']);
      final corruptions = <void Function(Map<String, Object?>)>[
        (r) => r['schema_version'] = '1.0',
        (r) => r['protocol'] = 'converse_stream_owner_v1',
        (r) => r['owner_instance'] = 'a' * 32,
        (r) => r['capture_id'] = 'a' * 32,
        (r) => r['phase'] = 'finish',
        (r) => r['request_sha256'] = 'a' * 64,
        (r) => r['segment_index'] = 1,
        (r) => r['segment_sample_offset'] = -1,
        (r) => r['capture'] = null,
        (r) => r['publication'] = null,
        (r) => object(r['native'])['request_sha256'] = 'a' * 64,
        (r) => object(r['native'])['phase'] = 'finish',
        (r) => object(r['native'])['decoder_finished'] = 1,
        (r) => object(r['native'])['outcome'] = 'unknown',
        (r) => object(object(r['native'])['result'])['stream_id'] = 'a' * 64,
        (r) => object(object(r['native'])['result'])['worker_incarnation'] =
            'a' * 64,
        (r) => object(object(r['native'])['result'])['pcm_sequence'] = 99,
        (r) => object(object(r['native'])['result'])['received_samples'] = 99,
        (r) =>
            object(object(r['native'])['native_original'])['phase'] = 'finish',
        (r) => r['extra'] = true,
      ];
      for (final change in corruptions) {
        final raw = copy(valid);
        change(raw);
        expect(
          () => LiveStreamProof.fromJson(raw, original(1)),
          throwsFormatException,
        );
      }
    },
  );

  test(
    'capture receipt fences and scope flags cannot contradict native permission',
    () {
      final corruptions = <void Function(Map<String, Object?>)>[
        (c) => c['receipt'] = null,
        (c) => c['capture_open'] = false,
        (c) => c['scope_current'] = false,
        (c) => c['scope_current'] = 0,
        (c) => object(c['backend'])['controller_id'] = 'a' * 32,
        (c) => object(c['original'])['epoch'] = 2,
        (c) => object(c['original'])['owner_instance'] = 'a' * 32,
        (c) =>
            object(object(c['receipt'])['original'])['capture_id'] = 'a' * 32,
        (c) => object(c['receipt'])['registered_sequence'] = 1.0,
      ];
      for (final change in corruptions) {
        final raw = copy(operation(1)['proof']);
        change(object(raw['capture']));
        expect(
          () => LiveStreamProof.fromJson(raw, original(1)),
          throwsFormatException,
        );
      }
      final stopped = copy(object(fixture()['cancel'])['proof']);
      object(object(stopped['capture'])['fence'])['fence_id'] = 'invalid';
      expect(
        () => LiveStreamCancellation.fromJson(stopped, original(0).identity),
        throwsFormatException,
      );
    },
  );

  test(
    'paired publication binds exact source text revision cause order and scope',
    () {
      final corruptions = <void Function(Map<String, Object?>)>[
        (p) => object(p['original'])['capture_id'] = 'a' * 32,
        (p) => object(p['original'])['request_sha256'] = 'a' * 64,
        (p) => object(p['source'])['sequence'] = 9,
        (p) => object(p['source'])['origin'] = 'converse',
        (p) => object(object(p['source'])['payload'])['text'] = 'rewritten',
        (p) => object(object(p['source'])['payload'])['revision'] = 2,
        (p) => object(object(p['source'])['payload'])['epoch'] = 2,
        (p) => object(p['capture'])['sequence'] = 9,
        (p) => object(p['capture'])['causes'] = [],
        (p) => object(p['capture'])['task_refs'] = ['task'],
        (p) => object(object(p['capture'])['payload'])['source_event_id'] =
            'other',
        (p) =>
            object(object(p['capture'])['payload'])['input_sha256'] = 'a' * 64,
        (p) => object(
          object(p['capture'])['payload'],
        )['eligible_source_sequence'] = 9,
      ];
      for (final change in corruptions) {
        final raw = copy(operation(1)['proof']);
        change(object(raw['publication']));
        expect(
          () => LiveStreamProof.fromJson(raw, original(1)),
          throwsFormatException,
        );
      }
    },
  );

  test(
    'request bounds reject malformed PCM unsafe counters and ended speaking',
    () {
      for (final patch in [
        {'epoch': 1.0},
        {'epoch': 9007199254740992},
        {'sequence': 0},
        {'sample_offset': 960000},
        {'pcm16_base64': 'AA=='},
        {'pcm16_base64': 'AAAA\n'},
        {'pcm16_base64': base64Encode(List.filled(32002, 0))},
        {'user_speaking': 1},
        {'worker_incarnation': 'a' * 32},
        {'extra': false},
      ]) {
        expect(
          () => LiveStreamOperation.fromJson(LiveStreamPhase.pcm, {
            ...object(operation(1)['request']),
            ...patch,
          }),
          throwsFormatException,
        );
      }
      expect(
        () => LiveStreamOperation.fromJson(LiveStreamPhase.finish, {
          ...original(2).body,
          'capture_ended': true,
          'user_speaking': true,
        }),
        throwsFormatException,
      );
    },
  );

  test(
    'transport sends original once then only exact metadata recovery',
    () async {
      final requests = <http.Request>[];
      final owner = api(
        () => MockClient((request) async {
          requests.add(request);
          if (request.url.path.endsWith('/ownership')) {
            return response(fixture()['capability']);
          }
          if (request.url.path.endsWith('/cancel')) {
            return response(object(fixture()['cancel'])['proof']);
          }
          return response(operation(1)['proof']);
        }),
      );
      final request = original(1);
      expect((await owner.inspect().result).available, isTrue);
      expect((await owner.dispatch(request).result).confirmed, isTrue);
      expect((await owner.lookup(request).result).confirmed, isTrue);
      expect((await owner.reconcile(request).result).confirmed, isTrue);
      expect(
        (await owner.cancel(request, 'cancelled').result).cleanupConfirmed,
        isTrue,
      );
      expect(requests.map((r) => r.url.path.split('/').last), [
        'ownership',
        'pcm',
        'lookup',
        'reconcile',
        'cancel',
      ]);
      expect(jsonDecode(requests[1].body), request.body);
      for (final index in [2, 3]) {
        expect(jsonDecode(requests[index].body), request.lookup);
        expect(requests[index].body, isNot(contains('pcm16_base64')));
      }
      expect(
        requests.every(
          (r) =>
              r.headers['Authorization'] == 'Bearer synthetic-owner' &&
              !r.followRedirects &&
              r.url.query.isEmpty,
        ),
        isTrue,
      );
      await owner.close();
    },
  );

  test(
    'lost PCM acknowledgment remains unconfirmed and does not resend audio',
    () async {
      final paths = <String>[];
      final owner = api(
        () => MockClient((request) async {
          paths.add(request.url.path);
          return request.url.path.endsWith('/pcm')
              ? http.Response('cut', 503)
              : response(operation(1)['proof']);
        }),
      );
      final request = original(1);
      final attempt = owner.dispatch(request);
      await expectLater(
        attempt.result,
        throwsA(
          isA<OwnedSettingsFailure>().having(
            (e) => e.unconfirmed,
            'unconfirmed',
            true,
          ),
        ),
      );
      await attempt.settled;
      expect((await owner.lookup(request).result).confirmed, isTrue);
      expect(paths.where((p) => p.endsWith('/pcm')).length, 1);
      await owner.close();
    },
  );

  test(
    'strict JSON rejects duplicate unsafe and fractional identity fields',
    () async {
      for (final bytes in [
        utf8.encode('{"available":true,"available":false}'),
        utf8.encode('{"epoch":1.0}'),
        utf8.encode('{"epoch":9007199254740992}'),
        [0xff],
      ]) {
        final owner = api(
          () => MockClient(
            (_) async => http.Response.bytes(
              bytes,
              200,
              headers: {'content-type': 'application/json'},
            ),
          ),
        );
        await expectLater(
          owner.inspect().result,
          throwsA(isA<OwnedSettingsFailure>()),
        );
        await owner.close();
      }
      final bytes = utf8.encode(jsonEncode({'text': 'x' * 270000}));
      expect(() => providerJsonDecode(bytes), throwsFormatException);
      expect(
        object(providerJsonDecode(bytes, maxBytes: 448 * 1024))['text'],
        hasLength(270000),
      );
    },
  );

  test(
    'expired original send stays owned through late response retirement',
    () async {
      final send = Completer<http.StreamedResponse>();
      final client = _HeldClient(send.future);
      final owner = api(
        () => client,
        timeout: const Duration(milliseconds: 20),
      );
      final attempt = owner.dispatch(original(1));
      var settled = false;
      unawaited(attempt.settled.then((_) => settled = true));
      await expectLater(
        attempt.result,
        throwsA(
          isA<OwnedSettingsFailure>().having(
            (e) => e.unconfirmed,
            'unconfirmed',
            true,
          ),
        ),
      );
      expect(attempt.dispatched, isTrue);
      expect(settled, isFalse);
      expect(client.closes, 1);
      send.complete(
        http.StreamedResponse(
          Stream.value(utf8.encode(jsonEncode(operation(1)['proof']))),
          200,
          headers: {'content-type': 'application/json'},
        ),
      );
      await attempt.settled.timeout(const Duration(seconds: 1));
      expect(client.sends, 1);
      await owner.close();
    },
  );
}

class _HeldClient extends http.BaseClient {
  _HeldClient(this.response);
  final Future<http.StreamedResponse> response;
  int sends = 0, closes = 0;
  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) {
    sends++;
    return response;
  }

  @override
  void close() {
    closes++;
  }
}
