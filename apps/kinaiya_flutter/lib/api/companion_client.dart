/// Per-request owned, bounded owner companion HTTP (ADR0045, ADR0110).
///
/// Uses the in-memory owner Bearer header only. Exposes no redemption route,
/// no `/self` device routes, and never transfers the owner credential to a
/// companion device or request body.
library;

import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

import '../config/connection.dart';
import '../config/service_root.dart';
import 'companion_models.dart';

class OwnerCompanionFailure implements Exception {
  const OwnerCompanionFailure(
    this.message, {
    this.status,
    this.unconfirmed = false,
  });

  final String message;
  final int? status;
  final bool unconfirmed;

  @override
  String toString() => message;
}

class OwnerCompanionApi {
  OwnerCompanionApi({
    required this.sessionBase,
    required String ownerToken,
    http.Client Function()? clientFactory,
    this.requestTimeout = const Duration(seconds: 5),
    this.cleanupTimeout = const Duration(milliseconds: 300),
  }) : _token = ownerToken,
       _factory = clientFactory ?? http.Client.new;

  final String sessionBase;
  final String _token;
  final http.Client Function() _factory;
  final Duration requestTimeout;
  final Duration cleanupTimeout;
  final Set<_OwnedRequest> _owners = {};
  bool _closed = false;
  Future<void>? _closing;

  /// Abort only this API's transports. Every client is closed synchronously
  /// before the bounded asynchronous drains; uncertain owners remain retained.
  Future<void> close() {
    _closed = true;
    return _closing ??= Future.wait(
      List<_OwnedRequest>.of(_owners).map((owner) async {
        await owner.close();
        if (owner.settled) _owners.remove(owner);
      }),
    ).then((_) {});
  }

  Future<OwnerPairingIssued> issuePairing(
    OwnerCompanionBinding binding, {
    required String label,
  }) {
    final validLabel = _shortText(label);
    return _request(
      method: 'POST',
      path: '/v1/companion/pairings',
      body: <String, Object?>{...binding.toJson(), 'label': validLabel},
      write: true,
      decode: OwnerPairingIssued.fromJson,
      valid: (issued) =>
          issued.binding == binding &&
          issued.label == validLabel &&
          issued.expiresAt > 0,
    );
  }

  Future<List<OwnerCompanionDevice>> listDevices() => _request(
    method: 'GET',
    path: '/v1/companion/devices',
    decode: decodeOwnerCompanionDevicePage,
  );

  Future<OwnerCompanionDevice> revokeDevice(String deviceId) {
    final validId = _id(deviceId);
    return _request(
      method: 'POST',
      path: '/v1/companion/devices/${Uri.encodeComponent(validId)}/revoke',
      body: const <String, Object?>{'schema_version': '1.0'},
      write: true,
      decode: OwnerCompanionDevice.fromJson,
      valid: (device) =>
          device.deviceId == validId &&
          device.status == OwnerCompanionDeviceStatus.revoked &&
          device.revokedAt != null,
    );
  }

  Future<OwnerCompanionCall> inviteCall(
    OwnerCompanionBinding binding, {
    required String deviceId,
    required String title,
    required String idempotencyKey,
  }) {
    final validDevice = _id(deviceId);
    final validTitle = _shortText(title);
    final validKey = _key(idempotencyKey);
    return _request(
      method: 'POST',
      path: '/v1/companion/calls',
      body: <String, Object?>{
        'schema_version': '1.0',
        'device_id': validDevice,
        'title': validTitle,
        'idempotency_key': validKey,
      },
      write: true,
      decode: OwnerCompanionCall.fromJson,
      valid: (call) =>
          call.binding == binding &&
          call.deviceId == validDevice &&
          call.title == validTitle,
    );
  }

  /// Reads one page with exact binding and optional exact device validation.
  Future<OwnerCompanionCallPage> listCallsPage(
    OwnerCompanionBinding binding, {
    String? deviceId,
    int after = 0,
    int limit = ownerCompanionMaxCallPageRows,
  }) => _readCallsPage(binding, deviceId: deviceId, after: after, limit: limit);

