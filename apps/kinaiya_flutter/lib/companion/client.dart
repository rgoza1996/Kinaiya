/// Bounded HTTPS device transport. Secrets stay in headers and memory.
library;

import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

import '../config/connection.dart';
import 'models.dart';

class CompanionFailure implements Exception {
  const CompanionFailure(this.message, {this.status, this.disconnect = false});
  final String message;
  final int? status;
  final bool disconnect;
  @override
  String toString() => message;
}

class CompanionClient {
  CompanionClient(String host, {http.Client Function()? clientFactory})
    : _factory = clientFactory ?? http.Client.new {
    if (host.length > 512) {
      throw const CompanionFailure('Host address is too long');
    }
    final problem = ConnectionProfile.validateServiceUrl(host);
    if (problem != null) throw CompanionFailure(problem);
    _base = Uri.parse(host.trim().replaceFirst(RegExp(r'/+$'), ''));
  }

  final http.Client Function() _factory;
  late final Uri _base;
  String? _token;
  final Set<http.Client> _requests = {};
  bool _closed = false;

  Future<Object?> _request(
    String method,
    String path, {
    Map<String, Object?>? body,
    bool authenticated = true,
  }) async {
    if (_closed) throw const CompanionFailure('Companion disconnected');
    final client = _factory();
    _requests.add(client);
    try {
      final request = http.Request(method, Uri.parse('$_base$path'))
        ..followRedirects = false
        ..headers['Content-Type'] = 'application/json';
      if (authenticated) {
        if (_token == null) {
          throw const CompanionFailure('Pair this companion first');
        }
        request.headers['Authorization'] = 'Bearer $_token';
      }
      if (body != null) request.body = jsonEncode(body);
      final response = await client
          .send(request)
          .timeout(const Duration(seconds: 5));
      if (response.statusCode != 200) {
        // Never reflect arbitrary response bodies, URLs or credential echoes.
        throw CompanionFailure(switch (response.statusCode) {
          401 || 403 => 'Access expired, revoked or unavailable. Pair again.',
          409 => 'The invitation changed. Refreshing its current state.',
          422 => 'The host rejected this request.',
          _ => 'The host could not complete this request.',
        }, status: response.statusCode);
      }
      final bytes = <int>[];
      final iterator = StreamIterator(response.stream);
      final deadline = DateTime.now().add(const Duration(seconds: 5));
      try {
        while (await iterator.moveNext().timeout(
          deadline.difference(DateTime.now()),
        )) {
          if (iterator.current.length > 262144 - bytes.length) {
            throw const CompanionFailure('Host response exceeded its limit');
          }
          bytes.addAll(iterator.current);
        }
      } finally {
        // Closing the transport must not wait on an adapter's onCancel future.
        client.close();
        try {
          await iterator.cancel().timeout(const Duration(milliseconds: 300));
        } catch (_) {
          _closed = true;
          _token = null;
          throw const CompanionFailure(
            'Connection cleanup is unconfirmed. Pair again before continuing.',
            disconnect: true,
          );
        }
      }
      return jsonDecode(utf8.decode(bytes));
    } on CompanionFailure {
      rethrow;
    } on TimeoutException {
      throw const CompanionFailure(
        'The host timed out. The action may have arrived.',
      );
    } on FormatException {
      throw const CompanionFailure(
        'The host returned an unsupported response.',
      );
    } catch (_) {
      throw const CompanionFailure(
        'Host unavailable. Check the connection and retry.',
      );
    } finally {
      _requests.remove(client);
      client.close();
    }
  }

  Future<CompanionDevice> pair(String code, String deviceName) async {
    if (!RegExp(r'^[A-Za-z0-9_-]{43,128}$').hasMatch(code) ||
        deviceName.isEmpty ||
        deviceName.runes.length > 120 ||
        utf8.encode(deviceName).length > 480 ||
        utf8.decode(utf8.encode(deviceName)) != deviceName ||
        deviceName.runes.any((rune) => rune < 32)) {
      throw const CompanionFailure(
        'Enter a complete pairing code and a device name of up to 120 characters.',
      );
    }
    final raw = object(
      await _request(
        'POST',
        '/v1/companion/pairings/redeem',
        authenticated: false,
        body: {'code': code, 'device_name': deviceName},
      ),
    );
    final token = text(raw, 'device_token');
    if (!RegExp(r'^[A-Za-z0-9_-]{43,128}$').hasMatch(token)) {
      throw const CompanionFailure('The host returned an invalid grant.');
    }
    final device = CompanionDevice.fromJson(raw['device']);
    if (_closed) throw const CompanionFailure('Companion disconnected');
    _token = token;
    return device;
  }

  Future<List<CompanionCall>> calls(CompanionDevice device) async {
    final calls = <CompanionCall>[];
    var after = 0;
    for (var page = 0; page < 5; page++) {
      final raw = object(
        await _request('GET', '/v1/companion/self/calls?after=$after'),
      );
      final rows = raw['calls'];
      if (rows is! List || rows.length > 20) {
        throw const CompanionFailure('The host returned an invalid call page.');
      }
      for (final row in rows) {
        final call = CompanionCall.fromJson(row);
        if (!call.belongsTo(device) ||
            call.sequence <= after ||
            calls.any((old) => old.callId == call.callId)) {
          throw const CompanionFailure(
            'The host returned an invalid device scope or cursor.',
          );
        }
        calls.add(call);
        after = call.sequence;
      }
      if (raw['next_after'] == null) return calls;
      if (rows.isEmpty || integer(raw, 'next_after') != after) {
        throw const CompanionFailure(
          'The host returned an invalid call cursor.',
        );
      }
    }
    throw const CompanionFailure('The host exceeded the retained call limit.');
  }

  Future<CompanionCall> act(
    CompanionDevice device,
    CompanionCall call,
    String action,
    String key,
  ) async {
    final expectedState = switch (action) {
      'answer' => CompanionCallState.acceptedWaitingMedia,
      'decline' => CompanionCallState.declined,
      'end' => CompanionCallState.ended,
      _ => throw const CompanionFailure('Unknown call action'),
    };
    if (!call.belongsTo(device)) {
      throw const CompanionFailure('Call belongs to another device scope');
    }
    final result = CompanionCall.fromJson(
      await _request(
        'POST',
        '/v1/companion/self/calls/${Uri.encodeComponent(call.callId)}/actions',
        body: {
          'action': action,
          'expected_revision': call.revision,
          'idempotency_key': key,
        },
      ),
    );
    if (!result.belongsTo(device) ||
        result.callId != call.callId ||
        result.sequence != call.sequence ||
        result.revision != call.revision + 1 ||
        result.state != expectedState) {
      throw const CompanionFailure(
        'The host returned an invalid action receipt.',
      );
    }
    return result;
  }

  void close() {
    _closed = true;
    _token = null;
    for (final request in _requests.toList()) {
      request.close();
    }
    _requests.clear();
  }
}
