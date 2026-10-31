import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/agent_workspace.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/models.dart';

import 'agent_workspace_state_test.dart' show sessionJson, harnessJson;

void main() {
  test(
    'production workspace client uses owner-only typed endpoints and exact create body',
    () async {
      final requests = <http.Request>[];
      Future<http.Response> handle(http.Request request) async {
        requests.add(request);
        expect(request.headers['Authorization'], 'Bearer synthetic-owner');
        expect(request.url.toString(), isNot(contains('synthetic-owner')));
        if (request.url.path == '/v1/agent/harnesses') {
          expect(request.url.host, 'agent.invalid');
          expect(request.url.queryParameters['limit'], '50');
          return http.Response(
            jsonEncode({
              'schema_version': '1.0',
              'configured': false,
              'items': [harnessJson('seed-v1', active: true)],
              'next_cursor': null,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/v1/sessions/page') {
          expect(request.url.host, 'session.invalid');
          expect(request.url.queryParameters, {
            'channel': 'agent',
            'harness_version': 'unassociated',
            'limit': '20',
            'after': '9007199254740999',
          });
          return http.Response(
            jsonEncode({
              'schema_version': '1.0',
              'items': [sessionJson('old', harness: null)],
              'next_cursor': '17',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        expect(request.url.path, '/v1/agent/sessions');
        expect(request.method, 'POST');
        expect(jsonDecode(request.body), {
          'schema_version': '1.0',
          'harness_version': 'seed-v1',
          'name': 'Synthetic work',
        });
        return http.Response(
          jsonEncode(sessionJson('new')),
          200,
          headers: {'content-type': 'application/json'},
        );
      }

      final client = KinaiyaClient(
        sessionBase: 'https://session.invalid',
        agentBase: 'https://agent.invalid',
        converseBase: 'https://converse.invalid',
        ownerToken: 'synthetic-owner',
        httpClient: MockClient(handle),
        agentWorkspaceClientFactory: () => MockClient(handle),
      );
      addTearDown(client.close);
      expect((await client.agentHarnesses()).items.single.kind, 'seed_runtime');
      expect(
        (await client.agentSessions(
          harnessVersion: 'unassociated',
          after: '9007199254740999',
        )).items.single.harnessVersion,
        isNull,
      );
      expect(
        (await client.createAgentSession(
          'seed-v1',
          name: 'Synthetic work',
        )).harnessVersion,
        'seed-v1',
      );
      expect(requests.length, 3);
    },
  );
  test('additive native session and task pins retain historical null', () {
    expect(SessionInfo.fromJson(sessionJson('s')).harnessVersion, 'seed-v1');
    expect(
      SessionInfo.fromJson(
        sessionJson('s')..remove('harness_version'),
      ).harnessVersion,
      isNull,
    );
    final task = {
      'schema_version': '1.0',
      'task_id': 't',
      'session_id': 's',
      'thread_id': 'thread-s',
      'epoch': 1,
      'objective': 'Synthetic objective',
      'objective_revision': 1,
      'state': 'running',
      'idempotency_key': 'k',
      'harness_version': 'selector-v1',
    };
    expect(AgentTask.fromJson(task).harnessVersion, 'selector-v1');
    expect(
      AgentTask.fromJson(task..remove('harness_version')).harnessVersion,
      isNull,
    );
  });
  test(
    'strict pages reject malformed counters, cursors, identities and booleans',
    () {
      for (final malformed in [
        {
          'schema_version': '2.0',
          'configured': false,
          'items': [],
          'next_cursor': null,
        },
        {
          'schema_version': '1.0',
          'configured': 'false',
          'items': [],
          'next_cursor': null,
        },
        {
          'schema_version': '1.0',
          'configured': false,
          'items': [harnessJson('seed-v1')..['selectable'] = 'true'],
          'next_cursor': null,
        },
        {
          'schema_version': '1.0',
          'configured': false,
          'items': [harnessJson('seed-v1'), harnessJson('seed-v1')],
          'next_cursor': null,
        },
      ]) {
        expect(
          () => AgentHarnessPage.fromJson(malformed),
          throwsFormatException,
        );
      }
      for (final malformed in [
        {
          'schema_version': '1.0',
          'items': [sessionJson('s')..['epoch'] = 1.5],
          'next_cursor': null,
        },
        {
          'schema_version': '1.0',
          'items': [sessionJson('s')..['channel'] = 'converse'],
          'next_cursor': null,
        },
        {
          'schema_version': '1.0',
          'items': [sessionJson('s')],
          'next_cursor': 17,
        },
        {
          'schema_version': '1.0',
          'items': [sessionJson('s')],
          'next_cursor': '-1',
        },
        {
          'schema_version': '1.0',
          'items': [sessionJson('s'), sessionJson('s')],
          'next_cursor': null,
        },
      ]) {
        expect(
          () => AgentSessionPage.fromJson(malformed),
          throwsFormatException,
        );
      }
    },
  );
  test(
    'native selectable is normative even for approved baseline after promotion',
    () {
      final item = AgentHarness.fromJson(
        harnessJson('baseline')..['eligibility'] = 'approved_baseline',
      );
      expect(item.selectable, isTrue);
      expect(item.active, isFalse);
      final inspected = AgentHarness.fromJson(
        harnessJson('unapproved', selectable: false),
      );
      expect(inspected.selectable, isFalse);
    },
  );
}
