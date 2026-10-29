/// Per-request owned, bounded owner memory HTTP. No inference or automatic retry.
library;

import 'dart:async';
import 'dart:convert';
import 'package:http/http.dart' as http;
import '../config/connection.dart';
import 'memory_models.dart';

class MemoryFailure implements Exception {
  const MemoryFailure(this.message, {this.status, this.unconfirmed = false});
  final String message;
  final int? status;
  final bool unconfirmed;
  @override
  String toString() => message;
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

  Future<MemoryUseState> useState(MemoryBinding binding) => _request(
    converseBase,
    '/v1/memory/state',
    binding.toJson(),
    binding,
    MemoryUseState.fromJson,
    (v) => v.binding,
  );
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
      valid: (v) => v.runtimeId == expectedRuntimeId,
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
  }) async {
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
    final bytes = utf8.encode(jsonEncode(body));
    if (bytes.length > 16 * 1024) {
      throw const MemoryFailure('Memory request exceeds its bound');
    }
    final uri = Uri.parse('${trimmed.replaceFirst(RegExp(r'/+$'), '')}$path');
    final request = http.Request('POST', uri)
      ..followRedirects = false
      ..headers.addAll({
        'Authorization': 'Bearer $_token',
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'Accept-Encoding': 'identity',
      })
      ..bodyBytes = bytes;
    _OwnedRequest? owner;
    T? result;
    MemoryFailure? failure;
    try {
      owner = _OwnedRequest(_factory(), cleanupTimeout);
      final raw = await owner.read(request).timeout(requestTimeout);
      final decoded = decode(_JsonReader(raw).read());
      result = decoded;
      if (scope(decoded) != binding || (valid != null && !valid(decoded))) {
        throw const FormatException('Invalid memory response');
      }
    } on _StatusFailure catch (e) {
      failure = MemoryFailure(
        'Memory request was refused',
        status: e.status,
        unconfirmed:
            write && (e.status >= 500 || (e.status >= 300 && e.status < 400)),
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
      if (owner != null && !await owner.close()) {
        failure = MemoryFailure(
          'Memory transport cleanup is unconfirmed',
          unconfirmed: write && owner.dispatched,
        );
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
  bool _closed = false;
  bool dispatched = false;
  Future<List<int>> read(http.Request request) async {
    dispatched = true; // Transport admission, not proof of native effects.
    final response = await client.send(request);
    _reader = StreamIterator(response.stream);
    if (_closed) {
      await _cancelReader().timeout(cleanupTimeout);
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
    while (await _reader!.moveNext()) {
      if (_closed) throw const MemoryFailure('Memory request ended');
      final chunk = _reader!.current;
      if (bytes.length + chunk.length > 256 * 1024) {
        throw const FormatException('Invalid memory response');
      }
      bytes.addAll(chunk);
    }
    if (response.contentLength != null &&
        response.contentLength != bytes.length) {
      throw const FormatException('Invalid memory response');
    }
    return bytes;
  }

  Future<void> _cancelReader() =>
      _cancel ??= _reader?.cancel() ?? Future<void>.value();
  Future<bool> close() async {
    _closed = true;
    var ok = true;
    // Close real HTTP first even if a custom stream cancellation stalls.
    try {
      client.close();
    } catch (_) {
      ok = false;
    }
    try {
      await _cancelReader().timeout(cleanupTimeout);
    } catch (_) {
      ok = false;
    }
    return ok;
  }
}

/// Memory-only JSON: check integer spelling before parsing/rounding on web.
class _JsonReader {
  _JsonReader(List<int> bytes)
    : text = utf8.decode(bytes, allowMalformed: false) {
    if (bytes.length > 256 * 1024) _bad();
  }
  final String text;
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
        if (list.length >= 256) _bad();
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
    if ((magnitude.length > 1 && magnitude.startsWith('0')) ||
        magnitude.length > 16 ||
        (magnitude.length == 16 &&
            magnitude.compareTo('9007199254740991') > 0)) {
      _bad();
    }
    if (at < text.length && '.eE'.contains(text[at])) _bad();
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
        at++; // jsonDecode validates escapes and required hex digits.
      }
    }
    _bad();
  }
}
