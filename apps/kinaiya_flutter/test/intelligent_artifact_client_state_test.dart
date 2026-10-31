/// Native transport and Converse ownership; all payloads are synthetic.
library;

import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/artifact_client.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/presentation/preferences.dart';
import 'package:kinaiya_flutter/state/app_state.dart';

import 'support/intelligent_artifact_fixtures.dart';

SessionInfo session({int epoch = 1, String id = 'session-test'}) => SessionInfo(
  sessionId: id,
  threadId: 'thread-test',
  channel: 'converse',
  epoch: epoch,
);

http.Response response(Object? value, {int status = 200}) => http.Response(
  jsonEncode(value),
  status,
  headers: {'content-type': 'application/json'},
);

Map<String, Object?> page(List<Object?> items, {int next = 1}) => {
  'items': items,
  'next_after': next,
};

Map<String, Object?> choiceFixture() => artifactFixture(
  components: [
    artifactComponent('choice', 'select', {
      'label': 'Scenario',
      'options': ['A', 'B'],
      'value': 'A',
    }),
  ],
);

KinaiyaClient artifactTransport(
  FutureOr<http.Response> Function(http.Request) handler, {
  FutureOr<http.Response> Function(http.Request)? primary,
  String agentBase = 'http://agent.test',
}) => KinaiyaClient.mock(
  sessionBase: 'http://session.test',
  agentBase: agentBase,
  converseBase: 'http://converse.test',
  mockTransport: MockClient(
    (request) async => primary == null ? response([]) : await primary(request),
  ),
  artifactClientFactory: () =>
      MockClient((request) async => await handler(request)),
);

AppState owner(
  FutureOr<http.Response> Function(http.Request) handler, {
  FutureOr<http.Response> Function(http.Request)? primary,
  DateTime Function()? now,
  ConnectionProfile? profile,
}) {
  final api = artifactTransport(handler, primary: primary);
  final state = AppState(artifactNow: now)
    ..connect(profile ?? ConnectionProfile.defaults(), injected: api);
  state.converseSession = session();
  addTearDown(state.dispose);
  return state;
}

