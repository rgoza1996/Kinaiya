/// Bound model settings only. No generation/voice transport or automatic retry.
library;

import 'dart:async';
import 'dart:convert';
import 'package:http/http.dart' as http;
import '../config/connection.dart';
import '../config/service_root.dart';
import 'agent_model_settings.dart';
import 'provider_settings.dart' show providerJsonDecode;

class AgentModelFailure implements Exception {
  const AgentModelFailure(
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

/// Public result and genuine transport settlement are deliberately separate.
class AgentModelAttempt<T> {
  AgentModelAttempt._(this.result, this.settled, this._owner);
  final Future<T> result;
  final Future<void> settled;
  final _ModelRequest? _owner;
  bool get dispatched => _owner?.dispatched ?? false;
}

class AgentModelApi {
  AgentModelApi({
    required String baseUrl,
    required String ownerToken,
    required http.Client Function() clientFactory,
    this.resultTimeout = const Duration(seconds: 10),
    this.cleanupTimeout = const Duration(seconds: 1),
  }) : baseUrl = normalizeServiceRoot(baseUrl),
       _token = ownerToken,
       _factory = clientFactory;

  final String baseUrl, _token;
  final http.Client Function() _factory;
  final Duration resultTimeout, cleanupTimeout;
  final Set<_ModelRequest> _owners = {};
  // Retention/capacity outlives every facade, widget and logical pending record.
  static final Set<_ModelRequest> _actualOwners = {};
  bool _closed = false;
  Future<void>? _closing;

  bool sameOwner(AgentModelApi other) =>
      baseUrl == other.baseUrl && _token == other._token;
  bool get hasCapacity => !_closed && _actualOwners.length < 32;

  AgentModelAttempt<AgentModelReceipt> change(AgentModelChange request) =>
      _start(
        'POST',
        '/v1/agent/options/models',
        request.body,
        (raw) => AgentModelReceipt.fromJson(raw, request),
      );

  AgentModelAttempt<AgentModelLookup> lookup(AgentModelChange request) =>
      _start(
        'POST',
        '/v1/agent/options/model-changes/lookup',
        request.body,
        (raw) => AgentModelLookup.fromJson(raw, request),
      );

  /// Legacy inspection is never authority to settle a bound pending request.
  AgentModelAttempt<Map<String, Object?>> legacyReceipt(String key) {
    if (!RegExp(r'^[A-Za-z0-9:._-]{1,128}$').hasMatch(key)) {
      return _failure(const AgentModelFailure('Invalid model receipt key'));
    }
    return _start(
      'GET',
      '/v1/agent/options/model-changes/${Uri.encodeComponent(key)}',
      null,
      (raw) {
        if (raw is! Map<String, Object?>) throw const FormatException();
        return raw;
      },
    );
  }

  AgentModelAttempt<T> _failure<T>(AgentModelFailure error) =>
      AgentModelAttempt._(Future<T>.error(error), Future<void>.value(), null);

  AgentModelAttempt<T> _start<T>(
    String method,
    String path,
    Map<String, Object?>? body,
    T Function(Object?) decode,
  ) {
    final elapsed = Stopwatch()..start();
    if (!hasCapacity) {
      return _failure(
        const AgentModelFailure('Model transport is busy or closed'),
      );
    }
    if (ConnectionProfile.validateServiceUrl(baseUrl) != null ||
        _token.isEmpty ||
        _token.length > 4096 ||
        _token.codeUnits.any((c) => c <= 32 || c >= 127) ||
        resultTimeout <= Duration.zero ||
        resultTimeout > const Duration(seconds: 10) ||
        cleanupTimeout <= Duration.zero ||
        cleanupTimeout > const Duration(seconds: 1)) {
      return _failure(const AgentModelFailure('Invalid model connection'));
    }
    _ModelRequest owner;
    http.Request request;
    try {
      final bytes = body == null ? <int>[] : utf8.encode(jsonEncode(body));
      if (bytes.length > 4096) {
        return _failure(
          const AgentModelFailure('Model request exceeds its bound'),
        );
      }
      request = http.Request(method, Uri.parse('$baseUrl$path'))
        ..followRedirects = false
        ..headers.addAll({
          'Authorization': 'Bearer $_token',
          'Accept': 'application/json',
          'Accept-Encoding': 'identity',
          if (body != null) 'Content-Type': 'application/json',
        });
      if (body != null) request.bodyBytes = bytes;
      owner = _ModelRequest(_factory(), cleanupTimeout);
    } catch (_) {
      return _failure(
        const AgentModelFailure('Model transport is unavailable'),
      );
    }
    _owners.add(owner);
    _actualOwners.add(owner);
    owner.onSettled = () {
      if (!owner.isSettled) return;
      _owners.remove(owner);
      _actualOwners.remove(owner);
      if (!owner.settled.isCompleted) owner.settled.complete();
    };
    if (elapsed.elapsed >= resultTimeout) {
      // Synchronous acquisition cannot be interrupted. Once it returns late,
      // close its owned client without dispatching a request or crediting time.
      owner._readFinished = true;
      unawaited(owner.close());
      return AgentModelAttempt._(
        Future<T>.error(const AgentModelFailure('Model request timed out')),
        owner.settled.future,
        owner,
      );
    }
    final execution = _run(owner, request, decode, elapsed);
    final remaining = resultTimeout - elapsed.elapsed;
    final result = execution.timeout(
      remaining > Duration.zero ? remaining : Duration.zero,
      onTimeout: () {
        // Bound the public refusal without pretending the real send/close ended.
        unawaited(owner.close());
        throw AgentModelFailure(
          'Model request timed out',
          unconfirmed: owner.dispatched,
        );
      },
    );
    return AgentModelAttempt._(result, owner.settled.future, owner);
  }

  Future<T> _run<T>(
    _ModelRequest owner,
    http.Request request,
    T Function(Object?) decode,
    Stopwatch elapsed,
  ) async {
    T? result;
    AgentModelFailure? failure;
    try {
      final raw = await owner.read(request);
      if (elapsed.elapsed >= resultTimeout) {
        throw TimeoutException('Model deadline');
      }
      // Reuse the bounded duplicate-key/unsafe-number/UTF8 rejecting codec.
      result = decode(providerJsonDecode(raw));
    } on _ModelStatus catch (error) {
      failure = AgentModelFailure(
        'Model request was refused',
        status: error.status,
        unconfirmed: owner.dispatched,
      );
    } catch (_) {
      failure = AgentModelFailure(
        'Model result is unconfirmed',
        unconfirmed: owner.dispatched,
      );
    } finally {
      if (!await owner.close()) {
        failure = AgentModelFailure(
          'Model transport cleanup is unconfirmed',
          unconfirmed: owner.dispatched,
        );
      }
    }
    if (elapsed.elapsed >= resultTimeout) {
      failure = AgentModelFailure(
        'Model request timed out',
        unconfirmed: owner.dispatched,
      );
    }
    if (failure != null) throw failure;
    return result as T;
  }

  Future<void> close() {
    _closed = true;
    return _closing ??= Future.wait(
      List.of(_owners).map((owner) async {
        await owner.close();
      }),
    );
  }
}

class _ModelStatus implements Exception {
  _ModelStatus(this.status);
  final int status;
}

class _ModelRequest {
  _ModelRequest(this.client, this.cleanupTimeout);
  final http.Client client;
  final Duration cleanupTimeout;
  final Completer<void> settled = Completer<void>();
  void Function()? onSettled;
  StreamIterator<List<int>>? _reader;
  Future<void>? _cancel;
  Future<bool>? _closing;
  bool _closed = false,
      _readFinished = false,
      _clientClosed = false,
      _subscriptionSettled = false;
  bool dispatched = false;
  bool get isSettled =>
      _readFinished &&
      _clientClosed &&
      (_reader == null || _subscriptionSettled);

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
        throw const FormatException();
      }
      if (response.statusCode != 200) throw _ModelStatus(response.statusCode);
      if (response.headers['content-type']
                  ?.split(';')
                  .first
                  .trim()
                  .toLowerCase() !=
              'application/json' ||
          !{'', 'identity'}.contains(
            (response.headers['content-encoding'] ?? '').trim().toLowerCase(),
          ) ||
          (response.contentLength != null && response.contentLength! > 16384)) {
        throw const FormatException();
      }
      final bytes = <int>[];
      var more = await first;
      while (more) {
        if (_closed) throw const FormatException();
        final chunk = _reader!.current;
        if (bytes.length + chunk.length > 16384) throw const FormatException();
        bytes.addAll(chunk);
        more = await _reader!.moveNext();
      }
      // Natural EOF is actual subscription settlement. Do not await a synthetic
      // cancellation of an already completed subscription in a different Zone.
      if (!_closed) _subscriptionSettled = true;
      if (response.contentLength != null &&
          response.contentLength != bytes.length) {
        throw const FormatException();
      }
      return bytes;
    } finally {
      _readFinished = true;
      onSettled?.call();
    }
  }

  Future<void> _cancelReader() {
    final reader = _reader;
    if (reader == null || _subscriptionSettled) return Future<void>.value();
    if (_cancel != null) return _cancel!;
    final cancel = _cancel = reader.cancel();
    unawaited(
      cancel.then<void>((_) {
        _subscriptionSettled = true;
        onSettled?.call();
      }, onError: (Object _, StackTrace __) {}),
    );
    return cancel;
  }

  Future<void> _awaitCancellation() async {
    final reader = _reader;
    if (reader == null || _subscriptionSettled) return;
    await _cancelReader().timeout(cleanupTimeout);
    _subscriptionSettled = true;
    onSettled?.call();
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
