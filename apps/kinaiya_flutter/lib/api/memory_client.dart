/// Per-request owned, bounded owner memory HTTP. No inference or automatic retry.
library;

import 'dart:async';
import 'dart:convert';
import 'package:http/http.dart' as http;
import '../config/connection.dart';
import '../config/service_root.dart';
import 'memory_models.dart';

class MemoryFailure implements Exception {
  const MemoryFailure(this.message, {this.status, this.unconfirmed = false});
  final String message;
  final int? status;
  final bool unconfirmed;
  @override
  String toString() => message;
}

/// The inspected native source only. Unrelated journal metadata is bounded and
/// parsed but is never retained as authority or copied into the note request.
class MemorySourceInspection {
  MemorySourceInspection._(
    this.binding,
    this.eventId,
    this.revision,
    this.sequence,
    this.text,
  );

  factory MemorySourceInspection.fromJson(Object? value) {
    if (value is! Map<String, Object?> || value['schema_version'] != '1.0') {
      throw const FormatException('Invalid memory source');
    }
    final payload = value['payload'];
    final sequence = value['sequence'];
    if (payload is! Map<String, Object?> ||
        value['kind'] != 'conversation.user' ||
        value['origin'] != 'authenticated-user' ||
        payload['final'] != true ||
        sequence is! int ||
        sequence < 1 ||
        sequence > memoryMaxSafeInteger ||
        payload['text'] is! String) {
      throw const FormatException('Invalid memory source');
    }
    final revision = payload.containsKey('revision') ? payload['revision'] : 1;
    if (revision is! int || revision < 1 || revision > memoryMaxSafeInteger) {
      throw const FormatException('Invalid memory source');
    }
    return MemorySourceInspection._(
      MemoryBinding.fromJson({
        'schema_version': value['schema_version'],
        'session_id': value['session_id'],
        'thread_id': value['thread_id'],
        'epoch': value['epoch'],
      }),
      memoryIdentifier(value['event_id']),
      revision,
      sequence,
      payload['text'] as String,
    );
  }

  final MemoryBinding binding;
  final String eventId;
  final int revision, sequence;
  final String text;
}

class MemoryApi {
  MemoryApi({
    required this.sessionBase,
    required this.converseBase,
    required String ownerToken,
    http.Client Function()? clientFactory,
    this.requestTimeout = const Duration(seconds: 5),
    this.cleanupTimeout = const Duration(milliseconds: 300),
  }) : _token = ownerToken,
       _factory = clientFactory ?? http.Client.new;
  final String sessionBase;
  final String converseBase;
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

  Future<MemoryNamespaceState> state(MemoryBinding binding) => _request(
    sessionBase,
    '/v1/converse-memory/state',
    binding.toJson(),
    binding,
    MemoryNamespaceState.fromJson,
    (v) => v.binding,
  );
  Future<MemoryNamespaceState> configure(
    MemoryBinding binding, {
    required int expectedRevision,
    required bool enabled,
  }) {
    _safe(expectedRevision);
    return _request(
      sessionBase,
      '/v1/converse-memory/configure',
      {
        ...binding.toJson(),
        'expected_revision': expectedRevision,
        'enabled': enabled,
      },
      binding,
      MemoryNamespaceState.fromJson,
      (v) => v.binding,
      write: true,
      valid: (v) => v.enabled == enabled && v.revision >= expectedRevision,
    );
  }

  Future<MemoryCatalog> catalog(MemoryBinding binding) => _request(
    sessionBase,
    '/v1/converse-memory/catalog',
    binding.toJson(),
    binding,
    MemoryCatalog.fromJson,
    (v) => v.binding,
  );
  Future<MemoryInspection> inspect(MemoryBinding binding, String nodeId) {
    _id(nodeId);
    return _request(
      sessionBase,
      '/v1/converse-memory/inspect',
      {...binding.toJson(), 'node_id': nodeId},
      binding,
      MemoryInspection.fromJson,
      (v) => v.binding,
      valid: (v) => v.node.nodeId == nodeId,
    );
  }

  /// Owner read of one resolved, finalized native user event. Capture still
  /// validates the complete correction chain and freshness under its own CAS;
  /// this response is an inspected revision, not permission to bypass that.
  Future<MemorySourceInspection> inspectSource(
    MemoryBinding binding,
    String eventId,
  ) {
    _id(eventId);
    return _request(
      sessionBase,
      '/v1/transcript/entries/${Uri.encodeComponent(eventId)}',
      const {},
      binding,
      MemorySourceInspection.fromJson,
      (value) => value.binding,
      valid: (value) => value.eventId == eventId,
      journalSource: true,
    );
  }

