/// Owner-authenticated, bounded artifact reads and presentation-only actions.
library;

import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:http/http.dart' as http;
import 'package:crypto/crypto.dart';

import '../artifacts/models.dart';
import '../artifacts/media.dart';
import '../config/service_root.dart';
import 'models.dart';

class ArtifactFailure implements Exception {
  const ArtifactFailure(this.message, {this.status});
  final String message;
  final int? status;
  @override
  String toString() => message;
}

class ArtifactPage {
  ArtifactPage(this.items, this.nextAfter);
  final List<IntelligentArtifact> items;
  final int? nextAfter;
}

class ArtifactApi {
  ArtifactApi({
    required String base,
    required String ownerToken,
    http.Client Function()? clientFactory,
    this.timeout = const Duration(seconds: 6),
    this.cleanupTimeout = const Duration(milliseconds: 300),
  }) : _base = normalizeServiceRoot(base),
       _token = ownerToken,
       _factory = clientFactory ?? http.Client.new;

  final String _base, _token;
  final http.Client Function() _factory;
  final Duration timeout, cleanupTimeout;
  final Set<_ArtifactRequest> _owners = {};
  bool _closed = false;

  Map<String, String> _scope(SessionInfo session) => {
    'session_id': session.sessionId,
    'thread_id': session.threadId,
    'epoch': '${session.epoch}',
  };

  Future<ArtifactPage> page(SessionInfo session, {int after = 0}) async {
    final value = await _request('GET', '/v1/artifacts', {
      ..._scope(session),
      'after': '$after',
      'limit': '20',
    }, maxBytes: 2 * 1024 * 1024);
    final items = value['items'], next = value['next_after'];
    if (items is! List ||
        items.length > 20 ||
        (next != null &&
            (next is! int ||
                next < after ||
                next > 9007199254740991 ||
                (items.isNotEmpty && next <= after)))) {
      throw const ArtifactFailure('Invalid artifact page.');
    }
    final parsed = items.map(_readable).toList();
    if (parsed.any((entry) => !_matches(entry, session))) {
      throw const ArtifactFailure('Artifact session identity does not match.');
    }
    return ArtifactPage(List.unmodifiable(parsed), next as int?);
  }

  Future<IntelligentArtifact> detail(SessionInfo session, String id) async =>
      _checked(await _request('GET', _path(id), _scope(session)), session, id);

  Future<ArtifactMedia> media(
    SessionInfo session,
    IntelligentArtifact artifact,
    String assetId,
    String variant,
  ) async {
    if (!_matches(artifact, session) ||
        !{'source', 'highlighted'}.contains(variant) ||
        !RegExp(r'^media_[a-f0-9]{32}$').hasMatch(assetId) ||
        !artifact.components.any(
          (c) => c.type == 'media' && c.data['asset_id'] == assetId,
        )) {
      throw const ArtifactFailure('Invalid scoped video reference.');
    }
    final route = '${_path(artifact.artifactId)}/media/$assetId';
    final metadata = await _request(
      'GET',
      route,
      _scope(session),
      maxBytes: 16384,
    );
    final binding = metadata['binding'], part = metadata[variant];
    bool digest(Object? value) =>
        value is String && RegExp(r'^[a-f0-9]{64}$').hasMatch(value);
    if (metadata['schema_version'] != 'artifact_media.v1' ||
        metadata['asset_id'] != assetId ||
        metadata['publication_only'] != true ||
        metadata['content_scope'] != artifact.contentScope ||
        !digest(metadata['overlay_sha256']) ||
        binding is! Map ||
        binding['task_id'] != artifact.taskId ||
        binding['objective_revision'] != artifact.objectiveRevision ||
        binding['session_id'] != session.sessionId ||
        binding['thread_id'] != session.threadId ||
        binding['epoch'] != session.epoch ||
        part is! Map ||
        part['mime_type'] != 'video/mp4' ||
        !digest(part['sha256']) ||
        part['bytes'] is! int ||
        (part['bytes'] as int) < 16 ||
        (part['bytes'] as int) > 32 * 1024 * 1024) {
      throw const ArtifactFailure('Invalid native video receipt.');
    }
    final bytes = await _videoBytes(route, {
      ..._scope(session),
      'variant': variant,
    }, part['bytes'] as int);
    if (bytes.length != part['bytes'] ||
        sha256.convert(bytes).toString() != part['sha256'] ||
        ascii.decode(bytes.sublist(4, 8), allowInvalid: true) != 'ftyp') {
      throw const ArtifactFailure(
        'Native video bytes differ from their receipt.',
      );
    }
    return ArtifactMedia(
      Uint8List.fromList(bytes),
      variant: variant,
      sha256: part['sha256'] as String,
    );
  }

