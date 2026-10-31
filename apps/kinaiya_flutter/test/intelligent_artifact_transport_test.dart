import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:kinaiya_flutter/api/artifact_client.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/artifacts/models.dart';

import 'support/intelligent_artifact_fixtures.dart';

const _timeout = Duration(milliseconds: 15);
const _cleanup = Duration(milliseconds: 5);
const _secret = 'synthetic-transport-secret';

SessionInfo _session() => SessionInfo(
  sessionId: 'session-test',
  threadId: 'thread-test',
  channel: 'converse',
  epoch: 1,
);

String _page() => jsonEncode({'items': [], 'next_after': 0});

class _HeldSend extends http.BaseClient {
  final response = Completer<http.StreamedResponse>();
  final requests = <http.BaseRequest>[];
  int closes = 0;

  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) {
    requests.add(request);
    return response.future;
  }

  @override
  void close() => closes++;
}

class _Body extends http.BaseClient {
  _Body({this.status = 200, this.declared});
  final int status;
  final int? declared;
  final cancellation = Completer<void>();
  int listens = 0, cancels = 0, closes = 0;
  late final stream = StreamController<List<int>>(
    onListen: () => listens++,
    onCancel: () {
      cancels++;
      return cancellation.future;
    },
  );

  http.StreamedResponse get response => http.StreamedResponse(
    stream.stream,
    status,
    contentLength: declared,
    headers: {'content-type': 'application/json', 'x-private-fixture': _secret},
  );

  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) async =>
      response;

  @override
  void close() => closes++;

  Future<void> finish() async {
    if (!cancellation.isCompleted) cancellation.complete();
    await stream.close();
  }
}

class _Completed extends http.BaseClient {
  _Completed(
    this.body, {
    this.status = 200,
    this.throwOnClose = false,
    this.declared,
    this.declareLength = true,
  });
  final String body;
  final int status;
  final bool throwOnClose;
  final bool declareLength;
  final int? declared;
  final requests = <http.BaseRequest>[];
  int closes = 0;

  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) async {
    requests.add(request);
    final bytes = utf8.encode(body);
    return http.StreamedResponse(
      Stream.value(bytes),
      status,
      contentLength: declareLength ? declared ?? bytes.length : null,
      headers: {'content-type': 'application/json'},
    );
  }

  @override
  void close() {
    closes++;
    if (throwOnClose) throw StateError(_secret);
  }
}

ArtifactApi _api(http.Client Function() factory) => ArtifactApi(
  base: 'https://agent.invalid/prefix/',
  ownerToken: 'synthetic-owner',
  clientFactory: factory,
  timeout: _timeout,
  cleanupTimeout: _cleanup,
);

Matcher _failure({bool unconfirmed = false, int? status}) =>
    isA<ArtifactFailure>()
        .having(
          (e) => e.toString(),
          'private error text',
          isNot(contains(_secret)),
        )
        .having((e) => e.status, 'status', status)
        .having(
          (e) => e.message,
          'message',
          unconfirmed ? contains('unconfirmed') : isNotEmpty,
        );

Future<void> _flush() => Future<void>.delayed(const Duration(milliseconds: 10));

