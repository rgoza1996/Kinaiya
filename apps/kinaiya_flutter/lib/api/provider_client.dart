/// Independently owned bounded provider HTTP. No discovery or automatic retry.
library;

import 'dart:async';
import 'dart:convert';
import 'package:http/http.dart' as http;
import '../config/connection.dart';
import '../config/service_root.dart';
import 'provider_settings.dart';

class ProviderFailure implements Exception {
  const ProviderFailure(this.message, {this.status, this.unconfirmed = false});
  final String message;
  final int? status;
  final bool unconfirmed;
  @override
  String toString() => message;
}

class ProviderApi {
  ProviderApi({
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
  final Set<_OwnedRequest> _owners = {};
  bool _closed = false;
  Future<void>? _closing;

  /// Compare owner scope in RAM without exposing credentials to UI metadata.
  bool sameOwner(ProviderApi other) =>
      baseUrl == other.baseUrl && _token == other._token;

  Future<void> close() {
    _closed = true;
    return _closing ??= Future.wait(
      List.of(_owners).map((owner) async {
        await owner.close();
        if (owner.settled) _owners.remove(owner);
      }),
    ).then((_) {});
  }

  Future<ProviderCatalogSnapshot> inspect() =>
      _request('GET', '/v1/providers', null, ProviderCatalogSnapshot.fromJson);

  Future<ProviderChangeReceipt> change(Map<String, Object?> request) {
    // Detach exactly once; this contains a key only for an explicit import.
    // Request bodies and failure details are never interpolated into errors.
    Map<String, Object?> body;
    try {
      body = jsonDecode(jsonEncode(request)) as Map<String, Object?>;
    } catch (_) {
      throw const ProviderFailure('Invalid provider change');
    }
    if (body['schema_version'] != '1.0' ||
        body['expected_revision'] is! int ||
        (body['expected_revision'] as int) < 0 ||
        (body['expected_revision'] as int) >= providerMaxRevision ||
        body['idempotency_key'] is! String ||
        !RegExp(
          r'^[A-Za-z0-9:._-]{1,128}$',
        ).hasMatch(body['idempotency_key'] as String) ||
        !providerOperations.contains(body['operation'])) {
      throw const ProviderFailure('Invalid provider change');
    }
    return _request('POST', '/v1/providers/changes', body, (value) {
      final receipt = ProviderChangeReceipt.fromJson(value);
      if (!receipt.matches(body)) throw const FormatException();
      return receipt;
    }).whenComplete(() {
      body.remove('key');
    });
  }

  Future<ProviderChangeReceipt> receipt(String key) {
    if (!RegExp(r'^[A-Za-z0-9:._-]{1,128}$').hasMatch(key)) {
      throw const ProviderFailure('Invalid provider receipt key');
    }
    return _request(
      'GET',
      '/v1/providers/changes/${Uri.encodeComponent(key)}',
      null,
      ProviderChangeReceipt.fromJson,
    );
  }

  Future<T> _request<T>(
    String method,
    String path,
    Map<String, Object?>? body,
    T Function(Object?) decode,
  ) async {
    final write = method == 'POST';
    if (_closed || _owners.length >= 4) {
      throw const ProviderFailure('Provider transport is busy or closed');
    }
    if (ConnectionProfile.validateServiceUrl(baseUrl) != null ||
        _token.isEmpty ||
        _token.length > 4096 ||
        _token.codeUnits.any((c) => c <= 32 || c >= 127) ||
        requestTimeout <= Duration.zero ||
        cleanupTimeout <= Duration.zero) {
      throw const ProviderFailure('Invalid provider connection');
    }
    final bytes = body == null ? <int>[] : utf8.encode(jsonEncode(body));
    if (bytes.length > 65536) {
      throw const ProviderFailure('Provider change exceeds its request bound');
    }
    final request = http.Request(method, Uri.parse('$baseUrl$path'))
      ..followRedirects = false
      ..headers.addAll({
        'Authorization': 'Bearer $_token',
        'Accept': 'application/json',
        'Accept-Encoding': 'identity',
        if (write) 'Content-Type': 'application/json',
      });
    if (body != null) request.bodyBytes = bytes;
    _OwnedRequest? owner;
    T? result;
    ProviderFailure? failure;
    try {
      final owned = _OwnedRequest(_factory(), cleanupTimeout);
      owner = owned;
      _owners.add(owned);
      owned.onSettled = () {
        if (owned.settled) _owners.remove(owned);
      };
      final raw = await owned.read(request).timeout(requestTimeout);
      result = decode(providerJsonDecode(raw));
    } on _StatusFailure catch (error) {
      failure = ProviderFailure(
        'Provider request was refused',
        status: error.status,
        unconfirmed: write && (error.status < 400 || error.status >= 500),
      );
    } on TimeoutException {
      failure = ProviderFailure(
        'Provider request timed out',
        unconfirmed: write && (owner?.dispatched ?? false),
      );
    } on FormatException {
      failure = ProviderFailure(
        'Invalid provider response',
        unconfirmed: write && (owner?.dispatched ?? false),
      );
    } catch (_) {
      failure = ProviderFailure(
        'Provider request failed',
        unconfirmed: write && (owner?.dispatched ?? false),
      );
    } finally {
      if (owner != null) {
        if (!await owner.close()) {
          failure = ProviderFailure(
            'Provider transport cleanup is unconfirmed',
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
  bool _closed = false,
      _finished = false,
      _clientClosed = false,
      _cancelSettled = false;
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
        throw const ProviderFailure('Provider request ended');
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
        throw const FormatException();
      }
      final bytes = <int>[];
      var more = await first;
      while (more) {
        if (_closed) throw const ProviderFailure('Provider request ended');
        final chunk = _reader!.current;
        if (bytes.length + chunk.length > 256 * 1024) {
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