  Future<MemoryUseState> useState(MemoryBinding binding) => _request(
    converseBase,
    '/v1/memory/state',
    binding.toJson(),
    binding,
    MemoryUseState.fromJson,
    (v) => v.binding,
  );

  Future<MemoryInspection> capture(
    MemoryBinding binding, {
    required int expectedRevision,
    required String idempotencyKey,
    required String kind,
    required String sourceEventId,
    required int sourceRevision,
    required String body,
    required MemoryFact? fact,
    List<String> prerequisiteIds = const [],
  }) {
    final request = _contentRequest({
      ...binding.toJson(),
      'expected_revision': expectedRevision,
      'idempotency_key': idempotencyKey,
      'kind': kind,
      'source_event_id': sourceEventId,
      'source_revision': sourceRevision,
      'body': body,
      'fact': fact?.toJson(),
      'prerequisite_ids': prerequisiteIds,
    }, 'capture');
    final parents = (request['prerequisite_ids'] as List<String>).toSet();
    return _request(
      sessionBase,
      '/v1/converse-memory/capture',
      request,
      binding,
      MemoryInspection.fromJson,
      (v) => v.binding,
      write: true,
      valid: (v) =>
          v.state.revision == expectedRevision + 1 &&
          v.node.kind == kind &&
          v.node.sourceEventId == sourceEventId &&
          v.node.sourceRevision == sourceRevision &&
          v.node.prerequisiteIds.length == parents.length &&
          v.node.prerequisiteIds.every(parents.contains) &&
          (v.node.state == 'forgotten' ||
              v.node.body == body &&
                  (fact == null
                      ? v.node.fact == null
                      : fact.matches(v.node.fact))),
    );
  }

  Future<MemoryForgetResult> forget(
    MemoryBinding binding, {
    required int expectedRevision,
    required String idempotencyKey,
    required String nodeId,
  }) {
    final request = _contentRequest({
      ...binding.toJson(),
      'expected_revision': expectedRevision,
      'idempotency_key': idempotencyKey,
      'node_id': nodeId,
    }, 'forget');
    return _request(
      sessionBase,
      '/v1/converse-memory/forget',
      request,
      binding,
      MemoryForgetResult.fromJson,
      (v) => v.binding,
      write: true,
      valid: (v) => (v.forgottenIds.isEmpty
          ? v.state.revision == expectedRevision
          : v.state.revision == expectedRevision + 1 &&
                v.forgottenIds.contains(nodeId)),
    );
  }

  Future<MemoryOperationLookup> lookupOperation(
    MemoryBinding binding, {
    required String operation,
    required Map<String, Object?> request,
  }) {
    final original = _contentRequest(request, operation);
    if (original['session_id'] != binding.sessionId ||
        original['thread_id'] != binding.threadId ||
        original['epoch'] != binding.epoch) {
      throw const MemoryFailure('Invalid memory operation binding');
    }
    return _request(
      sessionBase,
      '/v1/converse-memory/operations/lookup',
      {'schema_version': '1.0', 'operation': operation, 'request': original},
      binding,
      MemoryOperationLookup.fromJson,
      (v) => v.binding,
      valid: (v) =>
          v.operation == operation &&
          v.idempotencyKey == original['idempotency_key'] &&
          (v.receipt == null ||
              v.receipt!.revision ==
                      (original['expected_revision'] as int) + 1 &&
                  (operation == 'capture' ||
                      v.receipt!.nodeId == original['node_id'])),
    );
  }

  static Map<String, Object?> _contentRequest(
    Map<String, Object?> value,
    String operation,
  ) {
    try {
      return memoryContentRequest(value, operation);
    } on FormatException {
      throw const MemoryFailure('Invalid memory content request');
    }
  }