  Future<OwnerCompanionCallPage> _readCallsPage(
    OwnerCompanionBinding binding, {
    String? deviceId,
    int after = 0,
    int limit = ownerCompanionMaxCallPageRows,
    bool includePriorEpochs = false,
  }) {
    _cursor(after);
    if (limit < 1 || limit > ownerCompanionMaxCallPageRows) {
      throw const OwnerCompanionFailure('Invalid companion call page limit.');
    }
    final validDevice = deviceId == null ? null : _id(deviceId);
    return _request(
      method: 'GET',
      path: '/v1/companion/calls',
      query: <String, String>{
        'session_id': binding.sessionId,
        if (validDevice != null) 'device_id': validDevice,
        'after': '$after',
        'limit': '$limit',
      },
      decode: OwnerCompanionCallPage.fromJson,
      valid: (page) => page.calls.every(
        (call) =>
            (call.binding == binding ||
                (includePriorEpochs &&
                    call.binding.sessionId == binding.sessionId &&
                    call.binding.threadId == binding.threadId &&
                    call.binding.epoch < binding.epoch)) &&
            call.sequence > after &&
            (validDevice == null || call.deviceId == validDevice),
      ),
    );
  }

  /// Validates the complete retained inventory before returning this binding.
  /// Prior epochs remain countable history; they are never action candidates.
  Future<List<OwnerCompanionCall>> listCalls(
    OwnerCompanionBinding binding, {
    String? deviceId,
    int? maxPages,
  }) async {
    final validDevice = deviceId == null ? null : _id(deviceId);
    final maxAllowedPages = validDevice == null ? 160 : 5;
    final effectiveMaxPages = maxPages ?? maxAllowedPages;
    if (effectiveMaxPages < 1 || effectiveMaxPages > maxAllowedPages) {
      throw const OwnerCompanionFailure('Invalid companion call page bound.');
    }
    final calls = <OwnerCompanionCall>[];
    final seen = <String>{};
    final perDeviceCounts = <String, int>{};
    var after = 0;
    for (var pageIndex = 0; pageIndex < effectiveMaxPages; pageIndex++) {
      final page = await _readCallsPage(
        binding,
        deviceId: validDevice,
        after: after,
        includePriorEpochs: true,
      );
      for (final call in page.calls) {
        if (call.sequence <= after || !seen.add(call.callId)) {
          throw const OwnerCompanionFailure(
            'The host returned an invalid companion call cursor.',
          );
        }
        final count = (perDeviceCounts[call.deviceId] ?? 0) + 1;
        perDeviceCounts[call.deviceId] = count;
        if (count > ownerCompanionMaxCallsPerDevice ||
            perDeviceCounts.length > ownerCompanionMaxDevices) {
          throw const OwnerCompanionFailure(
            'The host exceeded the retained companion call limit.',
          );
        }
        // Reset retains prior epochs in this same session/thread. Validate and
        // count that inventory before projecting the current binding for UI use.
        if (call.binding == binding) calls.add(call);
        after = call.sequence;
      }
      if (page.nextAfter == null) {
        return List<OwnerCompanionCall>.unmodifiable(calls);
      }
      if (page.calls.isEmpty || page.nextAfter != after) {
        throw const OwnerCompanionFailure(
          'The host returned an invalid companion call cursor.',
        );
      }
    }
    throw const OwnerCompanionFailure(
      'The host exceeded the retained companion call limit.',
    );
  }

  Future<OwnerCompanionCall> getCall(
    String callId, {
    OwnerCompanionBinding? expectedBinding,
    String? expectedDeviceId,
  }) {
    final validCall = _id(callId);
    final validDevice = expectedDeviceId == null ? null : _id(expectedDeviceId);
    return _request(
      method: 'GET',
      path: '/v1/companion/calls/${Uri.encodeComponent(validCall)}',
      decode: OwnerCompanionCall.fromJson,
      valid: (call) =>
          call.callId == validCall &&
          (expectedBinding == null || call.binding == expectedBinding) &&
          (validDevice == null || call.deviceId == validDevice),
    );
  }

  Future<OwnerCompanionCall> endCall(
    OwnerCompanionBinding binding, {
    required String callId,
    required String deviceId,
    required int sequence,
    required int expectedRevision,
    required String idempotencyKey,
  }) {
    final validCall = _id(callId);
    final validDevice = _id(deviceId);
    _counter(sequence);
    _counter(expectedRevision);
    final validKey = _key(idempotencyKey);
    return _request(
      method: 'POST',
      path: '/v1/companion/calls/${Uri.encodeComponent(validCall)}/end',
      body: <String, Object?>{
        'schema_version': '1.0',
        'expected_revision': expectedRevision,
        'idempotency_key': validKey,
      },
      write: true,
      decode: OwnerCompanionCall.fromJson,
      valid: (call) =>
          call.callId == validCall &&
          call.binding == binding &&
          call.deviceId == validDevice &&
          call.sequence == sequence &&
          call.revision == expectedRevision + 1 &&
          call.state == OwnerCompanionCallState.ended &&
          call.endReason == 'owner_ended',
    );
  }