  Future<List<int>> _videoBytes(
    String route,
    Map<String, String> query,
    int maxBytes,
  ) async {
    if (_closed || _owners.length >= 2) {
      throw const ArtifactFailure('Artifact connection is busy or closed.');
    }
    final owner = _ArtifactRequest(_factory(), cleanupTimeout);
    _owners.add(owner);
    owner.onSettled = () {
      if (owner.settled) _owners.remove(owner);
    };
    List<int>? result;
    ArtifactFailure? failure;
    try {
      final request = http.Request(
        'GET',
        Uri.parse('$_base$route/content').replace(queryParameters: query),
      )..followRedirects = false;
      request.headers['Authorization'] = 'Bearer $_token';
      request.headers['Accept'] = 'video/mp4';
      result = await owner
          .read(request, maxBytes, expectedMime: 'video/mp4')
          .timeout(timeout);
    } catch (_) {
      failure = const ArtifactFailure(
        'Could not verify the scoped video response.',
      );
    } finally {
      if (!await owner.close()) {
        failure = const ArtifactFailure(
          'Video transport cleanup is unconfirmed.',
        );
      }
      if (owner.settled) _owners.remove(owner);
    }
    if (failure != null) throw failure;
    return result!;
  }

  Future<IntelligentArtifact> action(
    SessionInfo session,
    IntelligentArtifact artifact,
    String componentId,
    Object? value,
    String idempotencyKey,
  ) async {
    if (!_matches(artifact, session)) {
      throw const ArtifactFailure('Artifact session identity does not match.');
    }
    final result = await _request(
      'POST',
      '${_path(artifact.artifactId)}/actions',
      {},
      body: {
        'session_id': session.sessionId,
        'thread_id': session.threadId,
        'epoch': session.epoch,
        'expected_revision': artifact.revision,
        'component_id': componentId,
        'value': value,
        'idempotency_key': idempotencyKey,
      },
    );
    return _checked(result, session, artifact.artifactId);
  }

  static String _path(String id) {
    if (id.isEmpty || id.length > 128) {
      throw const ArtifactFailure('Invalid artifact identity.');
    }
    return '/v1/artifacts/${Uri.encodeComponent(id)}';
  }

  static bool _matches(IntelligentArtifact value, SessionInfo session) =>
      value.sessionId == session.sessionId &&
      value.threadId == session.threadId &&
      value.epoch == session.epoch;

  IntelligentArtifact _checked(Object? raw, SessionInfo session, String id) {
    final value = _readable(raw);
    if (value.artifactId != id || !_matches(value, session)) {
      throw const ArtifactFailure('Artifact identity does not match.');
    }
    return value;
  }

  static IntelligentArtifact _readable(Object? raw) =>
      IntelligentArtifact.tryParse(raw) ??
      IntelligentArtifact.readableFallback(raw);

  Future<Map<String, Object?>> _request(
    String method,
    String path,
    Map<String, String> query, {
    Map<String, Object?>? body,
    int maxBytes = intelligentArtifactMaxBytes,
  }) async {
    if (_closed || _owners.length >= 2) {
      throw const ArtifactFailure('Artifact connection is busy or closed.');
    }
    if (timeout <= Duration.zero || cleanupTimeout <= Duration.zero) {
      throw const ArtifactFailure('Invalid artifact request limits.');
    }
    _ArtifactRequest? owner;
    Map<String, Object?>? result;
    ArtifactFailure? failure;
    try {
      final uri = Uri.parse(
        '$_base$path',
      ).replace(queryParameters: query.isEmpty ? null : query);
      final request = http.Request(method, uri)..followRedirects = false;
      request.headers['Authorization'] = 'Bearer $_token';
      if (body != null) {
        request.headers['Content-Type'] = 'application/json';
        request.body = jsonEncode(body);
        if (request.bodyBytes.length > 8192) {
          throw const ArtifactFailure('Artifact action exceeds its limit.');
        }
      }
      final owned = _ArtifactRequest(_factory(), cleanupTimeout);
      owner = owned;
      _owners.add(owned);
      owned.onSettled = () {
        if (owned.settled) _owners.remove(owned);
      };
      final bytes = await owned.read(request, maxBytes).timeout(timeout);
      final decoded = jsonDecode(utf8.decode(bytes, allowMalformed: false));
      if (decoded is! Map) {
        throw const ArtifactFailure('Invalid artifact response.');
      }
      result = decoded.cast<String, Object?>();
    } on ArtifactFailure catch (error) {
      failure = error;
    } on TimeoutException {
      failure = const ArtifactFailure(
        'Artifact request timed out; its result is unconfirmed.',
      );
    } catch (_) {
      failure = const ArtifactFailure(
        'Could not read a valid artifact response.',
      );
    } finally {
      if (owner != null) {
        if (!await owner.close()) {
          // Even an otherwise valid POST response does not release its exact
          // intent when transport cleanup remains unconfirmed.
          failure = const ArtifactFailure(
            'Artifact request cleanup is unconfirmed; its result is unconfirmed.',
          );
        }
        if (owner.settled) _owners.remove(owner);
      }
    }
    if (failure != null) throw failure;
    return result!;
  }

