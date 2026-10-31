/// Bounded owner-only evidence reads. No inference, writes or automatic retries.
library;

import 'dart:async';
import 'dart:convert';
import 'package:http/http.dart' as http;
import 'decisions_models.dart';
import '../config/service_root.dart';

class DecisionsFailure implements Exception {
  const DecisionsFailure(this.message, {this.status});
  final String message;
  final int? status;
  @override
  String toString() => message;
}

class DecisionsApi {
  DecisionsApi({
    required this.base,
    required String ownerToken,
    http.Client Function()? clientFactory,
    this.timeout = const Duration(seconds: 10),
  }) : _token = ownerToken,
       _factory = clientFactory ?? http.Client.new;
  final String base, _token;
  final http.Client Function() _factory;
  final Duration timeout;
  final Map<http.Client, void Function()> _owners = {};
  bool _closed = false;

  void close() {
    _closed = true;
    for (final close in List.of(_owners.values)) {
      close();
    }
  }

  Future<DecisionPage> list(
    Map<String, String> filters, {
    int? through,
    int? before,
  }) async => DecisionPage.fromJson(
    await _get('/v1/decisions/inspections', {
      ...filters,
      'limit': '20',
      if (through != null) 'through_sequence': '$through',
      if (through != null) 'before_sequence': '${before ?? through + 1}',
    }, 256 * 1024),
  );

  Future<DecisionDetail> detail(String id, {int? through, int? before}) async {
    if (id.isEmpty || id.length > 128) {
      throw const DecisionsFailure('Invalid source identity');
    }
    final result = DecisionDetail.fromJson(
      await _get('/v1/decisions/inspections/${Uri.encodeComponent(id)}', {
        'limit': '5',
        if (through != null) 'through_call_sequence': '$through',
        if (before != null) 'before_call_sequence': '$before',
      }, 4 * 1024 * 1024),
    );
    if (result.summary.eventId != id) {
      throw const DecisionsFailure('Source identity mismatch');
    }
    return result;
  }

  Future<Map<String, Object?>> _get(
    String path,
    Map<String, String> query,
    int maxBytes,
  ) async {
    if (_closed || _owners.length >= 2) {
      throw const DecisionsFailure('Evidence reader is busy or closed');
    }
    final Uri root;
    try {
      root = Uri.parse(normalizeServiceRoot(base));
    } on FormatException {
      throw const DecisionsFailure('Invalid Session service URL');
    }
    final uri = root.replace(path: '${root.path}$path', queryParameters: query);
    final client = _factory();
    var closed = false;
    void close() {
      if (!closed) {
        closed = true;
        try {
          client.close();
        } catch (_) {
          /* No transport text in UI. */
        }
      }
    }

    _owners[client] = close;
    final pending = (() async {
      final request = http.Request('GET', uri)..followRedirects = false;
      request.headers['Authorization'] = 'Bearer $_token';
      final response = await client.send(request);
      if (response.statusCode != 200) {
        throw DecisionsFailure(
          response.statusCode == 404
              ? 'Decisions inspection is unavailable on this host.'
              : 'Could not read Decisions evidence (HTTP ${response.statusCode}).',
          status: response.statusCode,
        );
      }
      if ((response.contentLength ?? 0) > maxBytes) {
        throw const DecisionsFailure(
          'Evidence response exceeds its size limit',
        );
      }
      final bytes = <int>[];
      await for (final chunk in response.stream) {
        if (bytes.length + chunk.length > maxBytes) {
          throw const DecisionsFailure(
            'Evidence response exceeds its size limit',
          );
        }
        bytes.addAll(chunk);
      }
      final value = jsonDecode(utf8.decode(bytes, allowMalformed: false));
      if (value is! Map) {
        throw const DecisionsFailure('Invalid Decisions evidence');
      }
      return value.cast<String, Object?>();
    })();
    // A timeout requests transport closure but does not prove its future settled.
    // Retain ownership until settlement, so stalled adapters cannot accumulate.
    pending.then<void>(
      (_) {
        _owners.remove(client);
        close();
      },
      onError: (Object _, StackTrace __) {
        _owners.remove(client);
        close();
      },
    );
    try {
      return await pending.timeout(timeout);
    } on DecisionsFailure {
      rethrow;
    } on TimeoutException {
      throw const DecisionsFailure(
        'Evidence read timed out. Refresh to retry.',
      );
    } catch (_) {
      throw const DecisionsFailure('Could not read valid Decisions evidence.');
    } finally {
      close();
    }
  }
}