  Future<OwnerCompanionCall> updateNote(
    OwnerCompanionBinding binding, {
    required String callId,
    required String deviceId,
    required int sequence,
    required int expectedRevision,
    required String note,
  }) {
    final validCall = _id(callId);
    final validDevice = _id(deviceId);
    _counter(sequence);
    _counter(expectedRevision);
    final validNote = _note(note);
    return _request(
      method: 'POST',
      path: '/v1/companion/calls/${Uri.encodeComponent(validCall)}/note',
      body: <String, Object?>{
        'schema_version': '1.0',
        'expected_revision': expectedRevision,
        'note': validNote,
      },
      write: true,
      decode: OwnerCompanionCall.fromJson,
      valid: (call) =>
          call.callId == validCall &&
          call.binding == binding &&
          call.deviceId == validDevice &&
          call.sequence == sequence &&
          call.revision == expectedRevision + 1 &&
          call.note == validNote,
    );
  }

  static String _id(String value) {
    try {
      return ownerCompanionIdentifier(value);
    } on FormatException {
      throw const OwnerCompanionFailure('Invalid companion identifier.');
    }
  }

  static String _key(String value) {
    try {
      return ownerCompanionIdempotencyKey(value);
    } on FormatException {
      throw const OwnerCompanionFailure('Invalid companion idempotency key.');
    }
  }

  static String _shortText(String value) {
    try {
      return ownerCompanionShortText(value);
    } on FormatException {
      throw const OwnerCompanionFailure(
        'Enter 1 to 120 characters without control characters.',
      );
    }
  }

  static String _note(String value) {
    try {
      return ownerCompanionNoteText(value);
    } on FormatException {
      throw const OwnerCompanionFailure(
        'Notes must be valid UTF-8 text of at most 4096 bytes without control characters.',
      );
    }
  }

  static void _counter(int value) {
    try {
      ownerCompanionInteger(value, min: 1);
    } on FormatException {
      throw const OwnerCompanionFailure(
        'Invalid companion revision or sequence.',
      );
    }
  }

  static void _cursor(int value) {
    try {
      ownerCompanionInteger(value, min: 0);
    } on FormatException {
      throw const OwnerCompanionFailure('Invalid companion page cursor.');
    }
  }

  static String _statusMessage(int status) => switch (status) {
    401 || 403 => 'Owner authentication or companion scope was refused.',
    404 => 'The requested companion device or invitation was not found.',
    409 => 'Companion state changed. Refresh to inspect the current state.',
    413 => 'Companion request exceeded the 16 KiB limit.',
    422 => 'The host rejected the companion request parameters.',
    _ => 'The host could not complete the companion request.',
  };

