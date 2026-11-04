import 'dart:async';
import 'dart:convert';
import 'dart:io' show HttpOverrides, Platform;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/companion_client.dart';
import 'package:kinaiya_flutter/api/companion_models.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/companion/client.dart';
import 'package:kinaiya_flutter/companion/models.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/converse_companions.dart';

// Public synthetic companion fixtures; no live models, audio, or credentials.
Map<String, Object?> _deviceJson({
  String id = 'device-1',
  String name = 'Test phone',
  String label = 'Foreground companion',
  String sessionId = 'session-1',
  String threadId = 'thread-1',
  int epoch = 1,
  int createdAt = 1000,
  int expiresAt = 87400,
  int? revokedAt,
  String status = 'active',
}) => <String, Object?>{
  'schema_version': '1.0',
  'device_id': id,
  'device_name': name,
  'label': label,
  'scope': 'companion_control',
  'session_id': sessionId,
  'thread_id': threadId,
  'epoch': epoch,
  'created_at': createdAt,
  'expires_at': expiresAt,
  'revoked_at': revokedAt,
  'status': status,
};

Map<String, Object?> _callJson({
  String id = 'call-1',
  int sequence = 1,
  String deviceId = 'device-1',
  String sessionId = 'session-1',
  String threadId = 'thread-1',
  int epoch = 1,
  String title = 'Companion call',
  String state = 'ringing',
  int revision = 1,
  int createdAt = 1000,
  int ringingDeadline = 1030,
  int? mediaDeadline,
  String? endReason,
  String note = '',
}) => <String, Object?>{
  'schema_version': '1.0',
  'call_id': id,
  'sequence': sequence,
  'device_id': deviceId,
  'session_id': sessionId,
  'thread_id': threadId,
  'epoch': epoch,
  'title': title,
  'state': state,
  'revision': revision,
  'created_at': createdAt,
  'ringing_deadline': ringingDeadline,
  'media_deadline': state == 'accepted_waiting_media'
      ? (mediaDeadline ?? 1045)
      : mediaDeadline,
  'end_reason': endReason,
  'note': note,
};

http.Response _jsonRes(Object body, {int status = 200}) => http.Response.bytes(
  utf8.encode(jsonEncode(body)),
  status,
  headers: const <String, String>{'content-type': 'application/json'},
);

class _HeldCancelTransport extends http.BaseClient {
  final Completer<void> cancelCompleter = Completer<void>();
  bool closed = false;

  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) async {
    late StreamController<List<int>> controller;
    controller = StreamController<List<int>>(
      onListen: () => controller.add(List<int>.filled(262145, 120)),
      onCancel: () => cancelCompleter.future,
    );
    return http.StreamedResponse(
      controller.stream,
      200,
      headers: const <String, String>{'content-type': 'application/json'},
    );
  }

  @override
  void close() {
    closed = true;
  }
}

AppState _connectedState(
  FutureOr<http.Response> Function(http.Request) companionHandler, {
  SessionInfo? converseSession,
}) {
  final state = AppState();
  state.connected = true;
  state.sessionAuthenticated = true;
  state.converseSession =
      converseSession ??
      SessionInfo(
        sessionId: 'session-1',
        threadId: 'thread-1',
        channel: 'converse',
        epoch: 1,
        name: 'Converse',
        harnessVersion: 'seed-v1',
      );
  state.converseDraft = 'Keep my converse draft';
  state.agentDraft = 'Keep my agent draft';
  state.client = KinaiyaClient.mock(
    sessionBase: 'https://host.invalid',
    agentBase: 'https://host.invalid',
    converseBase: 'https://host.invalid',
    mockTransport: MockClient((_) async => _jsonRes(<String, Object?>{})),
    companionClientFactory: () =>
        MockClient((req) async => await companionHandler(req)),
  );
  return state;
}

