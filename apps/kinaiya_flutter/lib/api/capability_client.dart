/// Independently bounded owner capability settings metadata and exact write recovery.
library;

import 'dart:async';
import 'dart:convert';
import 'package:http/http.dart' as http;
import '../config/connection.dart';
import '../config/service_root.dart';
import 'capability_settings.dart';

class CapabilityFailure implements Exception {
  const CapabilityFailure(this.message, {this.status, this.dispatched = false});
  final String message;
  final int? status;
  final bool dispatched;
  @override
  String toString() => message;
}

class _KnownCapabilityChange {
  _KnownCapabilityChange(this.owner, this.body);
  final CapabilityApi owner;
  final Map<String, Object?> body;
  int inFlight = 0;
  bool dispatched = false;
  CapabilityLookup? fence;
  CapabilityApi? fenceOwner;
}

class CapabilityApi {
  CapabilityApi({
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
  final Set<_CapabilityRequest> _owners = {};
  static final Set<_CapabilityRequest> _allOwners = {};
  static final List<_KnownCapabilityChange> _known = [];
  static int _reservedOwners = 0;
  int _reservedLocal = 0;
  bool _closed = false;
  Future<void>? _closing;

  bool sameOwner(CapabilityApi other) =>
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

  Future<CapabilitySnapshot> inspect(String section, {int after = 0}) {
    if (!capabilitySections.contains(section) || after < 0 || after > 1000000) {
      throw const CapabilityFailure('Invalid capability page');
    }
    return _request('GET', '/v1/agent/options/capabilities', null, (raw) {
      final result = CapabilitySnapshot.fromJson(raw);
      if (result.section != section ||
          (result.nextAfter != null &&
              result.nextAfter != after + result.items.length)) {
        throw const FormatException();
      }
      return result;
    }, query: {'section': section, 'after': '$after', 'limit': '8'});
  }

  bool hasUnconfirmedSection(String section) => _known.any(
    (item) =>
        sameOwner(item.owner) &&
        capabilityActionSection(item.body['action'] as Map<String, Object?>) ==
            section,
  );

  bool blocksAction(Map<String, Object?> action) {
    final section = capabilityActionSection(action);
    final other = _known.where(
      (item) =>
          sameOwner(item.owner) &&
          capabilityActionSection(
                item.body['action'] as Map<String, Object?>,
              ) ==
              section,
    );
    return other.any(
      (item) =>
          action['operation'] != 'disable_plugin' ||
          (item.body['action'] as Map<String, Object?>)['operation'] !=
              'evaluate_plugin',
    );
  }

  _KnownCapabilityChange? _record(String key) {
    for (final record in _known) {
      if (sameOwner(record.owner) && record.body['idempotency_key'] == key) {
        return record;
      }
    }
    return null;
  }

  bool _sameBody(Map<String, Object?> a, Map<String, Object?> b) =>
      jsonEncode(a) == jsonEncode(b);

  Future<CapabilityReceipt> change(Map<String, Object?> request) async {
    final body = capabilityChange(request);
    final key = body['idempotency_key'] as String;
    final prior = _record(key);
    if (prior != null && !_sameBody(prior.body, body)) {
      throw const CapabilityFailure(
        'Original capability settings request changed',
      );
    }
    if (prior != null &&
        (prior.inFlight != 0 ||
            !identical(prior.fenceOwner, this) ||
            prior.fence == null ||
            (prior.fence!.fenced || prior.fence!.status != 'unobserved') ||
            !prior.fence!.matches(body))) {
      throw const CapabilityFailure(
        'Look up the original native request before an exact retry',
      );
    }
    if (prior == null &&
        (blocksAction(body['action'] as Map<String, Object?>) ||
            _known.length >= 16)) {
      throw const CapabilityFailure(
        'Retained capability settings changes must be reconciled',
      );
    }
    final record = prior ?? _KnownCapabilityChange(this, body);
    record.fence = null;
    record.fenceOwner = null;
    if (prior == null) _known.add(record);
    record.inFlight++;
    try {
      final receipt = await _request(
        'POST',
        '/v1/agent/options/capabilities',
        body,
        (raw) {
          final receipt = CapabilityReceipt.fromJson(raw);
          if (!receipt.matches(body)) throw const FormatException();
          return receipt;
        },
      );
      _known.remove(record);
      return receipt;
    } on CapabilityFailure catch (error) {
      record.dispatched |= error.dispatched;
      rethrow;
    } finally {
      record.inFlight--;
      // A reentrant factory can start the same exact request. One local
      // refusal cannot discard the other operation's unresolved identity.
      if (!record.dispatched && record.inFlight == 0) _known.remove(record);
    }
  }

  Future<CapabilityLookup> lookup(Map<String, Object?> request) async {
    final body = capabilityChange(request);
    final result = await _request(
      'POST',
      '/v1/agent/options/capability-changes/lookup',
      body,
      (raw) {
        final lookup = CapabilityLookup.fromJson(raw);
        if (!lookup.matches(body)) throw const FormatException();
        return lookup;
      },
    );
    final record = _record(body['idempotency_key'] as String);
    if (record != null && !_sameBody(record.body, body)) {
      throw const CapabilityFailure('Original capability request changed');
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

  void releaseFenced(Map<String, Object?> request, CapabilityLookup proof) {
    final body = capabilityChange(request),
        record = _record(request['idempotency_key'] as String);
    if (!proof.matches(body) ||
        !proof.fenced ||
        proof.receipt != null ||
        record == null ||
        !_sameBody(record.body, body) ||
        !identical(record.fence, proof) ||
        !identical(record.fenceOwner, this)) {
      throw const CapabilityFailure(
        'Original capability change is not natively fenced',
      );
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
      throw const CapabilityFailure(
        'capability settings transport is busy or closed',
      );
    }
    if (ConnectionProfile.validateServiceUrl(baseUrl) != null ||
        _token.isEmpty ||
        _token.length > 4096 ||
        _token.codeUnits.any((c) => c <= 32 || c >= 127) ||
        requestTimeout <= Duration.zero ||
        cleanupTimeout <= Duration.zero) {
      throw const CapabilityFailure('Invalid capability settings connection');
    }
    final bytes = body == null ? <int>[] : utf8.encode(jsonEncode(body));
    if (bytes.length > 16 * 1024) {
      throw const CapabilityFailure(
        'capability settings request exceeds its bound',
      );
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
    _CapabilityRequest? owner;
    T? result;
    CapabilityFailure? failure;
    var reserved = true;
    _reservedOwners++;
    _reservedLocal++;
    try {
      final owned = _CapabilityRequest(_factory(), cleanupTimeout);
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
        throw const CapabilityFailure('capability settings transport closed');
      }
      final raw = await owned.read(request).timeout(requestTimeout);
      if (elapsed.elapsed >= requestTimeout) {
        throw TimeoutException('capability settings deadline');
      }
      result = decode(capabilityJsonDecode(raw));
      if (elapsed.elapsed >= requestTimeout) {
        throw TimeoutException('capability settings deadline');
      }
    } on _CapabilityStatus catch (error) {
      failure = CapabilityFailure(
        'capability settings request was refused',
        status: error.status,
        dispatched: write && (owner?.dispatched ?? false),
      );
    } catch (_) {
      failure = CapabilityFailure(
        'capability settings request was not confirmed',
        dispatched: write && (owner?.dispatched ?? false),
      );
    } finally {
      if (reserved) {
        _reservedOwners--;
        _reservedLocal--;
      }
      if (owner != null) {
        if (!await owner.close()) {
          failure = CapabilityFailure(
            'capability settings transport cleanup is unconfirmed',
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
      failure = CapabilityFailure(
        'capability settings response exceeded its deadline',
        dispatched: write && (owner?.dispatched ?? false),
      );
    }
    if (failure != null) throw failure;
    return result as T;
  }
}

class _CapabilityStatus implements Exception {
  _CapabilityStatus(this.status);
  final int status;
}

class _CapabilityRequest {
  _CapabilityRequest(this.client, this.cleanupTimeout);
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
        throw const CapabilityFailure('capability settings request ended');
      }
      if (response.statusCode != 200) {
        throw _CapabilityStatus(response.statusCode);
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
          throw const CapabilityFailure('capability settings request ended');
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