  Future<T> _request<T>({
    required String method,
    required String path,
    Map<String, String>? query,
    Map<String, Object?>? body,
    required T Function(Object?) decode,
    bool write = false,
    bool Function(T)? valid,
  }) async {
    if (_closed || _owners.length >= 8) {
      throw const OwnerCompanionFailure('Companion transport is unavailable.');
    }
    final trimmed = sessionBase.trim();
    if (trimmed.length > 4096 ||
        ConnectionProfile.validateServiceUrl(trimmed) != null ||
        _token.isEmpty ||
        _token.length > 4096 ||
        _token.codeUnits.any((c) => c <= 32 || c >= 127) ||
        requestTimeout <= Duration.zero ||
        cleanupTimeout <= Duration.zero) {
      throw const OwnerCompanionFailure('Invalid companion connection.');
    }
    if ((method == 'GET') != (body == null)) {
      throw const OwnerCompanionFailure('Invalid companion request method.');
    }
    final bytes = body == null ? const <int>[] : utf8.encode(jsonEncode(body));
    if (bytes.length > 16 * 1024) {
      throw const OwnerCompanionFailure(
        'Companion request exceeded the 16 KiB limit.',
      );
    }
    final uri = Uri.parse('${normalizeServiceRoot(sessionBase)}$path').replace(
      queryParameters: query != null && query.isNotEmpty ? query : null,
    );
    final request = http.Request(method, uri)
      ..followRedirects = false
      ..headers.addAll(<String, String>{
        'Authorization': 'Bearer $_token',
        if (body != null) 'Content-Type': 'application/json',
        'Accept': 'application/json',
        'Accept-Encoding': 'identity',
      })
      ..bodyBytes = bytes;
    _OwnedRequest? owner;
    T? result;
    OwnerCompanionFailure? failure;
    try {
      final owned = _OwnedRequest(_factory(), cleanupTimeout);
      owner = owned;
      _owners.add(owned);
      owned.onSettled = () {
        if (owned.settled) _owners.remove(owned);
      };
      final raw = await owned.read(request).timeout(requestTimeout);
      final decoded = decode(_CompanionJsonReader(raw).read());
      if (valid != null && !valid(decoded)) {
        throw const FormatException('Invalid companion response');
      }
      result = decoded;
    } on _StatusFailure catch (e) {
      failure = OwnerCompanionFailure(
        _statusMessage(e.status),
        status: e.status,
        unconfirmed: write && (e.status < 400 || e.status >= 500),
      );
    } on TimeoutException {
      failure = OwnerCompanionFailure(
        'The companion request timed out.',
        unconfirmed: write && (owner?.dispatched ?? false),
      );
    } on FormatException {
      failure = OwnerCompanionFailure(
        'The host returned an invalid companion response.',
        unconfirmed: write && (owner?.dispatched ?? false),
      );
    } catch (_) {
      failure = OwnerCompanionFailure(
        'The companion request failed.',
        unconfirmed: write && (owner?.dispatched ?? false),
      );
    } finally {
      if (owner != null) {
        if (!await owner.close()) {
          failure = OwnerCompanionFailure(
            'Companion transport cleanup is unconfirmed.',
            unconfirmed: write && owner.dispatched,
          );
        }
        if (owner.settled) _owners.remove(owner);
      }
    }
    if (failure != null) throw failure;
    return result as T;
  }
}

class _StatusFailure implements Exception {
  _StatusFailure(this.status);
  final int status;
}

class _OwnedRequest {
  _OwnedRequest(this.client, this.cleanupTimeout);

  final http.Client client;
  final Duration cleanupTimeout;
  StreamIterator<List<int>>? _reader;
  Future<void>? _cancel;
  Future<bool>? _closing;
  void Function()? onSettled;
  bool _closed = false;
  bool _finished = false;
  bool _clientClosed = false;
  bool _cancelSettled = false;
  bool dispatched = false;

  bool get settled =>
      _finished && _clientClosed && (_reader == null || _cancelSettled);

  Future<List<int>> read(http.Request request) async {
    try {
      dispatched = true;
      final response = await client.send(request);
      _reader = StreamIterator(response.stream);
      final first = _reader!.moveNext();
      unawaited(
        first.then<void>((_) {}, onError: (Object _, StackTrace __) {}),
      );
      if (_closed) {
        await _awaitCancellation();
        throw const OwnerCompanionFailure('Companion request ended.');
      }
      if (response.statusCode != 200) {
        throw _StatusFailure(response.statusCode);
      }
      if (response.headers['content-type']
                  ?.split(';')
                  .first
                  .trim()
                  .toLowerCase() !=
              'application/json' ||
          !{'', 'identity'}.contains(
            (response.headers['content-encoding'] ?? '').trim().toLowerCase(),
          ) ||
          (response.contentLength != null &&
              response.contentLength! > 256 * 1024)) {
        throw const FormatException('Invalid companion response');
      }
      final bytes = <int>[];
      var more = await first;
      while (more) {
        if (_closed) {
          throw const OwnerCompanionFailure('Companion request ended.');
        }
        final chunk = _reader!.current;
        if (bytes.length + chunk.length > 256 * 1024) {
          throw const FormatException('Invalid companion response');
        }
        bytes.addAll(chunk);
        more = await _reader!.moveNext();
      }
      if (response.contentLength != null &&
          response.contentLength != bytes.length) {
        throw const FormatException('Invalid companion response');
      }
      return bytes;
    } finally {
      _finished = true;
      onSettled?.call();
    }
  }

  Future<void> _cancelReader() {
    final reader = _reader;
    if (reader == null) return Future<void>.value();
    if (_cancel != null) return _cancel!;
    final cancel = _cancel = reader.cancel();
    unawaited(
      cancel.then<void>((_) {
        _cancelSettled = true;
        onSettled?.call();
      }, onError: (Object _, StackTrace __) {}),
    );
    return cancel;
  }