  Future<MemoryUseState> configureUse(
    MemoryBinding binding, {
    required String expectedRuntimeId,
    required int expectedGeneration,
    required bool enabled,
    List<String> selectorIds = const [],
  }) {
    _id(expectedRuntimeId);
    _safe(expectedGeneration);
    final selected = List<String>.of(selectorIds);
    if (enabled
        ? selected.isEmpty || selected.length > 8
        : selected.isNotEmpty) {
      throw const MemoryFailure('Invalid memory selection');
    }
    for (final id in selected) {
      _id(id);
    }
    if (selected.toSet().length != selected.length) {
      throw const MemoryFailure('Invalid memory selection');
    }
    return _request(
      converseBase,
      '/v1/memory/use',
      {
        ...binding.toJson(),
        'expected_runtime_id': expectedRuntimeId,
        'expected_generation': expectedGeneration,
        'enabled': enabled,
        if (enabled) 'implementation_id': 'korina-dag-v1',
        if (enabled) 'selector_ids': selected,
      },
      binding,
      MemoryUseState.fromJson,
      (v) => v.binding,
      write: true,
      valid: (v) =>
          v.runtimeId == expectedRuntimeId &&
          v.enabled == enabled &&
          (enabled
              ? v.useGeneration > expectedGeneration
              : v.useGeneration >= expectedGeneration) &&
          (!enabled ||
              (v.implementationId == 'korina-dag-v1' &&
                  v.selectorIds.length == selected.length &&
                  List.generate(
                    selected.length,
                    (i) => v.selectorIds[i] == selected[i],
                  ).every((v) => v))),
    );
  }

  static void _safe(int n) {
    if (n < 0 || n > memoryMaxSafeInteger) {
      throw const MemoryFailure('Invalid memory request');
    }
  }

  static void _id(String id) {
    try {
      memoryIdentifier(id);
    } on FormatException {
      throw const MemoryFailure('Invalid memory request');
    }
  }

