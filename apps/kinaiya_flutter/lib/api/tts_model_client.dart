/// Independently bounded owner TTS model metadata and exact write recovery.
library;

import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

import '../config/connection.dart';
import '../config/service_root.dart';
import 'tts_model_settings.dart';

class TtsModelFailure implements Exception {
  const TtsModelFailure(this.message, {this.status, this.dispatched = false});
  final String message;
  final int? status;
  final bool dispatched;
  @override
  String toString() => message;
}

class _KnownTtsChange {
  _KnownTtsChange(this.owner, this.body);
  final TtsModelsApi owner;
  final Map<String, Object?> body;
  int inFlight = 0;
  bool dispatched = false;
  TtsModelLookup? fence;
  TtsModelsApi? fenceOwner;
}

class TtsModelsApi {
  TtsModelsApi({
    required String baseUrl,
    required String ownerToken,
    http.Client Function()? clientFactory,
    this.requestTimeout = const Duration(seconds: 5),
    this.cleanupTimeout = const Duration(milliseconds: 300),
  }) : baseUrl = normalizeServiceRoot(baseUrl),
       _token = ownerToken,
       _factory = clientFactory ?? http.Client.new;
  final String baseUrl;
  final String _token;
  final http.Client Function() _factory;
  final Duration requestTimeout, cleanupTimeout;
  final Set<_TtsModelRequest> _owners = {};
  static final Set<_TtsModelRequest> _allOwners = {};
  static final List<_KnownTtsChange> _known = [];
  static int _reservedOwners = 0;
  int _reservedLocal = 0;
  bool _closed = false;
  Future<void>? _closing;

  bool sameOwner(TtsModelsApi other) =>
      baseUrl == other.baseUrl && _token == other._token;
  bool get hasUnconfirmedChange => _known.any((item) => sameOwner(item.owner));

  Future<void> close() {
    _closed = true;
    return _closing ??= Future.wait(
      List.of(_owners).map((owner) async {
        await owner.close();
        if (owner.settled) {
          _owners.remove(owner);
          _allOwners.remove(owner);
        }
      }),
    ).then((_) {});
  }

  Future<TtsModelsSnapshot> inspect() => _request(
    'GET',
    '/v1/converse/options/tts-model',
    null,
    TtsModelsSnapshot.fromJson,
  );

  _KnownTtsChange? _record(String key) {
    for (final record in _known) {
      if (sameOwner(record.owner) && record.body['idempotency_key'] == key) {
        return record;
      }
    }
    return null;
  }

  bool _sameBody(Map<String, Object?> a, Map<String, Object?> b) =>
      jsonEncode(a) == jsonEncode(b);

  Future<TtsModelReceipt> change(Map<String, Object?> request) async {
    final body = ttsModelChange(request);
    final key = body['idempotency_key'] as String;
    final prior = _record(key);
    if (prior != null && !_sameBody(prior.body, body)) {
      throw const TtsModelFailure('Original TTS model request changed');
    }
    if (prior != null &&
        (prior.inFlight != 0 ||
            !identical(prior.fenceOwner, this) ||
            prior.fence == null ||
            prior.fence!.fenced ||
            !prior.fence!.matches(body))) {
      throw const TtsModelFailure(
        'Look up the original native request before an exact retry',
      );
    }
    if (prior == null && (hasUnconfirmedChange || _known.length >= 16)) {
      throw const TtsModelFailure(
        'Retained TTS model changes must be reconciled',
      );
    }
    final record = prior ?? _KnownTtsChange(this, body);
    record.fence = null;
    record.fenceOwner = null;
    if (prior == null) _known.add(record);
    record.inFlight++;
    try {
      final receipt = await _request(
        'POST',
        '/v1/converse/options/tts-model',
        body,
        (raw) {
          final receipt = TtsModelReceipt.fromJson(raw);
          if (!receipt.matches(body)) throw const FormatException();
          return receipt;
        },
      );
      _known.remove(record);
      return receipt;
    } on TtsModelFailure catch (error) {
      record.dispatched |= error.dispatched;
      rethrow;
    } finally {
      record.inFlight--;
      // A reentrant factory can start the same exact request. One local
      // refusal cannot discard the other operation's unresolved identity.
      if (!record.dispatched && record.inFlight == 0) _known.remove(record);
    }
  }

  Future<TtsModelLookup> lookup(Map<String, Object?> request) async {
    final body = ttsModelChange(request);
    final result = await _request(
      'POST',
      '/v1/converse/options/tts-model-changes/lookup',
      body,
      (raw) {
        final lookup = TtsModelLookup.fromJson(raw);
        if (!lookup.matches(body)) throw const FormatException();
        return lookup;
      },
    );
    final record = _record(body['idempotency_key'] as String);
    if (record != null && !_sameBody(record.body, body)) {
      throw const TtsModelFailure('Original TTS request changed');
    }
    if (record != null) {
      if (result.receipt != null) {
        _known.remove(record);
      } else {
        record.fence = result;
        record.fenceOwner = this;
      }
    }
    return result;
  }

