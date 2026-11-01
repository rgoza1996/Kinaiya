/// Independent bounded observation reads; opening this never measures a prompt.
library;

import 'dart:async';
import 'package:http/http.dart' as http;
import '../config/connection.dart';
import '../config/service_root.dart';
import 'context_usage.dart';
import 'tool_permissions.dart' show permissionJsonDecode;

class ContextReadFailure implements Exception {
  const ContextReadFailure(this.message, {this.status});
  final String message;
  final int? status;
  @override
  String toString() => message;
}

class ContextUsageApi {
  ContextUsageApi({
    required String baseUrl,
    required String ownerToken,
    required this.role,
    required http.Client Function() clientFactory,
    this.requestTimeout = const Duration(seconds: 5),
    this.cleanupTimeout = const Duration(milliseconds: 300),
  }) : baseUrl = normalizeServiceRoot(baseUrl),
       _token = ownerToken,
       _factory = clientFactory;
  final String baseUrl, role, _token;
  final http.Client Function() _factory;
  final Duration requestTimeout, cleanupTimeout;
  final Set<_ContextRead> _owners = {};
  static final Set<_ContextRead> _allOwners = {};
  static int _reserved = 0;
  int _reservedLocal = 0;
  bool _closed = false;
  Future<void>? _closing;
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

  Future<ContextUsageSnapshot> inspect() async {
    if (_closed ||
        _owners.length + _reservedLocal >= 4 ||
        _allOwners.length + _reserved >= 16) {
      throw const ContextReadFailure('Context reader is busy or closed');
    }
    if (!{'agent', 'converse'}.contains(role) ||
        ConnectionProfile.validateServiceUrl(baseUrl) != null ||
        _token.isEmpty ||
        _token.length > 4096 ||
        _token.codeUnits.any((c) => c <= 32 || c >= 127) ||
        requestTimeout <= Duration.zero ||
        requestTimeout > const Duration(seconds: 5) ||
        cleanupTimeout <= Duration.zero ||
        cleanupTimeout > const Duration(milliseconds: 300)) {
      throw const ContextReadFailure('Invalid context connection');
    }
    final request =
        http.Request('GET', Uri.parse('$baseUrl/v1/$role/options/context'))
          ..followRedirects = false
          ..headers.addAll({
            'Authorization': 'Bearer $_token',
            'Accept': 'application/json',
            'Accept-Encoding': 'identity',
          });
    final elapsed = Stopwatch()..start();
    _ContextRead? owner;
    ContextUsageSnapshot? result;
    ContextReadFailure? failure;
    var reserved = true;
    _reserved++;
    _reservedLocal++;
    try {
      final owned = _ContextRead(_factory(), cleanupTimeout);
      owner = owned;
      _owners.add(owned);
      _allOwners.add(owned);
      _reserved--;
      _reservedLocal--;
      reserved = false;
      owned.onSettled = () {
        if (owned.settled) {
          _owners.remove(owned);
          _allOwners.remove(owned);
        }
      };
      if (_closed || elapsed.elapsed >= requestTimeout) {
        throw const ContextReadFailure('Context reader closed or timed out');
      }
      final raw = await owned
          .read(request)
          .timeout(requestTimeout - elapsed.elapsed);
      if (elapsed.elapsed >= requestTimeout) {
        throw const ContextReadFailure('Context read timed out');
      }
      result = ContextUsageSnapshot.fromJson(permissionJsonDecode(raw));
      if (result.role != role) throw const FormatException();
    } on _ContextStatus catch (error) {
      failure = ContextReadFailure(
        'Native context observations are unavailable',
        status: error.status,
      );
    } catch (_) {
      failure = const ContextReadFailure(
        'Context observations could not be confirmed',
      );
    } finally {
      if (reserved) {
        _reserved--;
        _reservedLocal--;
      }
      if (owner != null) {
        if (!await owner.close()) {
          failure = const ContextReadFailure(
            'Context read cleanup is unconfirmed',
          );
        }
        if (owner.settled) {
          _owners.remove(owner);
          _allOwners.remove(owner);
        }
      }
    }
    if (failure != null) throw failure;
    if (elapsed.elapsed >= requestTimeout) {
      throw const ContextReadFailure('Context read timed out');
    }
    return result!;
  }
}

class _ContextStatus implements Exception {
  _ContextStatus(this.status);
  final int status;
}

class _ContextRead {
  _ContextRead(this.client, this.cleanupTimeout);
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
        throw const ContextReadFailure('Context read ended');
      }
      if (response.statusCode != 200) {
        throw _ContextStatus(response.statusCode);
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
        throw const FormatException();
      }
      final bytes = <int>[];
      var more = await first;
      while (more) {
        if (_closed) {
          throw const ContextReadFailure('Context read ended');
        }
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