void main() {
  setUp(resetCompanionUncertaintyForTest);

  test(
    'OwnerCompanionBinding projects Converse session without leaking channel or harness fields',
    () {
      final converse = SessionInfo(
        sessionId: 'session-1',
        threadId: 'thread-1',
        channel: 'converse',
        epoch: 2,
        name: 'My session',
        harnessVersion: 'seed-v1',
      );
      final binding = OwnerCompanionBinding.fromSession(converse);
      expect(binding.toJson(), <String, Object?>{
        'schema_version': '1.0',
        'session_id': 'session-1',
        'thread_id': 'thread-1',
        'epoch': 2,
      });

      final agent = SessionInfo(
        sessionId: 'agent-1',
        threadId: 'thread-a',
        channel: 'agent',
        epoch: 1,
        harnessVersion: 'seed-v1',
      );
      expect(
        () => OwnerCompanionBinding.fromSession(agent),
        throwsFormatException,
      );
    },
  );

  test(
    'owner DTOs parse active, expired, revoked, and stale devices while device parser stays active-only',
    () {
      final directory = decodeOwnerCompanionDevicePage(<String, Object?>{
        'schema_version': '1.0',
        'devices': <Object?>[
          _deviceJson(id: 'd-active', status: 'active'),
          _deviceJson(id: 'd-expired', status: 'expired'),
          _deviceJson(id: 'd-revoked', status: 'revoked', revokedAt: 2000),
          _deviceJson(id: 'd-stale', status: 'stale', epoch: 9),
        ],
      });
      expect(
        directory.map((d) => d.status).toList(),
        <OwnerCompanionDeviceStatus>[
          OwnerCompanionDeviceStatus.active,
          OwnerCompanionDeviceStatus.expired,
          OwnerCompanionDeviceStatus.revoked,
          OwnerCompanionDeviceStatus.stale,
        ],
      );
      // Phone CompanionDevice parser still rejects non-active grants.
      for (final status in const <String>['expired', 'revoked', 'stale']) {
        expect(
          () => CompanionDevice.fromJson(
            _deviceJson(
              id: 'd-$status',
              status: status,
              revokedAt: status == 'revoked' ? 2000 : null,
            ),
          ),
          throwsFormatException,
        );
      }
    },
  );

  test(
    'OwnerCompanionApi enforces JSON/wire bounds, monotonic paging, and bounded stream-cancel ownership',
    () async {
      final binding = OwnerCompanionBinding('session-1', 'thread-1', 1);
      final held = _HeldCancelTransport();
      final heldApi = OwnerCompanionApi(
        sessionBase: 'https://host.invalid',
        ownerToken: 'o' * 32,
        clientFactory: () => held,
        cleanupTimeout: const Duration(milliseconds: 50),
      );
      await expectLater(
        heldApi.inviteCall(
          binding,
          deviceId: 'device-1',
          title: 'Synthetic invite',
          idempotencyKey: 'key-1',
        ),
        throwsA(
          isA<OwnerCompanionFailure>().having(
            (e) => e.unconfirmed,
            'unconfirmed',
            isTrue,
          ),
        ),
      );
      expect(held.closed, isTrue);
      held.cancelCompleter.complete();
      await heldApi.close();

      // Duplicate JSON key rejection and 5-page 100-call monotonic paging.
      final duplicateApi = OwnerCompanionApi(
        sessionBase: 'https://host.invalid',
        ownerToken: 'o' * 32,
        clientFactory: () => MockClient(
          (_) async => http.Response(
            '{"schema_version":"1.0","devices":[],"devices":[]}',
            200,
            headers: const <String, String>{'content-type': 'application/json'},
          ),
        ),
      );
      await expectLater(
        duplicateApi.listDevices(),
        throwsA(isA<OwnerCompanionFailure>()),
      );
      await duplicateApi.close();

      final pagedApi = OwnerCompanionApi(
        sessionBase: 'https://host.invalid',
        ownerToken: 'o' * 32,
        clientFactory: () => MockClient((req) async {
          final after = int.parse(req.url.queryParameters['after'] ?? '0');
          final rows = List<Map<String, Object?>>.generate(
            20,
            (i) => _callJson(
              id: 'call-${after + i + 1}',
              sequence: after + i + 1,
              note: '"' * 4096,
            ),
          );
          final nextAfter = after + 20 < 100 ? after + 20 : null;
          return _jsonRes(<String, Object?>{
            'schema_version': '1.0',
            'calls': rows,
            'next_after': nextAfter,
          });
        }),
      );
      final allCalls = await pagedApi.listCalls(binding);
      expect(allCalls.length, 100);
      expect(allCalls.first.sequence, 1);
      expect(allCalls.last.sequence, 100);
      await pagedApi.close();

      // Unfiltered listCalls pages across >5 pages (101 calls across 2 devices,
      // and 3,200 calls across 32 devices with 100 calls per device) while
      // enforcing the 100-per-device and 3,200-total bounds.
      OwnerCompanionApi makeMultiDeviceApi(
        int totalCalls, {
        int callsPerDevice = 100,
      }) => OwnerCompanionApi(
        sessionBase: 'https://host.invalid',
        ownerToken: 'o' * 32,
        clientFactory: () => MockClient((req) async {
          final after = int.parse(req.url.queryParameters['after'] ?? '0');
          final remaining = totalCalls - after;
          final count = remaining > 20 ? 20 : (remaining > 0 ? remaining : 0);
          final rows = List<Map<String, Object?>>.generate(count, (i) {
            final seq = after + i + 1;
            final deviceIndex = ((seq - 1) ~/ callsPerDevice) + 1;
            return _callJson(
              id: 'call-$seq',
              sequence: seq,
              deviceId: 'device-$deviceIndex',
              state: seq == totalCalls ? 'ringing' : 'ended',
              endReason: seq == totalCalls ? null : 'owner_ended',
            );
          });
          final nextAfter = after + count < totalCalls ? after + count : null;
          return _jsonRes(<String, Object?>{
            'schema_version': '1.0',
            'calls': rows,
            'next_after': nextAfter,
          });
        }),
      );

      final api101 = makeMultiDeviceApi(101);
      final calls101 = await api101.listCalls(binding);
      expect(calls101.length, 101);
      expect(calls101.last.callId, 'call-101');
      expect(calls101.last.deviceId, 'device-2');
      expect(calls101.last.state, OwnerCompanionCallState.ringing);
      await api101.close();

      final api3200 = makeMultiDeviceApi(3200);
      final calls3200 = await api3200.listCalls(binding);
      expect(calls3200.length, 3200);
      expect(calls3200.last.sequence, 3200);
      expect(calls3200.last.deviceId, 'device-32');
      expect(calls3200.last.state, OwnerCompanionCallState.ringing);
      await api3200.close();

      final apiSingleDevice101 = makeMultiDeviceApi(101, callsPerDevice: 101);
      await expectLater(
        apiSingleDevice101.listCalls(binding),
        throwsA(isA<OwnerCompanionFailure>()),
      );
      await apiSingleDevice101.close();

      final api3201 = makeMultiDeviceApi(3201);
      await expectLater(
        api3201.listCalls(binding),
        throwsA(isA<OwnerCompanionFailure>()),
      );
      await api3201.close();
    },
  );

  test(
    'owner inventory validates history before projecting current epoch',
    () async {
      final binding = OwnerCompanionBinding('session-1', 'thread-1', 2);
      final api = OwnerCompanionApi(
        sessionBase: 'https://host.invalid',
        ownerToken: 'o' * 32,
        clientFactory: () => MockClient((req) async {
          final after = int.parse(req.url.queryParameters['after']!);
          final end = after + 20 < 101 ? after + 20 : 101;
          return _jsonRes(<String, Object?>{
            'schema_version': '1.0',
            'calls': <Object?>[
              for (var index = after; index < end; index++)
                _callJson(
                  id: 'call-${index + 1}',
                  sequence: index + 1,
                  deviceId: index < 100 ? 'old-device' : 'current-device',
                  epoch: index < 100 ? 1 : 2,
                  state: index < 100 ? 'ended' : 'ringing',
                  endReason: index < 100 ? 'session_invalidated' : null,
                ),
            ],
            'next_after': end < 101 ? end : null,
          });
        }),
      );
      addTearDown(api.close);
      final calls = await api.listCalls(binding);
      expect(calls.single.callId, 'call-101');
      // The public page method continues to require the exact requested binding.
      await expectLater(
        api.listCallsPage(binding),
        throwsA(isA<OwnerCompanionFailure>()),
      );
    },
  );

  test(
    'owner inventory refuses foreign rows and native capacity violations',
    () async {
      final binding = OwnerCompanionBinding('session-1', 'thread-1', 2);
      final invalidInventories = <List<Map<String, Object?>>>[
        <Map<String, Object?>>[_callJson(sessionId: 'other-session')],
        <Map<String, Object?>>[_callJson(threadId: 'other-thread')],
        <Map<String, Object?>>[_callJson(epoch: 3)],
        <Map<String, Object?>>[_callJson(deviceId: 'other-device', epoch: 2)],
        List<Map<String, Object?>>.generate(
          101,
          (index) => _callJson(id: 'call-$index', sequence: index + 1),
        ),
        List<Map<String, Object?>>.generate(
          33,
          (index) => _callJson(
            id: 'call-$index',
            sequence: index + 1,
            deviceId: 'device-$index',
          ),
        ),
      ];
      for (
        var caseIndex = 0;
        caseIndex < invalidInventories.length;
        caseIndex++
      ) {
        final rows = invalidInventories[caseIndex];
        final api = OwnerCompanionApi(
          sessionBase: 'https://host.invalid',
          ownerToken: 'o' * 32,
          clientFactory: () => MockClient((req) async {
            final after = int.parse(req.url.queryParameters['after']!);
            final end = after + 20 < rows.length ? after + 20 : rows.length;
            return _jsonRes(<String, Object?>{
              'schema_version': '1.0',
              'calls': rows.sublist(after, end),
              'next_after': end < rows.length ? end : null,
            });
          }),
        );
        addTearDown(api.close);
        await expectLater(
          api.listCalls(binding, deviceId: caseIndex == 3 ? 'device-1' : null),
          throwsA(isA<OwnerCompanionFailure>()),
          reason: 'invalid inventory case $caseIndex',
        );
      }
    },
  );

  testWidgets(
    'Companions panel opens read-only, pairs, invites, updates note, ends, revokes, and preserves drafts',
    (tester) async {
      String? copiedText;
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(SystemChannels.platform, (call) async {
            if (call.method == 'Clipboard.setData') {
              final args = call.arguments as Map<dynamic, dynamic>;
              copiedText = args['text'] as String?;
            }
            return null;
          });
      addTearDown(
        () => TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
            .setMockMethodCallHandler(SystemChannels.platform, null),
      );

      var now = 1000;
      final requests = <String>[];
      final devices = <Map<String, Object?>>[
        _deviceJson(id: 'device-1', name: 'Pixel 9', status: 'active'),
        _deviceJson(
          id: 'device-stale',
          name: 'Old tablet',
          sessionId: 'session-old',
          epoch: 3,
          status: 'stale',
        ),
      ];
      final calls = <Map<String, Object?>>[];

      final state = _connectedState((req) {
        requests.add('${req.method} ${req.url.path}');
        if (req.method == 'GET' && req.url.path == '/v1/companion/devices') {
          return _jsonRes(<String, Object?>{
            'schema_version': '1.0',
            'devices': devices,
          });
        }
        if (req.method == 'GET' && req.url.path == '/v1/companion/calls') {
          return _jsonRes(<String, Object?>{
            'schema_version': '1.0',
            'calls': calls,
            'next_after': null,
          });
        }
        if (req.method == 'POST' && req.url.path == '/v1/companion/pairings') {
          final body = jsonDecode(req.body) as Map<String, Object?>;
          expect(
            body.keys.toSet(),
            equals(<String>{
              'schema_version',
              'session_id',
              'thread_id',
              'epoch',
              'label',
            }),
          );
          return _jsonRes(<String, Object?>{
            'schema_version': '1.0',
            'session_id': 'session-1',
            'thread_id': 'thread-1',
            'epoch': 1,
            'label': body['label'],
            'pairing_code': 'p' * 43,
            'expires_at': now + 120,
          });
        }
        if (req.method == 'POST' && req.url.path == '/v1/companion/calls') {
          final body = jsonDecode(req.body) as Map<String, Object?>;
          final created = _callJson(
            id: 'call-1',
            sequence: 1,
            deviceId: body['device_id'] as String,
            title: body['title'] as String,
            state: 'accepted_waiting_media',
            revision: 2,
            mediaDeadline: now + 15,
          );
          calls
            ..clear()
            ..add(created);
          return _jsonRes(created);
        }
        if (req.method == 'POST' &&
            req.url.path == '/v1/companion/calls/call-1/note') {
          final body = jsonDecode(req.body) as Map<String, Object?>;
          final updated = _callJson(
            id: 'call-1',
            sequence: 1,
            state: 'accepted_waiting_media',
            revision: (body['expected_revision'] as int) + 1,
            mediaDeadline: now + 15,
            note: body['note'] as String,
          );
          calls
            ..clear()
            ..add(updated);
          return _jsonRes(updated);
        }
        if (req.method == 'POST' &&
            req.url.path == '/v1/companion/calls/call-1/end') {
          final body = jsonDecode(req.body) as Map<String, Object?>;
          final ended = _callJson(
            id: 'call-1',
            sequence: 1,
            state: 'ended',
            revision: (body['expected_revision'] as int) + 1,
            endReason: 'owner_ended',
            note: calls.single['note'] as String,
          );
          calls
            ..clear()
            ..add(ended);
          return _jsonRes(ended);
        }
        if (req.method == 'POST' &&
            req.url.path == '/v1/companion/devices/device-stale/revoke') {
          final revoked = _deviceJson(
            id: 'device-stale',
            name: 'Old tablet',
            sessionId: 'session-old',
            epoch: 3,
            status: 'revoked',
            revokedAt: now,
          );
          devices[1] = revoked;
          return _jsonRes(revoked);
        }
        return _jsonRes(<String, Object?>{'detail': 'unexpected'}, status: 404);
      });

      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: ConverseCompanionsDialog(
              state: state,
              nowSeconds: () => now,
              pollInterval: Duration.zero,
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      // Opening only performs GET reads and preserves drafts.
      expect(
        requests,
        equals(<String>[
          'GET /v1/companion/devices',
          'GET /v1/companion/calls',
        ]),
      );
      expect(state.converseDraft, 'Keep my converse draft');
      expect(state.agentDraft, 'Keep my agent draft');

      // Cross-session device is read-only except revocation.
      expect(
        find.byKey(const ValueKey('companion-invite-device-stale')),
        findsNothing,
      );
      expect(
        find.byKey(const ValueKey('companion-revoke-device-stale')),
        findsOneWidget,
      );

      // Issue pairing code and copy.
      await tester.ensureVisible(
        find.byKey(const ValueKey('companion-issue-pairing')),
      );
      await tester.tap(find.byKey(const ValueKey('companion-issue-pairing')));
      await tester.pumpAndSettle();
      expect(
        find.byKey(const ValueKey('companion-pairing-code')),
        findsOneWidget,
      );
      expect(find.textContaining('Expires in 120s'), findsOneWidget);
      await tester.ensureVisible(
        find.byKey(const ValueKey('companion-copy-pairing-code')),
      );
      await tester.tap(
        find.byKey(const ValueKey('companion-copy-pairing-code')),
      );
      await tester.pumpAndSettle();
      expect(copiedText, 'p' * 43);

      // Invite active companion device.
      await tester.ensureVisible(
        find.byKey(const ValueKey('companion-invite-device-1')),
      );
      await tester.tap(find.byKey(const ValueKey('companion-invite-device-1')));
      await tester.pumpAndSettle();
      expect(
        find.text('Accepted · waiting for voice connection'),
        findsOneWidget,
      );
      expect(
        find.byKey(const ValueKey('companion-call-waiting-note-call-1')),
        findsOneWidget,
      );

      // Update literal note (including HTML/JSON-looking text).
      const literalSnippet = '<script>alert("x")</script> {"cmd":"noop"}';
      await tester.ensureVisible(
        find.byKey(const ValueKey('companion-note-input-call-1')),
      );
      await tester.enterText(
        find.byKey(const ValueKey('companion-note-input-call-1')),
        literalSnippet,
      );
      await tester.ensureVisible(
        find.byKey(const ValueKey('companion-save-note-call-1')),
      );
      await tester.tap(
        find.byKey(const ValueKey('companion-save-note-call-1')),
      );
      await tester.pumpAndSettle();
      expect(
        tester
            .widget<SelectableText>(
              find.byKey(const ValueKey('companion-call-note-call-1')),
            )
            .data,
        literalSnippet,
      );

      // End call and revoke stale device.
      await tester.ensureVisible(
        find.byKey(const ValueKey('companion-end-call-1')),
      );
      await tester.tap(find.byKey(const ValueKey('companion-end-call-1')));
      await tester.pumpAndSettle();
      expect(find.text('Ended by host owner'), findsOneWidget);

      await tester.ensureVisible(
        find.byKey(const ValueKey('companion-revoke-device-stale')),
      );
      await tester.tap(
        find.byKey(const ValueKey('companion-revoke-device-stale')),
      );
      await tester.pumpAndSettle();
      expect(find.text('Revoked · other conversation'), findsOneWidget);
      state.dispose();
    },
  );

  testWidgets(
    'retains uncertain invite, end, and note fences across close/reopen and requires GET before another note CAS edit',
    (tester) async {
      final inviteKeys = <String>[];
      final endKeys = <String>[];
      var failInvite = true;
      var failNote = true;
      var failEnd = true;
      final calls = <Map<String, Object?>>[];

      final state = _connectedState((req) {
        if (req.method == 'GET' && req.url.path == '/v1/companion/devices') {
          return _jsonRes(<String, Object?>{
            'schema_version': '1.0',
            'devices': <Object?>[_deviceJson()],
          });
        }
        if (req.method == 'GET' && req.url.path == '/v1/companion/calls') {
          return _jsonRes(<String, Object?>{
            'schema_version': '1.0',
            'calls': calls,
            'next_after': null,
          });
        }
        if (req.method == 'POST' && req.url.path == '/v1/companion/calls') {
          final body = jsonDecode(req.body) as Map<String, Object?>;
          inviteKeys.add(body['idempotency_key'] as String);
          if (failInvite) {
            return _jsonRes(<String, Object?>{'detail': 'error'}, status: 503);
          }
          final created = _callJson(id: 'call-1', sequence: 1, revision: 1);
          calls
            ..clear()
            ..add(created);
          return _jsonRes(created);
        }
        if (req.method == 'POST' &&
            req.url.path == '/v1/companion/calls/call-1/note') {
          final body = jsonDecode(req.body) as Map<String, Object?>;
          if (failNote) {
            return _jsonRes(<String, Object?>{'detail': 'error'}, status: 503);
          }
          final updated = _callJson(
            id: 'call-1',
            sequence: 1,
            revision: (body['expected_revision'] as int) + 1,
            note: body['note'] as String,
          );
          calls
            ..clear()
            ..add(updated);
          return _jsonRes(updated);
        }
        if (req.method == 'GET' &&
            req.url.path == '/v1/companion/calls/call-1') {
          return _jsonRes(calls.single);
        }
        if (req.method == 'POST' &&
            req.url.path == '/v1/companion/calls/call-1/end') {
          final body = jsonDecode(req.body) as Map<String, Object?>;
          endKeys.add(body['idempotency_key'] as String);
          if (failEnd) {
            return _jsonRes(<String, Object?>{'detail': 'error'}, status: 503);
          }
          final ended = _callJson(
            id: 'call-1',
            sequence: 1,
            state: 'ended',
            revision: (body['expected_revision'] as int) + 1,
            endReason: 'owner_ended',
            note: calls.single['note'] as String,
          );
          calls
            ..clear()
            ..add(ended);
          return _jsonRes(ended);
        }
        return _jsonRes(<String, Object?>{'detail': 'not found'}, status: 404);
      });

      Widget buildDialog(String keyName) => MaterialApp(
        home: Scaffold(
          body: ConverseCompanionsDialog(
            key: ValueKey(keyName),
            state: state,
            pollInterval: Duration.zero,
          ),
        ),
      );

      await tester.pumpWidget(buildDialog('open-1'));
      await tester.pumpAndSettle();

      // 1. Trigger uncertain invite, close and reopen, then retry with same key.
      await tester.ensureVisible(
        find.byKey(const ValueKey('companion-invite-device-1')),
      );
      await tester.tap(find.byKey(const ValueKey('companion-invite-device-1')));
      await tester.pumpAndSettle();
      expect(inviteKeys.length, 1);
      expect(
        find.byKey(const ValueKey('companion-retry-invite')),
        findsOneWidget,
      );

      // Unmount (close) and remount (reopen).
      await tester.pumpWidget(const MaterialApp(home: SizedBox.shrink()));
      await tester.pumpWidget(buildDialog('open-2'));
      await tester.pumpAndSettle();
      expect(
        find.byKey(const ValueKey('companion-retry-invite')),
        findsOneWidget,
      );

      failInvite = false;
      await tester.ensureVisible(
        find.byKey(const ValueKey('companion-retry-invite')),
      );
      await tester.tap(find.byKey(const ValueKey('companion-retry-invite')));
      await tester.pumpAndSettle();
      expect(inviteKeys.length, 2);
      expect(inviteKeys[1], equals(inviteKeys[0]));

      // 2. Trigger uncertain note edit: fences Save note until GET reconcile.
      await tester.ensureVisible(
        find.byKey(const ValueKey('companion-note-input-call-1')),
      );
      await tester.enterText(
        find.byKey(const ValueKey('companion-note-input-call-1')),
        'Retained note draft',
      );
      await tester.ensureVisible(
        find.byKey(const ValueKey('companion-save-note-call-1')),
      );
      await tester.tap(
        find.byKey(const ValueKey('companion-save-note-call-1')),
      );
      await tester.pumpAndSettle();
      expect(
        tester
            .widget<FilledButton>(
              find.byKey(const ValueKey('companion-save-note-call-1')),
            )
            .onPressed,
        isNull,
      );
      expect(find.text('Reconcile call'), findsOneWidget);

      // Reconcile via GET /v1/companion/calls/call-1 unlocks CAS save while preserving draft.
      failNote = false;
      await tester.ensureVisible(
        find.byKey(const ValueKey('companion-inspect-call-1')),
      );
      await tester.tap(find.byKey(const ValueKey('companion-inspect-call-1')));
      await tester.pumpAndSettle();
      expect(
        tester
            .widget<FilledButton>(
              find.byKey(const ValueKey('companion-save-note-call-1')),
            )
            .onPressed,
        isNotNull,
      );
      await tester.ensureVisible(
        find.byKey(const ValueKey('companion-save-note-call-1')),
      );
      await tester.tap(
        find.byKey(const ValueKey('companion-save-note-call-1')),
      );
      await tester.pumpAndSettle();
      expect(find.text('Retained note draft'), findsWidgets);

      // 3. Trigger uncertain end, close/reopen, and retry with same key.
      await tester.ensureVisible(
        find.byKey(const ValueKey('companion-end-call-1')),
      );
      await tester.tap(find.byKey(const ValueKey('companion-end-call-1')));
      await tester.pumpAndSettle();
      expect(endKeys.length, 1);

      await tester.pumpWidget(const MaterialApp(home: SizedBox.shrink()));
      await tester.pumpWidget(buildDialog('open-3'));
      await tester.pumpAndSettle();
      expect(
        find.byKey(const ValueKey('companion-retry-end-call-1')),
        findsOneWidget,
      );

      failEnd = false;
      await tester.ensureVisible(
        find.byKey(const ValueKey('companion-retry-end-call-1')),
      );
      await tester.tap(
        find.byKey(const ValueKey('companion-retry-end-call-1')),
      );
      await tester.pumpAndSettle();
      expect(endKeys.length, 2);
      expect(endKeys[1], equals(endKeys[0]));
      expect(find.text('Ended by host owner'), findsOneWidget);
      state.dispose();
    },
  );

  testWidgets(
    'retains in-flight invite, note, and end uncertainty when dialog closes or refreshes before unconfirmed response arrives',
    (tester) async {
      final inviteKeys = <String>[];
      final endKeys = <String>[];
      Completer<http.Response>? pendingInvite;
      Completer<http.Response>? pendingNote;
      Completer<http.Response>? pendingEnd;
      final calls = <Map<String, Object?>>[];

      final state = _connectedState((req) {
        if (req.method == 'GET' && req.url.path == '/v1/companion/devices') {
          return _jsonRes(<String, Object?>{
            'schema_version': '1.0',
            'devices': <Object?>[_deviceJson()],
          });
        }
        if (req.method == 'GET' && req.url.path == '/v1/companion/calls') {
          return _jsonRes(<String, Object?>{
            'schema_version': '1.0',
            'calls': calls,
            'next_after': null,
          });
        }
        if (req.method == 'POST' && req.url.path == '/v1/companion/calls') {
          final body = jsonDecode(req.body) as Map<String, Object?>;
          inviteKeys.add(body['idempotency_key'] as String);
          final gate = pendingInvite;
          if (gate != null) {
            return gate.future;
          }
          final created = _callJson(id: 'call-1', sequence: 1, revision: 1);
          calls
            ..clear()
            ..add(created);
          return _jsonRes(created);
        }
        if (req.method == 'POST' &&
            req.url.path == '/v1/companion/calls/call-1/note') {
          final body = jsonDecode(req.body) as Map<String, Object?>;
          final gate = pendingNote;
          if (gate != null) {
            return gate.future;
          }
          final updated = _callJson(
            id: 'call-1',
            sequence: 1,
            revision: (body['expected_revision'] as int) + 1,
            note: body['note'] as String,
          );
          calls
            ..clear()
            ..add(updated);
          return _jsonRes(updated);
        }
        if (req.method == 'GET' &&
            req.url.path == '/v1/companion/calls/call-1') {
          return _jsonRes(calls.single);
        }
        if (req.method == 'POST' &&
            req.url.path == '/v1/companion/calls/call-1/end') {
          final body = jsonDecode(req.body) as Map<String, Object?>;
          endKeys.add(body['idempotency_key'] as String);
          final gate = pendingEnd;
          if (gate != null) {
            return gate.future;
          }
          final ended = _callJson(
            id: 'call-1',
            sequence: 1,
            state: 'ended',
            revision: (body['expected_revision'] as int) + 1,
            endReason: 'owner_ended',
            note: calls.single['note'] as String,
          );
          calls
            ..clear()
            ..add(ended);
          return _jsonRes(ended);
        }
        return _jsonRes(<String, Object?>{'detail': 'not found'}, status: 404);
      });

      Widget buildDialog(String keyName) => MaterialApp(
        home: Scaffold(
          body: ConverseCompanionsDialog(
            key: ValueKey(keyName),
            state: state,
            pollInterval: Duration.zero,
          ),
        ),
      );

      await tester.pumpWidget(buildDialog('inflight-1'));
      await tester.pumpAndSettle();

      // 1. Start invite, close dialog while POST is still in flight, then complete 503.
      pendingInvite = Completer<http.Response>();
      await tester.ensureVisible(
        find.byKey(const ValueKey('companion-invite-device-1')),
      );
      await tester.tap(find.byKey(const ValueKey('companion-invite-device-1')));
      await tester.pump();
      expect(inviteKeys.length, 1);

      // Unmount before response completes.
      await tester.pumpWidget(const MaterialApp(home: SizedBox.shrink()));
      pendingInvite.complete(
        _jsonRes(<String, Object?>{'detail': 'error'}, status: 503),
      );
      pendingInvite = null;
      await tester.pump();

      // Reopen dialog: retained invite uncertainty surfaces and retries with original key.
      await tester.pumpWidget(buildDialog('inflight-2'));
      await tester.pumpAndSettle();
      expect(
        find.byKey(const ValueKey('companion-retry-invite')),
        findsOneWidget,
      );
      await tester.ensureVisible(
        find.byKey(const ValueKey('companion-retry-invite')),
      );
      await tester.tap(find.byKey(const ValueKey('companion-retry-invite')));
      await tester.pumpAndSettle();
      expect(inviteKeys.length, 2);
      expect(inviteKeys[1], equals(inviteKeys[0]));

      // 2. Start note CAS update, close dialog while POST is in flight, then complete 503.
      pendingNote = Completer<http.Response>();
      await tester.ensureVisible(
        find.byKey(const ValueKey('companion-note-input-call-1')),
      );
      await tester.enterText(
        find.byKey(const ValueKey('companion-note-input-call-1')),
        'In-flight note draft',
      );
      await tester.ensureVisible(
        find.byKey(const ValueKey('companion-save-note-call-1')),
      );
      await tester.tap(
        find.byKey(const ValueKey('companion-save-note-call-1')),
      );
      await tester.pump();

      await tester.pumpWidget(const MaterialApp(home: SizedBox.shrink()));
      await tester.pumpWidget(buildDialog('inflight-3'));
      await tester.pumpAndSettle();
      pendingNote.complete(
        _jsonRes(<String, Object?>{'detail': 'error'}, status: 503),
      );
      pendingNote = null;
      await tester.pump();
      expect(
        tester
            .widget<FilledButton>(
              find.byKey(const ValueKey('companion-save-note-call-1')),
            )
            .onPressed,
        isNull,
      );
      expect(find.text('Reconcile call'), findsOneWidget);

      await tester.ensureVisible(
        find.byKey(const ValueKey('companion-inspect-call-1')),
      );
      await tester.tap(find.byKey(const ValueKey('companion-inspect-call-1')));
      await tester.pumpAndSettle();
      expect(
        tester
            .widget<FilledButton>(
              find.byKey(const ValueKey('companion-save-note-call-1')),
            )
            .onPressed,
        isNotNull,
      );

      // 3. Start end call, close dialog while POST is in flight, then complete 503.
      pendingEnd = Completer<http.Response>();
      await tester.ensureVisible(
        find.byKey(const ValueKey('companion-end-call-1')),
      );
      await tester.tap(find.byKey(const ValueKey('companion-end-call-1')));
      await tester.pump();
      expect(endKeys.length, 1);

      await tester.pumpWidget(const MaterialApp(home: SizedBox.shrink()));
      pendingEnd.complete(
        _jsonRes(<String, Object?>{'detail': 'error'}, status: 503),
      );
      pendingEnd = null;
      await tester.pump();

      await tester.pumpWidget(buildDialog('inflight-4'));
      await tester.pumpAndSettle();
      expect(
        find.byKey(const ValueKey('companion-retry-end-call-1')),
        findsOneWidget,
      );
      await tester.ensureVisible(
        find.byKey(const ValueKey('companion-retry-end-call-1')),
      );
      await tester.tap(
        find.byKey(const ValueKey('companion-retry-end-call-1')),
      );
      await tester.pumpAndSettle();
      expect(endKeys.length, 2);
      expect(endKeys[1], equals(endKeys[0]));
      expect(find.text('Ended by host owner'), findsOneWidget);
      state.dispose();
    },
  );

  testWidgets('inspecting another call cannot reconcile an uncertain note', (
    tester,
  ) async {
    final calls = <Map<String, Object?>>[
      _callJson(),
      _callJson(
        id: 'call-2',
        sequence: 2,
        state: 'ended',
        endReason: 'owner_ended',
      ),
    ];
    final inspected = <String>[];
    final state = _connectedState((req) {
      if (req.method == 'POST') {
        return _jsonRes(<String, Object?>{}, status: 503);
      }
      if (req.url.path == '/v1/companion/devices') {
        return _jsonRes(<String, Object?>{
          'schema_version': '1.0',
          'devices': <Object?>[_deviceJson()],
        });
      }
      if (req.url.path == '/v1/companion/calls') {
        return _jsonRes(<String, Object?>{
          'schema_version': '1.0',
          'calls': calls,
          'next_after': null,
        });
      }
      inspected.add(req.url.path);
      return _jsonRes(
        calls.firstWhere(
          (call) => req.url.path.endsWith('/${call['call_id']}'),
        ),
      );
    });
    addTearDown(state.dispose);
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: ConverseCompanionsDialog(
            state: state,
            pollInterval: Duration.zero,
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();
    Future<void> tap(String key) async {
      final finder = find.byKey(ValueKey(key));
      await tester.ensureVisible(finder);
      await tester.tap(finder);
      await tester.pumpAndSettle();
    }

    await tester.ensureVisible(
      find.byKey(const ValueKey('companion-note-input-call-1')),
    );
    await tester.enterText(
      find.byKey(const ValueKey('companion-note-input-call-1')),
      'Uncertain A',
    );
    await tap('companion-save-note-call-1');
    await tap('companion-inspect-call-2');
    final saveA = find.byKey(const ValueKey('companion-save-note-call-1'));
    expect(tester.widget<FilledButton>(saveA).onPressed, isNull);
    expect(inspected, <String>['/v1/companion/calls/call-2']);
    await tap('companion-inspect-call-1');
    expect(tester.widget<FilledButton>(saveA).onPressed, isNotNull);
  });

  testWidgets('a GET started before note settlement cannot release its fence', (
    tester,
  ) async {
    final write = Completer<http.Response>();
    final oldRead = Completer<http.Response>();
    var listReads = 0;
    final oldCall = _callJson();
    http.Response page() => _jsonRes(<String, Object?>{
      'schema_version': '1.0',
      'calls': <Object?>[oldCall],
      'next_after': null,
    });
    final state = _connectedState((req) async {
      if (req.method == 'POST') return write.future;
      if (req.url.path == '/v1/companion/devices') {
        return _jsonRes(<String, Object?>{
          'schema_version': '1.0',
          'devices': <Object?>[_deviceJson()],
        });
      }
      if (req.url.path == '/v1/companion/calls') {
        listReads++;
        return listReads == 2 ? oldRead.future : page();
      }
      return _jsonRes(oldCall);
    });
    addTearDown(() {
      if (!write.isCompleted) {
        write.complete(_jsonRes(<String, Object?>{}, status: 503));
      }
      if (!oldRead.isCompleted) oldRead.complete(page());
      state.dispose();
    });
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: ConverseCompanionsDialog(
            state: state,
            pollInterval: Duration.zero,
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();
    Future<void> tap(String key) async {
      final finder = find.byKey(ValueKey(key));
      await tester.ensureVisible(finder);
      await tester.tap(finder);
      await tester.pump();
    }

    await tester.ensureVisible(
      find.byKey(const ValueKey('companion-note-input-call-1')),
    );
    await tester.enterText(
      find.byKey(const ValueKey('companion-note-input-call-1')),
      'Pending note',
    );
    await tap('companion-save-note-call-1');
    await tap('companion-refresh');
    expect(listReads, 2);
    write.complete(_jsonRes(<String, Object?>{}, status: 503));
    await tester.pump();
    await tester.pump();
    oldRead.complete(page());
    await tester.pumpAndSettle();
    final save = find.byKey(const ValueKey('companion-save-note-call-1'));
    expect(tester.widget<FilledButton>(save).onPressed, isNull);
    await tap('companion-inspect-call-1');
    await tester.pumpAndSettle();
    expect(tester.widget<FilledButton>(save).onPressed, isNotNull);
  });

  testWidgets(
    '320px short screen at 2x text keeps stop and revoke controls reachable without overflow',
    (tester) async {
      await tester.binding.setSurfaceSize(const Size(320, 480));
      addTearDown(() => tester.binding.setSurfaceSize(null));

      final state = _connectedState((req) {
        if (req.url.path == '/v1/companion/devices') {
          return _jsonRes(<String, Object?>{
            'schema_version': '1.0',
            'devices': <Object?>[_deviceJson()],
          });
        }
        return _jsonRes(<String, Object?>{
          'schema_version': '1.0',
          'calls': <Object?>[_callJson()],
          'next_after': null,
        });
      });

      await tester.pumpWidget(
        MaterialApp(
          builder: (context, child) => MediaQuery(
            data: MediaQuery.of(
              context,
            ).copyWith(textScaler: const TextScaler.linear(2.0)),
            child: child!,
          ),
          home: Scaffold(
            body: ConverseCompanionsDialog(
              state: state,
              pollInterval: Duration.zero,
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);

      await tester.ensureVisible(
        find.byKey(const ValueKey('companion-revoke-device-1')),
      );
      expect(tester.takeException(), isNull);
      await tester.ensureVisible(
        find.byKey(const ValueKey('companion-end-call-1')),
      );
      expect(tester.takeException(), isNull);
      state.dispose();
    },
  );

  test(
    'OwnerCompanionApi and CompanionClient e2e against live Session ASGI',
    () async {
      final liveUrl = Platform.environment['KINAIYA_LIVE_SESSION_URL'];
      final liveToken =
          Platform.environment['KINAIYA_LIVE_OWNER_TOKEN'] ?? ('o' * 32);
      if (liveUrl == null || liveUrl.isEmpty) {
        return;
      }
      final previousOverrides = HttpOverrides.current;
      HttpOverrides.global = null;
      addTearDown(() => HttpOverrides.global = previousOverrides);
      final httpClient = http.Client();
      addTearDown(httpClient.close);

      final owner = KinaiyaClient(
        sessionBase: liveUrl,
        converseBase: liveUrl,
        agentBase: liveUrl,
        decisionsBase: liveUrl,
        ownerToken: liveToken,
        httpClient: httpClient,
      );

      final session = await owner.createSession('converse');
      final binding = OwnerCompanionBinding.fromSession(session);

      // 1. Pair -> redeem -> list/self.
      final pairing = await owner.companion.issuePairing(
        binding,
        label: 'Foreground phone',
      );
      final phone = CompanionClient(liveUrl);
      addTearDown(phone.close);
      final phoneDevice = await phone.pair(
        pairing.pairingCode,
        'Pixel companion',
      );

      final listedDevices = await owner.companion.listDevices();
      expect(
        listedDevices.any(
          (d) =>
              d.deviceId == phoneDevice.deviceId &&
              d.status == OwnerCompanionDeviceStatus.active,
        ),
        isTrue,
      );

      // 2. Invite -> idempotent replay -> phone answer (waiting).
      final invited = await owner.companion.inviteCall(
        binding,
        deviceId: phoneDevice.deviceId,
        title: 'Check-in call',
        idempotencyKey: 'e2e-invite-1',
      );
      final replayedInvite = await owner.companion.inviteCall(
        binding,
        deviceId: phoneDevice.deviceId,
        title: 'Check-in call',
        idempotencyKey: 'e2e-invite-1',
      );
      expect(replayedInvite.callId, invited.callId);
      expect(invited.state, OwnerCompanionCallState.ringing);

      final phoneCalls = await phone.calls(phoneDevice);
      expect(phoneCalls.single.callId, invited.callId);

      final answered = await phone.act(
        phoneDevice,
        phoneCalls.single,
        'answer',
        'e2e-answer-1',
      );
      expect(answered.state, CompanionCallState.acceptedWaitingMedia);

      // 3. Literal note CAS update + stale CAS 409 check.
      const literalNote = '<script>noop()</script> {"role":"literal"}';
      final noted = await owner.companion.updateNote(
        binding,
        callId: invited.callId,
        deviceId: phoneDevice.deviceId,
        sequence: invited.sequence,
        expectedRevision: answered.revision,
        note: literalNote,
      );
      expect(noted.note, literalNote);
      expect(noted.revision, answered.revision + 1);

      await expectLater(
        owner.companion.updateNote(
          binding,
          callId: invited.callId,
          deviceId: phoneDevice.deviceId,
          sequence: invited.sequence,
          expectedRevision: answered.revision,
          note: 'stale cas',
        ),
        throwsA(
          isA<OwnerCompanionFailure>().having((e) => e.status, 'status', 409),
        ),
      );

      final phoneObserved = (await phone.calls(phoneDevice)).single;
      expect(phoneObserved.note, literalNote);

      // 4. End call -> reopen new invitation -> revoke device.
      final ended = await owner.companion.endCall(
        binding,
        callId: invited.callId,
        deviceId: phoneDevice.deviceId,
        sequence: invited.sequence,
        expectedRevision: noted.revision,
        idempotencyKey: 'e2e-end-1',
      );
      expect(ended.state, OwnerCompanionCallState.ended);
      expect(ended.endReason, 'owner_ended');

      final reopened = await owner.companion.inviteCall(
        binding,
        deviceId: phoneDevice.deviceId,
        title: 'Reopened call',
        idempotencyKey: 'e2e-invite-2',
      );
      expect(reopened.state, OwnerCompanionCallState.ringing);

      final revoked = await owner.companion.revokeDevice(phoneDevice.deviceId);
      expect(revoked.status, OwnerCompanionDeviceStatus.revoked);

      await expectLater(
        phone.calls(phoneDevice),
        throwsA(isA<CompanionFailure>().having((e) => e.status, 'status', 403)),
      );

      final afterRevokeCall = await owner.companion.getCall(
        reopened.callId,
        expectedBinding: binding,
        expectedDeviceId: phoneDevice.deviceId,
      );
      expect(afterRevokeCall.state, OwnerCompanionCallState.ended);
      expect(afterRevokeCall.endReason, 'device_revoked');

      // 5. Reset session -> stale device cannot invite, historical call readable, revoke still allowed.
      final pairing2 = await owner.companion.issuePairing(
        binding,
        label: 'Second phone',
      );
      final phone2 = CompanionClient(liveUrl);
      addTearDown(phone2.close);
      final phoneDevice2 = await phone2.pair(
        pairing2.pairingCode,
        'Tablet companion',
      );
      final resetRes = await httpClient.post(
        Uri.parse('$liveUrl/v1/sessions/${session.sessionId}/reset'),
        headers: <String, String>{
          'Authorization': 'Bearer $liveToken',
          'Content-Type': 'application/json',
        },
        body: '{}',
      );
      expect(resetRes.statusCode, 200);
      final reset = SessionInfo.fromJson(
        jsonDecode(resetRes.body) as Map<String, Object?>,
      );
      expect(reset.epoch, binding.epoch + 1);

      final devicesAfterReset = await owner.companion.listDevices();
      final staleDevice = devicesAfterReset.firstWhere(
        (d) => d.deviceId == phoneDevice2.deviceId,
      );
      expect(staleDevice.status, OwnerCompanionDeviceStatus.stale);

      final historical = await owner.companion.getCall(
        invited.callId,
        expectedBinding: binding,
        expectedDeviceId: phoneDevice.deviceId,
      );
      expect(historical.note, literalNote);

      final revokedStale = await owner.companion.revokeDevice(
        phoneDevice2.deviceId,
      );
      expect(revokedStale.status, OwnerCompanionDeviceStatus.revoked);
    },
  );
}