  Future<void> close() async {
    _closed = true;
    await Future.wait(
      List.of(_owners).map((owner) async {
        await owner.close();
        if (owner.settled) _owners.remove(owner);
      }),
    );
  }
}

/// Owns the actual send and response subscription past logical deadlines. A
/// deadline or failed close cannot silently free one of the two request slots.
class _ArtifactRequest {
  _ArtifactRequest(this.client, this.cleanupTimeout);
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
  bool get settled =>
      _finished && _clientClosed && (_reader == null || _cancelSettled);

  Future<List<int>> read(
    http.Request request,
    int maxBytes, {
    String? expectedMime,
  }) async {
    try {
      final response = await client.send(request);
      _reader = StreamIterator(response.stream);
      // StreamIterator subscribes lazily. Own the sole subscription before
      // refusing headers, including responses arriving after a timed-out send.
      final first = _reader!.moveNext();
      unawaited(
        first.then<void>((_) {}, onError: (Object _, StackTrace __) {}),
      );
      if (_closed) {
        await _awaitCancellation();
        throw const ArtifactFailure(
          'Artifact request ended; its result is unconfirmed.',
        );
      }
      if (response.statusCode != 200) {
        throw ArtifactFailure(
          response.statusCode == 409
              ? 'This view changed. Refresh it before trying again.'
              : 'Artifact request failed (HTTP ${response.statusCode}).',
          status: response.statusCode,
        );
      }
      if ((response.contentLength ?? 0) > maxBytes) {
        throw const ArtifactFailure('Artifact response exceeds its limit.');
      }
      if (expectedMime != null &&
          ((response.headers['content-type'] ?? '')
                      .split(';')
                      .first
                      .trim()
                      .toLowerCase() !=
                  expectedMime ||
              (response.headers['content-encoding'] ?? 'identity')
                      .trim()
                      .toLowerCase() !=
                  'identity')) {
        throw const ArtifactFailure('Invalid native video response type.');
      }
      final bytes = <int>[];
      var more = await first;
      while (more) {
        if (_closed) {
          throw const ArtifactFailure(
            'Artifact request ended; its result is unconfirmed.',
          );
        }
        final chunk = _reader!.current;
        if (bytes.length + chunk.length > maxBytes) {
          throw const ArtifactFailure('Artifact response exceeds its limit.');
        }
        bytes.addAll(chunk);
        more = await _reader!.moveNext();
      }
      // A natural onDone is actual response-subscription settlement, not an
      // invitation to create another deadline around the SDK's completed
      // cancellation future. Explicit cancellation still owns its real future.
      if (!_closed) _cancelSettled = true;
      if (response.contentLength != null &&
          response.contentLength != bytes.length) {
        throw const ArtifactFailure('Invalid artifact response length.');
      }
      return bytes;
    } finally {
      _finished = true;
      onSettled?.call();
    }
  }

  Future<void> _cancelReader() {
    final reader = _reader;
    // Do not memoize absence: a pending send may later produce a response.
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
    if (reader == null || _cancelSettled) return;
    await _cancelReader().timeout(cleanupTimeout);
    // Actual successful await is settlement proof even when an SDK completed
    // future delivers its observer in a different Zone.
    _cancelSettled = true;
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