void main() {
  testWidgets(
    'completed bodies and absent readers leave no cleanup deadline on shutdown',
    (tester) async {
      final completed = _Completed(_page());
      final api = _api(() => completed);
      expect((await api.page(_session())).items, isEmpty);
      await api.close();
      expect(completed.closes, 1);

      final held = _HeldSend();
      final closing = _api(() => held);
      final pending = expectLater(
        closing.page(_session()),
        throwsA(_failure(unconfirmed: true)),
      );
      await closing.close();
      held.response.complete(_Completed(_page()).send(held.requests.single));
      await pending;
      expect(held.closes, 1);
      await tester.pump();
    },
  );

  test(
    'natural EOF frees each owner and closes only its own transport once',
    () async {
      final clients = <_Completed>[];
      final api = _api(() {
        final client = _Completed(_page());
        clients.add(client);
        return client;
      });
      addTearDown(api.close);
      for (var i = 0; i < 5; i++) {
        expect((await api.page(_session())).items, isEmpty);
      }
      expect(clients.length, 5);
      expect(clients.every((client) => client.closes == 1), isTrue);
      for (final client in clients) {
        final request = client.requests.single;
        expect(request.url.path, '/prefix/v1/artifacts');
        expect(request.headers['Authorization'], 'Bearer synthetic-owner');
        expect(request.followRedirects, isFalse);
        expect(request.url.toString(), isNot(contains('synthetic-owner')));
      }
    },
  );

  test(
    'two timed-out sends retain slots and own every late response until cancellation settles',
    () async {
      final clients = <_HeldSend>[];
      var recovered = false;
      final api = _api(() {
        if (recovered) return _Completed(_page());
        final client = _HeldSend();
        clients.add(client);
        return client;
      });
      addTearDown(api.close);
      await Future.wait(
        List.generate(
          2,
          (_) => expectLater(
            api.page(_session()),
            throwsA(_failure(unconfirmed: true)),
          ),
        ),
      );
      expect(clients.length, 2);
      expect(clients.every((client) => client.closes == 1), isTrue);
      await expectLater(api.page(_session()), throwsA(_failure()));
      expect(clients.length, 2);
      final bodies = List.generate(2, (_) => _Body());
      for (var i = 0; i < clients.length; i++) {
        clients[i].response.complete(bodies[i].response);
      }
      await _flush();
      expect(
        bodies.every((body) => body.listens == 1 && body.cancels == 1),
        isTrue,
      );
      await expectLater(api.page(_session()), throwsA(_failure()));
      expect(clients.length, 2);
      await Future.wait(bodies.map((body) => body.finish()));
      await _flush();
      recovered = true;
      expect((await api.page(_session())).items, isEmpty);
    },
  );

  for (final refusal in ['status', 'declared length']) {
    test(
      '$refusal refusal first owns the stream; stalled cleanup retains both slots',
      () async {
        final clients = <_Body>[];
        var recovered = false;
        final api = _api(() {
          if (recovered) return _Completed(_page());
          final client = _Body(
            status: refusal == 'status' ? 409 : 200,
            declared: refusal == 'declared length' ? 2 * 1024 * 1024 + 1 : null,
          );
          clients.add(client);
          return client;
        });
        addTearDown(api.close);
        await Future.wait(
          List.generate(
            2,
            (_) => expectLater(
              api.page(_session()),
              throwsA(_failure(unconfirmed: true)),
            ),
          ),
        );
        expect(clients.length, 2);
        expect(
          clients.every(
            (client) =>
                client.listens == 1 &&
                client.cancels == 1 &&
                client.closes == 1,
          ),
          isTrue,
        );
        await expectLater(api.page(_session()), throwsA(_failure()));
        expect(clients.length, 2);
        await Future.wait(clients.map((client) => client.finish()));
        await _flush();
        recovered = true;
        expect((await api.page(_session())).items, isEmpty);
      },
    );
  }

  test(
    'a stalled body has a bounded deadline without freeing unsettled cancellation',
    () async {
      final clients = <_Body>[];
      var recovered = false;
      final api = _api(() {
        if (recovered) return _Completed(_page());
        final client = _Body();
        clients.add(client);
        return client;
      });
      addTearDown(api.close);
      await Future.wait(
        List.generate(
          2,
          (_) => expectLater(
            api.page(_session()),
            throwsA(_failure(unconfirmed: true)),
          ),
        ),
      );
      expect(clients.every((client) => client.cancels == 1), isTrue);
      await expectLater(api.page(_session()), throwsA(_failure()));
      expect(clients.length, 2);
      await Future.wait(clients.map((client) => client.finish()));
      await _flush();
      recovered = true;
      expect((await api.page(_session())).items, isEmpty);
    },
  );

  test(
    'throwing client close cannot release capacity or leak its exception',
    () async {
      final clients = <_Completed>[];
      final api = _api(() {
        final client = _Completed(_page(), throwOnClose: true);
        clients.add(client);
        return client;
      });
      addTearDown(api.close);
      for (var i = 0; i < 2; i++) {
        await expectLater(
          api.page(_session()),
          throwsA(_failure(unconfirmed: true)),
        );
      }
      await expectLater(api.page(_session()), throwsA(_failure()));
      expect(clients.length, 2);
      await api.close();
      expect(clients.every((client) => client.closes == 1), isTrue);
    },
  );

  test(
    'definitive status is preserved only after owned cleanup succeeds',
    () async {
      final client = _Completed(_secret, status: 409);
      final api = _api(() => client);
      addTearDown(api.close);
      await expectLater(api.page(_session()), throwsA(_failure(status: 409)));
      expect(client.closes, 1);
    },
  );

  test(
    'streamed byte bound and exact declared length fail without decoding private data',
    () async {
      for (final sample in [
        _Completed(
          'x' * (intelligentArtifactMaxBytes + 1),
          declareLength: false,
        ),
        _Completed(_secret, declared: _secret.length + 1),
        _Completed(_secret),
      ]) {
        final api = _api(() => sample);
        await expectLater(
          api.detail(_session(), 'artifact-test'),
          throwsA(_failure()),
        );
        await api.close();
        expect(sample.closes, 1);
      }
    },
  );

  test(
    'POST timeout never retries and explicit replay preserves the supplied idempotency key',
    () async {
      final held = _HeldSend();
      final raw = artifactFixture(
        components: [
          artifactComponent('choice', 'select', {
            'label': 'Scenario',
            'options': ['A', 'B'],
            'value': 'A',
          }),
        ],
      );
      final artifact = IntelligentArtifact.fromJson(raw);
      final result = {
        ...raw,
        'view_revision': 1,
        'view_state': {'choice': 'B'},
      };
      final completed = _Completed(jsonEncode(result));
      var count = 0;
      final api = _api(() => count++ == 0 ? held : completed);
      addTearDown(api.close);
      await expectLater(
        api.action(_session(), artifact, 'choice', 'B', 'exact-replay-key'),
        throwsA(_failure(unconfirmed: true)),
      );
      await _flush();
      expect(held.requests.length, 1);
      expect(count, 1);
      held.response.complete(
        _Completed(jsonEncode(result)).send(held.requests.single),
      );
      await _flush();
      final replay = await api.action(
        _session(),
        artifact,
        'choice',
        'B',
        'exact-replay-key',
      );
      expect(replay.viewState['choice'], 'B');
      expect(count, 2);
      final first = held.requests.single as http.Request;
      final second = completed.requests.single as http.Request;
      expect(first.method, 'POST');
      expect(first.body, second.body);
      expect(
        jsonDecode(first.body),
        containsPair('idempotency_key', 'exact-replay-key'),
      );
    },
  );

  test(
    'closing artifact API owns late sends without closing the task transport',
    () async {
      final task = _Completed('[]');
      final held = _HeldSend();
      final api = KinaiyaClient(
        sessionBase: 'https://agent.invalid',
        agentBase: 'https://agent.invalid',
        converseBase: 'https://agent.invalid',
        ownerToken: 'synthetic-owner',
        httpClient: task,
        artifactClientFactory: () => held,
      );
      addTearDown(api.close);
      final read = expectLater(
        api.artifacts.page(_session()),
        throwsA(_failure(unconfirmed: true)),
      );
      await api.artifacts.close();
      expect(task.closes, 0);
      expect(await api.listTasks(), isEmpty);
      expect(task.closes, 0);
      final body = _Body();
      held.response.complete(body.response);
      await read;
      expect(body.listens, 1);
      expect(body.cancels, 1);
      await body.finish();
      await _flush();
      expect(held.closes, 1);
    },
  );
}
