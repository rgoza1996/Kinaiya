/// Request-owned bounded workspace reads; independent of task and voice clients.
library;

import 'dart:async';
import 'dart:convert';
import 'package:http/http.dart' as http;
import '../config/connection.dart';
import '../config/service_root.dart';

class WorkspaceReadFailure implements Exception {
  const WorkspaceReadFailure(this.message, {this.status});
  final String message;
  final int? status;
  @override
  String toString() => message;
}

class AgentWorkspaceReader {
  AgentWorkspaceReader({
    required String ownerToken,
    required http.Client Function() clientFactory,
    this.requestTimeout = const Duration(seconds: 5),
    this.cleanupTimeout = const Duration(milliseconds: 300),
  }) : _token = ownerToken,
       _factory = clientFactory;
  final String _token;
  final http.Client Function() _factory;
  final Duration requestTimeout, cleanupTimeout;
  final Set<_WorkspaceRead> _owners = {};
  bool _closed = false;
  Future<void> close() async {
    _closed = true;
    await Future.wait(
      List.of(_owners).map((owner) async {
        await owner.close();
        if (owner.settled) _owners.remove(owner);
      }),
    );
  }

  Future<Object?> get(
    String base,
    String path, {
    Map<String, String>? query,
  }) async {
    if (_closed || _owners.length >= 3) {
      throw const WorkspaceReadFailure('Workspace reader is busy or closed');
    }
    if (ConnectionProfile.validateServiceUrl(base) != null ||
        _token.isEmpty ||
        _token.length > 4096 ||
        _token.codeUnits.any((c) => c <= 32 || c >= 127) ||
        requestTimeout <= Duration.zero ||
        cleanupTimeout <= Duration.zero) {
      throw const WorkspaceReadFailure('Invalid workspace connection');
    }
    final uri = Uri.parse(
      '${normalizeServiceRoot(base)}$path',
    ).replace(queryParameters: query);
    final request = http.Request('GET', uri)
      ..followRedirects = false
      ..headers.addAll({
        'Authorization': 'Bearer $_token',
        'Accept': 'application/json',
        'Accept-Encoding': 'identity',
      });
    _WorkspaceRead? owner;
    Object? result;
    WorkspaceReadFailure? failure;
    try {
      final owned = _WorkspaceRead(_factory(), cleanupTimeout);
      owner = owned;
      _owners.add(owned);
      owned.onSettled = () {
        if (owned.settled) _owners.remove(owned);
      };
      final raw = await owned.read(request).timeout(requestTimeout);
      result = jsonDecode(utf8.decode(raw, allowMalformed: false));
    } on _WorkspaceStatus catch (error) {
      failure = WorkspaceReadFailure(
        'Workspace read was refused',
        status: error.status,
      );
    } on TimeoutException {
      failure = const WorkspaceReadFailure('Workspace read timed out');
    } catch (_) {
      failure = const WorkspaceReadFailure('Workspace read failed');
    } finally {
      if (owner != null) {
        if (!await owner.close()) {
          failure = const WorkspaceReadFailure(
            'Workspace read cleanup is unconfirmed',
          );
        }
        if (owner.settled) _owners.remove(owner);
      }
    }
    if (failure != null) throw failure;
    return result;
  }
}

class _WorkspaceStatus implements Exception {
  _WorkspaceStatus(this.status);
  final int status;
}

class _WorkspaceRead {
  _WorkspaceRead(this.client, this.cleanupTimeout);
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
        throw const WorkspaceReadFailure('Workspace read ended');
      }
      if (response.statusCode != 200) {
        throw _WorkspaceStatus(response.statusCode);
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
              response.contentLength! > 1024 * 1024)) {
        throw const FormatException('Invalid workspace response');
      }
      final bytes = <int>[];
      var more = await first;
      while (more) {
        if (_closed) throw const WorkspaceReadFailure('Workspace read ended');
        final chunk = _reader!.current;
        if (bytes.length + chunk.length > 1024 * 1024) {
          throw const FormatException('Invalid workspace response');
        }
        bytes.addAll(chunk);
        more = await _reader!.moveNext();
      }
      if (response.contentLength != null &&
          response.contentLength != bytes.length) {
        throw const FormatException('Invalid workspace response');
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
