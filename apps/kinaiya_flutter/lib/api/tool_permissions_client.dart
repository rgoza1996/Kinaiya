/// Request-owned owner permission HTTP. Opening the editor never writes policy.
library;

import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

import '../config/connection.dart';
import '../config/service_root.dart';
import 'tool_permissions.dart';
import 'permission_targets.dart';
import 'authorizations.dart';

class ToolPermissionFailure implements Exception {
  const ToolPermissionFailure(
    this.message, {
    this.status,
    this.dispatched = false,
  });
  final String message;
  final int? status;

  /// A dispatched save remains uncertain even after a non-success HTTP status.
  final bool dispatched;
  @override
  String toString() => message;
}

class ToolPermissionsApi {
  ToolPermissionsApi({
    required String baseUrl,
    required String sessionBase,
    required String ownerToken,
    http.Client Function()? clientFactory,
    this.requestTimeout = const Duration(seconds: 5),
    this.cleanupTimeout = const Duration(milliseconds: 300),
  }) : baseUrl = normalizeServiceRoot(baseUrl),
       sessionBase = normalizeServiceRoot(sessionBase),
       _token = ownerToken,
       _factory = clientFactory ?? http.Client.new;
  final String baseUrl, sessionBase;
  final String _token;
  final http.Client Function() _factory;
  final Duration requestTimeout, cleanupTimeout;
  final Set<_PermissionRequest> _owners = {};
  static final Set<_PermissionRequest> _allOwners = {};
  static int _reservedOwners = 0;
  int _reservedLocal = 0;
  bool _closed = false;
  Future<void>? _closing;

  bool sameOwner(ToolPermissionsApi other) =>
      baseUrl == other.baseUrl &&
      sessionBase == other.sessionBase &&
      _token == other._token;

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

  Future<ToolPermissionsSnapshot> inspect({
    String kind = 'default',
    String? sessionId,
    String? taskId,
    String? scopeId,
  }) {
    final query = <String, String>{};
    if (scopeId != null) {
      if (!RegExp(r'^[a-f0-9]{64}$').hasMatch(scopeId) ||
          kind != 'default' ||
          sessionId != null ||
          taskId != null) {
        throw const ToolPermissionFailure('Invalid saved permission scope');
      }
      query['scope_id'] = scopeId;
    } else {
      if (!permissionScopeKinds.contains(kind) ||
          (kind == 'session'
              ? !_target(sessionId) || taskId != null
              : sessionId != null) ||
          (kind == 'task' ? !_target(taskId) : taskId != null)) {
        throw const ToolPermissionFailure('Invalid permission scope choice');
      }
      query.addAll({
        'scope': kind,
        if (sessionId != null) 'session_id': sessionId,
        if (taskId != null) 'task_id': taskId,
      });
    }
    return _request(
      'GET',
      '/v1/agent/options/tool-permissions',
      null,
      ToolPermissionsSnapshot.fromJson,
      query: query,
    ).then((snapshot) {
      if ((scopeId != null && snapshot.scopeId != scopeId) ||
          (scopeId == null && snapshot.scope.kind != kind) ||
          (sessionId != null && snapshot.scope.sessionId != sessionId) ||
          (taskId != null && snapshot.scope.taskId != taskId)) {
        throw const ToolPermissionFailure('Permission scope response changed');
      }
      return snapshot;
    });
  }

  static bool _target(String? value) =>
      value != null &&
      value.isNotEmpty &&
      value.runes.length <= 128 &&
      !value.codeUnits.contains(0);

  Future<ToolPermissionScopesPage> savedScopes({String? after}) {
    if (after != null && !RegExp(r'^[a-f0-9]{64}$').hasMatch(after)) {
      throw const ToolPermissionFailure('Invalid saved scope cursor');
    }
    return _request(
      'GET',
      '/v1/agent/options/tool-permission-scopes',
      null,
      ToolPermissionScopesPage.fromJson,
      query: {'limit': '20', if (after != null) 'after': after},
    ).then((page) {
      if (after != null &&
          page.scopes.any((scope) => scope.scopeId.compareTo(after) <= 0)) {
        throw const ToolPermissionFailure('Saved scope page changed');
      }
      return page;
    });
  }