  Future<T> _request<T>(
    String base,
    String path,
    Map<String, Object?> body,
    MemoryBinding binding,
    T Function(Object?) decode,
    MemoryBinding Function(T) scope, {
    bool write = false,
    bool Function(T)? valid,
    bool journalSource = false,
  }) async {
    if (_closed || _owners.length >= 8) {
      throw const MemoryFailure('Memory transport is unavailable');
    }
    final trimmed = base.trim();
    if (trimmed.length > 4096 ||
        ConnectionProfile.validateServiceUrl(trimmed) != null ||
        _token.isEmpty ||
        _token.length > 4096 ||
        _token.codeUnits.any((c) => c <= 32 || c >= 127) ||
        requestTimeout <= Duration.zero ||
        cleanupTimeout <= Duration.zero) {
      throw const MemoryFailure('Invalid memory connection');
    }
    if (journalSource && (write || body.isNotEmpty)) {
      throw const MemoryFailure('Invalid memory source request');
    }
    final bytes = journalSource ? const <int>[] : utf8.encode(jsonEncode(body));
    if (bytes.length > 16 * 1024) {
      throw const MemoryFailure('Memory request exceeds its bound');
    }
    final uri = Uri.parse('${normalizeServiceRoot(base)}$path').replace(
      queryParameters: journalSource ? const {'resolve': 'true'} : null,
    );
    final request = http.Request(journalSource ? 'GET' : 'POST', uri)
      ..followRedirects = false
      ..headers.addAll({
        'Authorization': 'Bearer $_token',
        if (!journalSource) 'Content-Type': 'application/json',
        'Accept': 'application/json',
        'Accept-Encoding': 'identity',
      })
      ..bodyBytes = bytes;
    _OwnedRequest? owner;
    T? result;
    MemoryFailure? failure;
    try {
      final owned = _OwnedRequest(_factory(), cleanupTimeout);
      owner = owned;
      _owners.add(owned);
      owned.onSettled = () {
        if (owned.settled) _owners.remove(owned);
      };
      final raw = await owner.read(request).timeout(requestTimeout);
      final decoded = decode(
        _JsonReader(raw, journalSource: journalSource).read(),
      );
      result = decoded;
      if (scope(decoded) != binding || (valid != null && !valid(decoded))) {
        throw const FormatException('Invalid memory response');
      }
    } on _StatusFailure catch (e) {
      failure = MemoryFailure(
        'Memory request was refused',
        status: e.status,
        unconfirmed: write && (e.status < 400 || e.status >= 500),
      );
    } on TimeoutException {
      failure = MemoryFailure(
        'Memory request timed out',
        unconfirmed: write && (owner?.dispatched ?? false),
      );
    } on FormatException {
      failure = MemoryFailure(
        'Invalid memory response',
        unconfirmed: write && (owner?.dispatched ?? false),
      );
    } catch (_) {
      failure = MemoryFailure(
        'Memory request failed',
        unconfirmed: write && (owner?.dispatched ?? false),
      );
    } finally {
      if (owner != null) {
        if (!await owner.close()) {
          failure = MemoryFailure(
            'Memory transport cleanup is unconfirmed',
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
      dispatched = true; // Transport admission, not proof of native effects.
      final response = await client.send(request);
      _reader = StreamIterator(response.stream);
      // StreamIterator is lazy. Begin its sole subscription BEFORE any early
      // refusal so cancellation owns the actual response, including late sends.
      final first = _reader!.moveNext();
      unawaited(
        first.then<void>((_) {}, onError: (Object _, StackTrace __) {}),
      );
      if (_closed) {
        await _awaitCancellation();
        throw const MemoryFailure('Memory request ended');
      }
      if (response.statusCode != 200) throw _StatusFailure(response.statusCode);
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
        throw const FormatException('Invalid memory response');
      }
      final bytes = <int>[];
      var more = await first;
      while (more) {
        if (_closed) throw const MemoryFailure('Memory request ended');
        final chunk = _reader!.current;
        if (bytes.length + chunk.length > 256 * 1024) {
          throw const FormatException('Invalid memory response');
        }
        bytes.addAll(chunk);
        more = await _reader!.moveNext();
      }
      if (response.contentLength != null &&
          response.contentLength != bytes.length) {
        throw const FormatException('Invalid memory response');
      }
      return bytes;
    } finally {
      _finished = true;
      onSettled?.call();
    }
  }

  Future<void> _cancelReader() {
    final reader = _reader;
    // No empty memoized future: a timed-out send can still deliver a response.
    if (reader == null) return Future<void>.value();
    if (_cancel != null) return _cancel!;
    final cancel = _cancel = reader.cancel();
    // A logical deadline is not settlement. Retain the real cancellation future
    // and release capacity only when it actually succeeds AND client/read close.
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
    // A successful await is itself actual settlement proof. In particular,
    // SDK completed futures may schedule the observer in a different Zone.
    // Do not credit this if no response reader existed for this cancellation.
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

/// Bounded memory JSON. Native memory responses require integer spelling.
/// Journal source metadata may contain finite fractions, while source identity
/// numbers still require integer spelling before any web numeric conversion.
class _JsonReader {
  _JsonReader(List<int> bytes, {this.journalSource = false})
    : text = utf8.decode(bytes, allowMalformed: false) {
    if (bytes.length > 256 * 1024) _bad();
  }
  final String text;
  final bool journalSource;
  int at = 0;
  int values = 0;
  Never _bad() => throw const FormatException('Invalid memory response');
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

  Object? _value(
    int depth, {
    bool payloadRoot = false,
    bool integerOnly = false,
  }) {
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
        if (key.length > (journalSource ? 4096 : 128) ||
            map.containsKey(key) ||
            map.length >= (journalSource ? 8192 : 32)) {
          _bad();
        }
        _space();
        if (at >= text.length || text[at++] != ':') _bad();
        map[key] = _value(
          depth + 1,
          payloadRoot: journalSource && depth == 0 && key == 'payload',
          integerOnly:
              journalSource &&
              ((depth == 0 && const {'epoch', 'sequence'}.contains(key)) ||
                  (payloadRoot && key == 'revision')),
        );
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
        if (list.length >= (journalSource ? 8192 : 256)) _bad();
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
    var fraction = false;
    if (at < text.length && text[at] == '.') {
      fraction = true;
      at++;
      final first = at;
      while (_digitAt()) {
        at++;
      }
      if (at == first) _bad();
    }
    if (at < text.length && 'eE'.contains(text[at])) {
      fraction = true;
      at++;
      if (at < text.length && '+-'.contains(text[at])) at++;
      final first = at;
      while (_digitAt()) {
        at++;
      }
      if (at == first) _bad();
    }
    if (fraction) {
      if (!journalSource || integerOnly || at - start > 128) _bad();
      final number = double.tryParse(text.substring(start, at));
      if (number == null || !number.isFinite) _bad();
      return number;
    }
    if (magnitude.length > 16 ||
        (magnitude.length == 16 &&
            magnitude.compareTo('9007199254740991') > 0)) {
      if (!journalSource || integerOnly || at - start > 128) _bad();
      final metadataNumber = double.tryParse(text.substring(start, at));
      if (metadataNumber == null || !metadataNumber.isFinite) _bad();
      // Only unrelated source metadata reaches this branch. The source DTO
      // drops it; identity, sequence, epoch and revision never accept rounding.
      return metadataNumber;
    }
    return int.parse(text.substring(start, at));
  }

  bool _digitAt() =>
      at < text.length &&
      text.codeUnitAt(at) >= 48 &&
      text.codeUnitAt(at) <= 57;

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
        at++; // jsonDecode validates escapes and required hex digits.
      }
    }
    _bad();
  }
}