  Future<void> _awaitCancellation() async {
    final reader = _reader;
    await _cancelReader().timeout(cleanupTimeout);
    if (reader != null) {
      _cancelSettled = true;
      onSettled?.call();
    }
  }

  Future<bool> close() => _closing ??= _close();

  Future<bool> _close() async {
    _closed = true;
    var ok = true;
    try {
      client.close();
      _clientClosed = true;
    } catch (_) {
      ok = false;
    }
    try {
      await _awaitCancellation();
    } catch (_) {
      ok = false;
    }
    onSettled?.call();
    return ok;
  }
}

/// Bounded companion JSON reader. Rejects duplicate keys, non-integer numbers,
/// leading zeros, unpaired UTF-16 surrogates, and payloads exceeding 256 KiB.
class _CompanionJsonReader {
  _CompanionJsonReader(List<int> bytes)
    : text = utf8.decode(bytes, allowMalformed: false) {
    if (bytes.length > 256 * 1024) _bad();
  }

  final String text;
  int at = 0;
  int values = 0;

  Never _bad() => throw const FormatException('Invalid companion response');

  void _space() {
    while (at < text.length && ' \n\r\t'.contains(text[at])) {
      at++;
    }
  }

  Object? read() {
    final result = _value(0);
    _space();
    if (at != text.length) _bad();
    return result;
  }

  Object? _value(int depth) {
    if (depth > 16 || ++values > 8192) _bad();
    _space();
    if (at >= text.length) _bad();
    final c = text[at];
    if (c == '"') return _string();
    if (c == '{') {
      at++;
      final map = <String, Object?>{};
      _space();
      if (at < text.length && text[at] == '}') {
        at++;
        return map;
      }
      while (true) {
        _space();
        if (at >= text.length || text[at] != '"') _bad();
        final key = _string();
        if (key.length > 128 || map.containsKey(key) || map.length >= 32) {
          _bad();
        }
        _space();
        if (at >= text.length || text[at++] != ':') _bad();
        map[key] = _value(depth + 1);
        _space();
        if (at >= text.length) _bad();
        final separator = text[at++];
        if (separator == '}') return map;
        if (separator != ',') _bad();
      }
    }
    if (c == '[') {
      at++;
      final list = <Object?>[];
      _space();
      if (at < text.length && text[at] == ']') {
        at++;
        return list;
      }
      while (true) {
        if (list.length >= 64) _bad();
        list.add(_value(depth + 1));
        _space();
        if (at >= text.length) _bad();
        final separator = text[at++];
        if (separator == ']') return list;
        if (separator != ',') _bad();
      }
    }
    for (final pair in [('true', true), ('false', false), ('null', null)]) {
      if (text.startsWith(pair.$1, at)) {
        at += pair.$1.length;
        return pair.$2;
      }
    }
    final start = at;
    if (text[at] == '-') at++;
    final digits = at;
    while (at < text.length &&
        text.codeUnitAt(at) >= 48 &&
        text.codeUnitAt(at) <= 57) {
      at++;
    }
    if (at == digits) _bad();
    final magnitude = text.substring(digits, at);
    if (magnitude.length > 1 && magnitude.startsWith('0')) _bad();
    if (at < text.length && '.eE'.contains(text[at])) _bad();
    if (magnitude.length > 16 ||
        (magnitude.length == 16 &&
            magnitude.compareTo('9007199254740991') > 0)) {
      _bad();
    }
    return int.parse(text.substring(start, at));
  }

  String _string() {
    final start = at++;
    while (at < text.length) {
      final c = text.codeUnitAt(at++);
      if (c == 34) {
        final value = jsonDecode(text.substring(start, at)) as String;
        for (var i = 0; i < value.length; i++) {
          final u = value.codeUnitAt(i);
          if (u >= 0xd800 && u <= 0xdbff) {
            if (++i >= value.length ||
                value.codeUnitAt(i) < 0xdc00 ||
                value.codeUnitAt(i) > 0xdfff) {
              _bad();
            }
          } else if (u >= 0xdc00 && u <= 0xdfff) {
            _bad();
          }
        }
        return value;
      }
      if (c < 32) _bad();
      if (c == 92) {
        if (at >= text.length) _bad();
        at++;
      }
    }
    _bad();
  }
}