  Future<ToolPermissionReceipt> change(
    Map<String, Object?> request,
    String scopeId,
  ) {
    final Map<String, Object?> body;
    try {
      body = permissionChange(request);
      if (!RegExp(r'^[a-f0-9]{64}$').hasMatch(scopeId)) {
        throw const FormatException();
      }
    } catch (_) {
      throw const ToolPermissionFailure('Invalid permission change');
    }
    return _request('POST', '/v1/agent/options/tool-permissions', body, (raw) {
      final result = ToolPermissionReceipt.fromJson(raw);
      if (!result.matches(body, scopeId)) throw const FormatException();
      return result;
    });
  }

  Future<ToolPermissionReceipt> receipt(String key) => _request(
    'GET',
    '/v1/agent/options/tool-permission-changes/${Uri.encodeComponent(permissionKey(key))}',
    null,
    ToolPermissionReceipt.fromJson,
  );

  Future<PermissionTargetsPage> inspectTargets({
    String kind = 'default',
    String? sessionId,
    String? taskId,
    String? scopeId,
    String? after,
  }) {
    final query = <String, String>{'limit': '20'};
    if (after != null) {
      if (!_exactTargetHash(after)) {
        throw const ToolPermissionFailure('Invalid target cursor');
      }
      query['after'] = after;
    }
    if (scopeId != null) {
      if (!_exactTargetHash(scopeId) ||
          kind != 'default' ||
          sessionId != null ||
          taskId != null) {
        throw const ToolPermissionFailure('Invalid saved target scope');
      }
      query['scope_id'] = scopeId;
    } else {
      if (!permissionScopeKinds.contains(kind) ||
          (kind == 'session'
              ? !_target(sessionId) || taskId != null
              : sessionId != null) ||
          (kind == 'task' ? !_target(taskId) : taskId != null)) {
        throw const ToolPermissionFailure('Invalid target scope');
      }
      query.addAll({
        'scope': kind,
        if (sessionId != null) 'session_id': sessionId,
        if (taskId != null) 'task_id': taskId,
      });
    }
    return _request(
      'GET',
      '/v1/agent/options/tool-permission-targets',
      null,
      PermissionTargetsPage.fromJson,
      query: query,
    ).then((page) {
      if ((scopeId != null && page.scopeId != scopeId) ||
          (scopeId == null && page.scope.kind != kind) ||
          (sessionId != null && page.scope.sessionId != sessionId) ||
          (taskId != null && page.scope.taskId != taskId) ||
          (after != null &&
              page.targets.any((row) => row.target.id.compareTo(after) <= 0))) {
        throw const ToolPermissionFailure(
          'Permission target scope or page changed',
        );
      }
      return page;
    });
  }

  Future<PermissionTargetReceipt> changeTarget(
    Map<String, Object?> request,
    String scopeId,
  ) {
    final Map<String, Object?> body;
    try {
      body = permissionTargetChange(request);
      if (!_exactTargetHash(scopeId)) {
        throw const FormatException();
      }
    } catch (_) {
      throw const ToolPermissionFailure('Invalid target permission change');
    }
    return _request('POST', '/v1/agent/options/tool-permission-targets', body, (
      raw,
    ) {
      final receipt = PermissionTargetReceipt.fromJson(raw);
      if (!receipt.matches(body, scopeId)) throw const FormatException();
      return receipt;
    });
  }

  Future<PermissionTargetReceipt> targetReceipt(String key) => _request(
    'GET',
    '/v1/agent/options/tool-permission-changes/${Uri.encodeComponent(_exactTargetKey(key))}',
    null,
    PermissionTargetReceipt.fromJson,
  );

  static bool _exactTargetHash(String value) =>
      value.length == 64 &&
      RegExp(r'[a-f0-9]{64}').matchAsPrefix(value)?.end == value.length;

  static String _exactTargetKey(String value) {
    if (value.isEmpty ||
        value.length > 128 ||
        RegExp(r'[A-Za-z0-9:._-]{1,128}').matchAsPrefix(value)?.end !=
            value.length) {
      throw const ToolPermissionFailure('Invalid target receipt key');
    }
    return value;
  }