  void releaseFenced(Map<String, Object?> request, TtsModelLookup proof) {
    final body = ttsModelChange(request),
        record = _record(request['idempotency_key'] as String);
    if (!proof.matches(body) ||
        !proof.fenced ||
        proof.receipt != null ||
        record == null ||
        !_sameBody(record.body, body) ||
        !identical(record.fence, proof) ||
        !identical(record.fenceOwner, this)) {
      throw const TtsModelFailure('Original TTS change is not natively fenced');
    }
    _known.remove(record);
  }

  Future<T> _request<T>(
    String method,
    String path,
    Map<String, Object?>? body,
    T Function(Object?) decode, {
    Map<String, String>? query,
  }) async {
    final write = method == 'POST';
    if (_closed ||
        _owners.length + _reservedLocal >= 4 ||
        _allOwners.length + _reservedOwners >= 16) {
      throw const TtsModelFailure('TTS model transport is busy or closed');
    }
    if (ConnectionProfile.validateServiceUrl(baseUrl) != null ||
        _token.isEmpty ||
        _token.length > 4096 ||
        _token.codeUnits.any((c) => c <= 32 || c >= 127) ||
        requestTimeout <= Duration.zero ||
        cleanupTimeout <= Duration.zero) {
      throw const TtsModelFailure('Invalid TTS model connection');
    }
    final bytes = body == null ? <int>[] : utf8.encode(jsonEncode(body));
    if (bytes.length > 4096) {
      throw const TtsModelFailure('TTS model request exceeds its bound');
    }
    final request =
        http.Request(
            method,
            Uri.parse('$baseUrl$path').replace(queryParameters: query),
          )
          ..followRedirects = false
          ..headers.addAll({
            'Authorization': 'Bearer $_token',
            'Accept': 'application/json',
            'Accept-Encoding': 'identity',
            if (write) 'Content-Type': 'application/json',
          });
    if (body != null) request.bodyBytes = bytes;
    final elapsed = Stopwatch()..start();
    _TtsModelRequest? owner;
    T? result;
    TtsModelFailure? failure;
    var reserved = true;
    _reservedOwners++;
    _reservedLocal++;
    try {
      final owned = _TtsModelRequest(_factory(), cleanupTimeout);
      owner = owned;
      _owners.add(owned);
      _allOwners.add(owned);
      _reservedOwners--;
      _reservedLocal--;
      reserved = false;
      owned.onSettled = () {
        if (owned.settled) {
          _owners.remove(owned);
          _allOwners.remove(owned);
        }
      };
      if (_closed) {
        throw const TtsModelFailure('TTS model transport closed');
      }
      final raw = await owned.read(request).timeout(requestTimeout);
      if (elapsed.elapsed >= requestTimeout) {
        throw TimeoutException('TTS model deadline');
      }
      result = decode(ttsModelJsonDecode(raw));
      if (elapsed.elapsed >= requestTimeout) {
        throw TimeoutException('TTS model deadline');
      }
    } on _TtsModelStatus catch (error) {
      failure = TtsModelFailure(
        'TTS model request was refused',
        status: error.status,
        dispatched: write && (owner?.dispatched ?? false),
      );
    } catch (_) {
      failure = TtsModelFailure(
        'TTS model request was not confirmed',
        dispatched: write && (owner?.dispatched ?? false),
      );
    } finally {
      if (reserved) {
        _reservedOwners--;
        _reservedLocal--;
      }
      if (owner != null) {
        if (!await owner.close()) {
          failure = TtsModelFailure(
            'TTS model transport cleanup is unconfirmed',
            dispatched: write && owner.dispatched,
          );
        }
        if (owner.settled) {
          _owners.remove(owner);
          _allOwners.remove(owner);
        }
      }
    }
    if (failure == null && elapsed.elapsed >= requestTimeout) {
      failure = TtsModelFailure(
        'TTS model response exceeded its deadline',
        dispatched: write && (owner?.dispatched ?? false),
      );
    }
    if (failure != null) throw failure;
    return result as T;
  }
}

class _TtsModelStatus implements Exception {
  _TtsModelStatus(this.status);
  final int status;
}

class _TtsModelRequest {
  _TtsModelRequest(this.client, this.cleanupTimeout);
  final http.Client client;
  final Duration cleanupTimeout;
  StreamIterator<List<int>>? _reader;
  Future<void>? _cancel;
  Future<bool>? _closing;
  void Function()? onSettled;
  bool _closed = false,
      _started = false,
      _finished = false,
      _clientClosed = false,
      _cancelSettled = false;
  bool dispatched = false;
  bool get settled =>
      (!_started || _finished) &&
      _clientClosed &&
      (_reader == null || _cancelSettled);

  Future<List<int>> read(http.Request request) async {
    _started = true;
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
        throw const TtsModelFailure('TTS model request ended');
      }
      if (response.statusCode != 200) {
        throw _TtsModelStatus(response.statusCode);
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
              response.contentLength! > 64 * 1024)) {
        throw const FormatException();
      }
      final bytes = <int>[];
      var more = await first;
      while (more) {
        if (_closed) {
          throw const TtsModelFailure('TTS model request ended');
        }
        final chunk = _reader!.current;
        if (bytes.length + chunk.length > 64 * 1024) {
          throw const FormatException();
        }
        bytes.addAll(chunk);
        more = await _reader!.moveNext();
      }
      if (response.contentLength != null &&
          response.contentLength != bytes.length) {
        throw const FormatException();
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