void main() {
  test(
    'artifact API canonical path/auth/scope; actions never put credentials in URLs',
    () async {
      final requests = <http.Request>[];
      final artifact = choiceFixture();
      final api = ArtifactApi(
        base: ' https://agent.test/proxy/ ',
        ownerToken: 'synthetic-owner',
        clientFactory: () => MockClient((request) async {
          requests.add(request);
          return response(
            request.url.path.endsWith('/actions') ? artifact : page([artifact]),
          );
        }),
      );
      addTearDown(api.close);
      final result = await api.page(session());
      await api.action(
        session(),
        result.items.single,
        'choice',
        'B',
        'synthetic-action',
      );
      expect(requests.first.url.path, '/proxy/v1/artifacts');
      expect(requests.first.url.queryParameters, {
        'session_id': 'session-test',
        'thread_id': 'thread-test',
        'epoch': '1',
        'after': '0',
        'limit': '20',
      });
      expect(
        requests.last.url.path,
        '/proxy/v1/artifacts/artifact-test/actions',
      );
      expect(
        requests.every(
          (r) =>
              r.headers['authorization'] == 'Bearer synthetic-owner' &&
              !r.url.toString().contains('synthetic-owner') &&
              !r.followRedirects,
        ),
        isTrue,
      );
      expect(
        jsonDecode(requests.last.body),
        containsPair('idempotency_key', 'synthetic-action'),
      );
    },
  );

  test(
    'one unsupported artifact falls back without losing a valid sibling',
    () async {
      final unsupported = artifactFixture(id: 'future')
        ..['schema_version'] = '99.0';
      unsupported['components'] = [
        {'untrusted': 'not executable'},
      ];
      final api = ArtifactApi(
        base: 'https://agent.test',
        ownerToken: 'fixture',
        clientFactory: () => MockClient(
          (_) async => response(page([unsupported, artifactFixture()])),
        ),
      );
      addTearDown(api.close);
      final result = await api.page(session());
      expect(result.items.length, 2);
      expect(result.items.first.supported, isFalse);
      expect(result.items.first.components, isEmpty);
      expect(result.items.last.supported, isTrue);
    },
  );

  test(
    'cross-scope and cursor regression fail before data can be displayed',
    () async {
      final wrong = artifactFixture()..['session_id'] = 'other-session';
      var value = page([wrong]);
      final api = ArtifactApi(
        base: 'https://agent.test',
        ownerToken: 'fixture',
        clientFactory: () => MockClient((_) async => response(value)),
      );
      addTearDown(api.close);
      await expectLater(api.page(session()), throwsA(isA<ArtifactFailure>()));
      value = page([artifactFixture()], next: 2);
      await expectLater(
        api.page(session(), after: 2),
        throwsA(isA<ArtifactFailure>()),
      );
    },
  );

  test(
    'stream byte bound and redirects reject without reflecting response secrets',
    () async {
      for (final status in [302, 500]) {
        final api = ArtifactApi(
          base: 'https://agent.test',
          ownerToken: 'fixture',
          clientFactory: () => MockClient(
            (_) async => http.Response(
              'secret-response',
              status,
              headers: {'location': 'https://elsewhere.test'},
            ),
          ),
        );
        try {
          await api.page(session());
          fail('Expected rejection');
        } catch (error) {
          expect(error.toString(), isNot(contains('secret-response')));
        } finally {
          api.close();
        }
      }
      final api = ArtifactApi(
        base: 'https://agent.test',
        ownerToken: 'fixture',
        clientFactory: () => MockClient(
          (_) async => http.Response('x' * (2 * 1024 * 1024 + 1), 200),
        ),
      );
      addTearDown(api.close);
      await expectLater(api.page(session()), throwsA(isA<ArtifactFailure>()));
    },
  );

  test(
    'native high-water prevents older tasks reopening after close; chooser can reopen',
    () async {
      var rows = [artifactFixture(id: 'new', submitSequence: 20)];
      var cursor = 1;
      final state = owner((_) async => response(page(rows, next: cursor)));
      await state.refreshArtifacts();
      expect(state.activeArtifact?.artifactId, 'new');
      state.closeArtifact();
      rows = [artifactFixture(id: 'old-late', submitSequence: 10)];
      cursor = 2;
      await state.refreshArtifacts();
      expect(state.activeArtifact, isNull);
      state.chooseArtifact('new');
      expect(state.activeArtifact?.artifactId, 'new');
      state.pinArtifact(true);
      rows = [artifactFixture(id: 'newer', submitSequence: 30)];
      cursor = 3;
      await state.refreshArtifacts();
      expect(state.activeArtifact?.artifactId, 'new');
      state.pinArtifact(false);
      rows = [];
      await state.refreshArtifacts();
      state.chooseArtifact('newer');
      expect(state.activeArtifact?.artifactId, 'newer');
    },
  );

  test(
    'a missing detail does not starve later native artifact updates',
    () async {
      final detailReads = <String>[];
      var pageReads = 0;
      final state = owner(
        (request) async {
          final name = request.url.pathSegments.last;
          if (name == 'artifacts') {
            return response(
              page(
                pageReads++ == 0 ? [artifactFixture(id: 'kept')] : [],
                next: 1,
              ),
            );
          }
          detailReads.add(name);
          return name == 'missing'
              ? response({}, status: 404)
              : response(artifactFixture(id: 'kept', revision: 2));
        },
        primary: (_) async => response([
          for (final (index, id) in ['missing', 'kept'].indexed)
            {
              'event_id': 'event-$id',
              'sequence': index + 1,
              'session_id': 'session-test',
              'thread_id': 'thread-test',
              'epoch': 1,
              'kind': 'artifact.updated',
              'origin': 'agent-supervisor',
              'payload': {'artifact_id': id},
            },
        ]),
      );
      await state.refreshArtifacts();
      await state.refreshNotices();
      // refreshNotices also starts one bounded page read. Let it settle before
      // explicitly requesting the queued native detail references.
      await Future<void>.delayed(Duration.zero);
      await state.refreshArtifacts();
      expect(detailReads, ['missing', 'kept']);
      expect(state.activeArtifact?.revision, 2);
      await state.refreshArtifacts();
      expect(detailReads.where((id) => id == 'missing').length, 1);
    },
  );

  test('unknown native ordering requires manual selection', () async {
    final state = owner(
      (_) async => response(page([artifactFixture(submitSequence: 0)])),
    );
    await state.refreshArtifacts();
    expect(state.activeArtifact, isNull);
    state.chooseArtifact('artifact-test');
    expect(state.activeArtifact, isNotNull);
  });

  test('new connection and epoch drop late artifact completion', () async {
    final pending = Completer<http.Response>();
    final state = owner((_) => pending.future);
    final read = state.refreshArtifacts();
    state.converseSession = session(epoch: 2);
    pending.complete(response(page([artifactFixture()])));
    await read;
    expect(state.converseArtifacts, isEmpty);
    state.disconnect();
    expect(state.converseArtifacts, isEmpty);
  });

  test(
    '500 after commit retains exact action key; replay reconciles without a second effect',
    () async {
      final keys = <String>[];
      final state = owner((request) async {
        if (request.method == 'GET') return response(page([choiceFixture()]));
        final body = jsonDecode(request.body) as Map;
        keys.add(body['idempotency_key'] as String);
        if (keys.length == 1) {
          return response({'detail': 'hidden'}, status: 500);
        }
        final result = choiceFixture()..['view_revision'] = 1;
        result['view_state'] = {'choice': 'B'};
        return response(result);
      });
      await state.refreshArtifacts();
      await state.interactWithArtifact('choice', 'B');
      expect(state.artifactActionUncertain, isTrue);
      state.closeArtifact();
      expect(state.activeArtifact, isNotNull);
      await state.interactWithArtifact('choice', 'B');
      expect(keys.length, 2);
      expect(keys.first, keys.last);
      expect(state.artifactActionUncertain, isFalse);
      expect(state.activeArtifact?.viewRevision, 1);
    },
  );

  test(
    'definitive stale revision rejects and does not retain uncertain action',
    () async {
      final state = owner(
        (r) async => r.method == 'GET'
            ? response(page([choiceFixture()]))
            : response({}, status: 409),
      );
      await state.refreshArtifacts();
      await state.interactWithArtifact('choice', 'B');
      expect(state.artifactActionUncertain, isFalse);
      expect(state.artifactError, contains('changed'));
    },
  );

  test(
    'same backend reconnect restores the exact uncertain intent for explicit retry',
    () async {
      final bodies = <Map<String, Object?>>[];
      final state = owner((request) async {
        if (request.method == 'GET') return response(page([choiceFixture()]));
        bodies.add((jsonDecode(request.body) as Map).cast<String, Object?>());
        return response({}, status: 500);
      });
      await state.refreshArtifacts();
      await state.interactWithArtifact('choice', 'B');
      final original = bodies.single;
      state.disconnect();
      state.connect(
        ConnectionProfile.defaults(),
        injected: artifactTransport((request) async {
          if (request.method == 'GET') return response(page([choiceFixture()]));
          bodies.add((jsonDecode(request.body) as Map).cast<String, Object?>());
          return response(
            choiceFixture()
              ..['view_revision'] = 1
              ..['view_state'] = {'choice': 'B'},
          );
        }),
      );
      state.converseSession = session();
      await state.refreshArtifacts();
      expect(state.artifactActionUncertain, isTrue);
      expect(state.artifactActionPending, isFalse);
      expect(
        state.activeArtifact?.artifactId,
        original['artifact_id'] ?? 'artifact-test',
      );
      expect(
        bodies.length,
        1,
        reason: 'reconnect and reads never replay a POST',
      );
      await state.interactWithArtifact('choice', 'A');
      expect(
        bodies.length,
        1,
        reason: 'a different value cannot bypass reconciliation',
      );
      await state.interactWithArtifact('choice', 'B');
      expect(bodies.length, 2);
      expect(bodies.last, original);
      expect(state.artifactActionUncertain, isFalse);
      expect(state.activeArtifact?.viewState['choice'], 'B');
    },
  );

  test(
    'in-flight reconnect retry keeps its original key and old completion cannot own it',
    () async {
      final oldReply = Completer<http.Response>(),
          newReply = Completer<http.Response>();
      final keys = <String>[];
      final state = owner((request) async {
        if (request.method == 'GET') return response(page([choiceFixture()]));
        keys.add(
          (jsonDecode(request.body) as Map)['idempotency_key'] as String,
        );
        return oldReply.future;
      });
      await state.refreshArtifacts();
      final oldAction = state.interactWithArtifact('choice', 'B');
      await Future<void>.delayed(Duration.zero);
      expect(state.artifactActionPending, isTrue);
      state.connect(
        ConnectionProfile.defaults(),
        injected: artifactTransport((request) async {
          if (request.method == 'GET') return response(page([choiceFixture()]));
          keys.add(
            (jsonDecode(request.body) as Map)['idempotency_key'] as String,
          );
          return newReply.future;
        }),
      );
      state.converseSession = session();
      await state.refreshArtifacts();
      expect(state.artifactActionPending, isFalse);
      expect(state.artifactActionUncertain, isTrue);
      expect(keys.length, 1);
      final retry = state.interactWithArtifact('choice', 'B');
      await Future<void>.delayed(Duration.zero);
      expect(state.artifactActionPending, isTrue);
      expect(keys.length, 2);
      expect(keys.first, keys.last);
      final updated = choiceFixture()
        ..['view_revision'] = 1
        ..['view_state'] = {'choice': 'B'};
      oldReply.complete(response(updated));
      await oldAction;
      expect(state.artifactActionPending, isTrue);
      expect(state.activeArtifact?.viewRevision, 0);
      newReply.complete(response(updated));
      await retry;
      expect(state.artifactActionPending, isFalse);
      expect(state.artifactActionUncertain, isFalse);
      expect(state.activeArtifact?.viewRevision, 1);
    },
  );

  for (final changed in ['host', 'credential', 'session', 'thread', 'epoch']) {
    test('retained intents stay private when $changed changes', () async {
      final keys = <String>[];
      final state = owner((request) async {
        if (request.method == 'GET') return response(page([choiceFixture()]));
        keys.add(
          (jsonDecode(request.body) as Map)['idempotency_key'] as String,
        );
        return response({}, status: 500);
      });
      await state.refreshArtifacts();
      await state.interactWithArtifact('choice', 'B');
      final profile = ConnectionProfile(
        sessionUrl: defaultSessionUrl,
        agentUrl: changed == 'host' ? 'http://127.0.0.1:9999' : defaultAgentUrl,
        converseUrl: defaultConverseUrl,
        ownerToken: changed == 'credential' ? 'different-synthetic-owner' : '',
      );
      final next = SessionInfo(
        sessionId: changed == 'session' ? 'next-session' : 'session-test',
        threadId: changed == 'thread' ? 'next-thread' : 'thread-test',
        epoch: changed == 'epoch' ? 2 : 1,
        channel: 'converse',
      );
      final raw = choiceFixture()
        ..['artifact_id'] = 'fresh-result'
        ..['session_id'] = next.sessionId
        ..['thread_id'] = next.threadId
        ..['epoch'] = next.epoch;
      state.connect(
        profile,
        injected: artifactTransport(
          (request) async {
            if (request.method == 'GET') return response(page([raw]));
            keys.add(
              (jsonDecode(request.body) as Map)['idempotency_key'] as String,
            );
            return response({
              ...raw,
              'view_revision': 1,
              'view_state': {'choice': 'B'},
            });
          },
          agentBase: changed == 'host'
              ? 'http://next-agent.test'
              : 'http://agent.test',
        ),
      );
      state.converseSession = next;
      await state.refreshArtifacts();
      expect(state.artifactActionUncertain, isFalse);
      expect(state.converseArtifacts.map((value) => value.artifactId), [
        'fresh-result',
      ]);
      expect(keys.length, 1);
      await state.interactWithArtifact('choice', 'B');
      expect(keys.length, 2);
      expect(keys.first, isNot(keys.last));
    });
  }

  test(
    'private retained intent capacity refuses new writes without eviction',
    () async {
      final state = AppState();
      addTearDown(state.dispose);
      var posts = 0;
      for (var i = 0; i <= 32; i++) {
        final scoped = session(id: 'scope-$i');
        final raw = choiceFixture()..['session_id'] = scoped.sessionId;
        state.connect(
          ConnectionProfile.defaults(),
          injected: artifactTransport((request) async {
            if (request.method == 'GET') return response(page([raw]));
            posts++;
            return response({}, status: 500);
          }),
        );
        state.converseSession = scoped;
        await state.refreshArtifacts();
        await state.interactWithArtifact('choice', 'B');
      }
      expect(posts, 32);
      expect(state.artifactError, contains('local limit'));
    },
  );

  test(
    'editing defers new results and never starts an inference submission',
    () async {
      var rows = [artifactFixture()];
      var cursor = 1;
      final methods = <String>[];
      final state = owner((r) async {
        methods.add(r.method);
        return response(page(rows, next: cursor));
      });
      await state.refreshArtifacts();
      state.editArtifact(true);
      rows = [artifactFixture(id: 'next', submitSequence: 20)];
      cursor = 2;
      await state.refreshArtifacts();
      expect(state.activeArtifact?.artifactId, 'artifact-test');
      expect(methods, everyElement('GET'));
      state.chooseArtifact('next');
      expect(state.activeArtifact?.artifactId, 'artifact-test');
      state.editArtifact(false);
      state.chooseArtifact('next');
      expect(state.activeArtifact?.artifactId, 'next');
    },
  );

  test('temporary 503 is not a permanent unsupported capability', () async {
    var available = false;
    var now = DateTime.utc(2026, 10, 30);
    final state = owner(
      (_) async => available
          ? response(page([artifactFixture()]))
          : response({}, status: 503),
      now: () => now,
    );
    await state.refreshArtifacts();
    expect(state.activeArtifact, isNull);
    available = true;
    now = now.add(const Duration(seconds: 6));
    await state.refreshArtifacts();
    expect(state.activeArtifact, isNotNull);
  });

  test(
    'Ask only prefills a draft and preserves artifact disclosure provenance',
    () async {
      final raw = artifactFixture()..['content_scope'] = 'local_only';
      final methods = <String>[];
      final state = owner((r) async {
        methods.add(r.method);
        return response(page([raw]));
      });
      await state.refreshArtifacts();
      expect(
        state.prefillArtifactQuestion('Explain this synthetic result.'),
        isTrue,
      );
      expect(state.converseDraft, 'Explain this synthetic result.');
      expect(methods, ['GET']);
      expect(state.draftScope(AppView.character), ContentScope.localOnly);
    },
  );

  for (final scenario in [
    (
      name: 'public result and empty draft use the selected public scope',
      artifactScope: 'public_synthetic',
      priorDraft: '',
      expected: ContentScope.publicSynthetic,
    ),
    (
      name: 'local result keeps an empty public draft local',
      artifactScope: 'local_only',
      priorDraft: '',
      expected: ContentScope.localOnly,
    ),
    (
      name: 'public result preserves an existing local draft',
      artifactScope: 'public_synthetic',
      priorDraft: 'Existing local note.',
      expected: ContentScope.localOnly,
    ),
    (
      name: 'private result keeps its private provenance',
      artifactScope: 'private_cloud_authorized',
      priorDraft: '',
      expected: ContentScope.privateCloudAuthorized,
    ),
  ]) {
    test('Ask ${scenario.name}', () async {
      final raw = artifactFixture()..['content_scope'] = scenario.artifactScope;
      final methods = <String>[];
      final state = owner(
        (request) async {
          methods.add(request.method);
          return response(page([raw]));
        },
        profile: ConnectionProfile(
          sessionUrl: defaultSessionUrl,
          agentUrl: defaultAgentUrl,
          converseUrl: defaultConverseUrl,
          ownerToken: '',
          backendProfile: BackendProfile.metaContributor,
        ),
      );
      state.converseDraft = scenario.priorDraft;
      await state.refreshArtifacts();
      expect(
        state.prefillArtifactQuestion('Explain this synthetic result.'),
        isTrue,
      );
      expect(
        state.converseDraft,
        scenario.priorDraft.isEmpty
            ? 'Explain this synthetic result.'
            : '${scenario.priorDraft}\nExplain this synthetic result.',
      );
      expect(state.draftScope(AppView.character), scenario.expected);
      expect(methods, ['GET']);
    });
  }

  test(
    'saved rich-rendering preference migrates every released shape without reset',
    () {
      final current = const PresentationPreferences(
        reducedMotion: true,
        showCaptions: false,
        intelligentUi: false,
      ).toJson();
      expect(PresentationPreferences.fromJson(current).intelligentUi, isFalse);
      final legacy = Map<String, dynamic>.from(current)
        ..remove('intelligent_ui');
      expect(PresentationPreferences.fromJson(legacy).intelligentUi, isTrue);
      expect(PresentationPreferences.fromJson(legacy).reducedMotion, isTrue);
      expect(PresentationPreferences.fromJson(legacy).showCaptions, isFalse);
      final gap = Map<String, dynamic>.from(current)
        ..remove('speech_mouth_motion');
      expect(
        () => PresentationPreferences.fromJson(gap),
        throwsFormatException,
      );
    },
  );
}