  Future<AuthorizationsPage> authorizations({String? after}) {
    if (after != null &&
        RegExp(r'apr_[a-f0-9]{16}').matchAsPrefix(after)?.end != after.length) {
      throw const ToolPermissionFailure('Invalid authorization cursor');
    }
    return _request(
      'GET',
      '/v1/agent/options/authorizations',
      null,
      AuthorizationsPage.fromJson,
      query: {'limit': '50', if (after != null) 'after': after},
    ).then((page) {
      if (after != null &&
          page.authorizations.any(
            (row) => row.identity.id.compareTo(after) <= 0,
          )) {
        throw const FormatException();
      }
      return page;
    });
  }

  Future<AuthorizationReceipt> revokeAuthorization(
    Map<String, Object?> original,
  ) {
    final body = authorizationRevoke(original);
    return _request('POST', '/v1/agent/options/authorizations/revoke', body, (
      raw,
    ) {
      final result = AuthorizationReceipt.fromJson(raw);
      if (!result.matches(body)) throw const FormatException();
      return result;
    });
  }

  Future<AuthorizationLookup> lookupAuthorization(
    Map<String, Object?> original,
  ) {
    final body = authorizationRevoke(original);
    return _request(
      'POST',
      '/v1/agent/options/authorizations/revocations/lookup',
      body,
      (raw) {
        final result = AuthorizationLookup.fromJson(raw);
        if (result.receipt != null && !result.receipt!.matches(body)) {
          throw const FormatException();
        }
        return result;
      },
    );
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
      throw const ToolPermissionFailure(
        'Permission transport is busy or closed',
      );
    }
    if (ConnectionProfile.validateServiceUrl(baseUrl) != null ||
        _token.isEmpty ||
        _token.length > 4096 ||
        _token.codeUnits.any((c) => c <= 32 || c >= 127) ||
        requestTimeout <= Duration.zero ||
        cleanupTimeout <= Duration.zero) {
      throw const ToolPermissionFailure('Invalid permission connection');
    }
    final bytes = body == null ? <int>[] : utf8.encode(jsonEncode(body));
    if (bytes.length > 8192) {
      throw const ToolPermissionFailure('Permission request exceeds its bound');
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
    _PermissionRequest? owner;
    T? result;
    ToolPermissionFailure? failure;
    var reserved = true;
    _reservedOwners++;
    _reservedLocal++;
    try {
      final owned = _PermissionRequest(_factory(), cleanupTimeout);
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
        throw const ToolPermissionFailure('Permission transport closed');
      }
      final raw = await owned.read(request).timeout(requestTimeout);
      if (elapsed.elapsed >= requestTimeout) {
        throw TimeoutException('Permission deadline');
      }
      result = decode(permissionJsonDecode(raw));
      if (elapsed.elapsed >= requestTimeout) {
        throw TimeoutException('Permission deadline');
      }
    } on _PermissionStatus catch (error) {
      failure = ToolPermissionFailure(
        'Permission request was refused',
        status: error.status,
        dispatched: write && (owner?.dispatched ?? false),
      );
    } catch (_) {
      failure = ToolPermissionFailure(
        'Permission request was not confirmed',
        dispatched: write && (owner?.dispatched ?? false),
      );
    } finally {
      if (reserved) {
        _reservedOwners--;
        _reservedLocal--;
      }
      if (owner != null) {
        if (!await owner.close()) {
          failure = ToolPermissionFailure(
            'Permission transport cleanup is unconfirmed',
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
      failure = ToolPermissionFailure(
        'Permission response exceeded its deadline',
        dispatched: write && (owner?.dispatched ?? false),
      );
    }
    if (failure != null) throw failure;
    return result as T;
  }
}

class _PermissionStatus implements Exception {
  _PermissionStatus(this.status);
  final int status;
}

class _PermissionRequest {
  _PermissionRequest(this.client, this.cleanupTimeout);
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
        throw const ToolPermissionFailure('Permission request ended');
      }
      if (response.statusCode != 200) {
        throw _PermissionStatus(response.statusCode);
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
          throw const ToolPermissionFailure('Permission request ended');
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
