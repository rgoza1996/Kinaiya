/// Shared owned settings JSON transport. Typed adapters keep domain authority separate.
library;

import 'dart:async';
import 'dart:convert';
import 'package:http/http.dart' as http;
import '../config/connection.dart';
import '../config/service_root.dart';
import 'provider_settings.dart' show providerJsonDecode;

enum SettingsDomain { model, runtime, control, standing }

class OwnedSettingsFailure implements Exception {
  const OwnedSettingsFailure(
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
class OwnedSettingsAttempt<T> {
  OwnedSettingsAttempt._(this.result, this.settled, this._owner);
  final Future<T> result;
  final Future<void> settled;
  final _SettingsRequest? _owner;
  bool get dispatched => _owner?.dispatched ?? false;
}

class OwnedSettingsTransport {
  OwnedSettingsTransport({
    required this.domain,
    required String baseUrl,
    required String ownerToken,
    required http.Client Function() clientFactory,
    this.resultTimeout = const Duration(seconds: 10),
    this.cleanupTimeout = const Duration(seconds: 1),
  }) : baseUrl = normalizeServiceRoot(baseUrl),
       _token = ownerToken,
       _factory = clientFactory;

  final SettingsDomain domain;
  String get _label => switch (domain) {
    SettingsDomain.model => 'Model',
    SettingsDomain.runtime => 'Execution settings',
    SettingsDomain.control => 'Agent mode',
    SettingsDomain.standing => 'Standing work',
  };
  String get _connectionLabel => switch (domain) {
    SettingsDomain.model => 'model',
    SettingsDomain.runtime => 'execution settings',
    SettingsDomain.control => 'Agent mode',
    SettingsDomain.standing => 'standing work',
  };
  int get _requestBytes => domain == SettingsDomain.standing ? 131072 : 4096;
  int get _responseBytes => domain == SettingsDomain.standing ? 262144 : 16384;
  final String baseUrl, _token;
  final http.Client Function() _factory;
  final Duration resultTimeout, cleanupTimeout;
  final Set<_SettingsRequest> _owners = {};
  // Retention/capacity outlives every facade, widget and logical pending record.
  static final Set<_SettingsRequest> _actualOwners = {};
  bool _closed = false;
  Future<void>? _closing;

  bool sameOwner(OwnedSettingsTransport other) =>
      domain == other.domain &&
      baseUrl == other.baseUrl &&
      _token == other._token;
  bool get hasCapacity => !_closed && _actualOwners.length < 32;

  OwnedSettingsAttempt<T> _failure<T>(OwnedSettingsFailure error) =>
      OwnedSettingsAttempt._(
        Future<T>.error(error),
        Future<void>.value(),
        null,
      );

  OwnedSettingsAttempt<T> start<T>(
    String method,
    String path,
    Map<String, Object?>? body,
    T Function(Object?) decode,
  ) {
    final elapsed = Stopwatch()..start();
    if (!hasCapacity) {
      return _failure(
        OwnedSettingsFailure('$_label transport is busy or closed'),
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
      return _failure(
        OwnedSettingsFailure('Invalid $_connectionLabel connection'),
      );
    }
    _SettingsRequest owner;
    http.Request request;
    try {
      final bytes = body == null ? <int>[] : utf8.encode(jsonEncode(body));
      if (bytes.length > _requestBytes) {
        return _failure(
          OwnedSettingsFailure('$_label request exceeds its bound'),
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
      owner = _SettingsRequest(_factory(), cleanupTimeout, _responseBytes);
    } catch (_) {
      return _failure(OwnedSettingsFailure('$_label transport is unavailable'));
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
      return OwnedSettingsAttempt._(
        Future<T>.error(OwnedSettingsFailure('$_label request timed out')),
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
        throw OwnedSettingsFailure(
          '$_label request timed out',
          unconfirmed: owner.dispatched,
        );
      },
    );
    return OwnedSettingsAttempt._(result, owner.settled.future, owner);
  }

  Future<T> _run<T>(
    _SettingsRequest owner,
    http.Request request,
    T Function(Object?) decode,
    Stopwatch elapsed,
  ) async {
    T? result;
    OwnedSettingsFailure? failure;
    try {
      final raw = await owner.read(request);
      if (elapsed.elapsed >= resultTimeout) {
        throw TimeoutException('$_label deadline');
      }
      // Reuse the bounded duplicate-key/unsafe-number/UTF8 rejecting codec.
      result = decode(providerJsonDecode(raw));
    } on _SettingsStatus catch (error) {
      failure = OwnedSettingsFailure(
        '$_label request was refused',
        status: error.status,
        unconfirmed: owner.dispatched,
      );
    } catch (_) {
      failure = OwnedSettingsFailure(
        '$_label result is unconfirmed',
        unconfirmed: owner.dispatched,
      );
    } finally {
      if (!await owner.close()) {
        failure = OwnedSettingsFailure(
          '$_label transport cleanup is unconfirmed',
          unconfirmed: owner.dispatched,
        );
      }
    }
    if (elapsed.elapsed >= resultTimeout) {
      failure = OwnedSettingsFailure(
        '$_label request timed out',
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

class _SettingsStatus implements Exception {
  _SettingsStatus(this.status);
  final int status;
}

class _SettingsRequest {
  _SettingsRequest(this.client, this.cleanupTimeout, this.responseBytes);
  final http.Client client;
  final Duration cleanupTimeout;
  final int responseBytes;
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
      if (response.statusCode != 200) {
        throw _SettingsStatus(response.statusCode);
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
              response.contentLength! > responseBytes)) {
        throw const FormatException();
      }
      final bytes = <int>[];
      var more = await first;
      while (more) {
        if (_closed) throw const FormatException();
        final chunk = _reader!.current;
        if (bytes.length + chunk.length > responseBytes) {
          throw const FormatException();
        }
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
