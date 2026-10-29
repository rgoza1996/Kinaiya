/// HTTP client for the session, Converse, Agent and commands surfaces.
///
/// All endpoints except /health require `Authorization: Bearer <owner token>`.
/// The token travels in a header only, never in URLs, storage or logs.
/// A [http.Client] can be injected so widget tests use a labeled mock.
library;

import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:uuid/uuid.dart';

import 'memory_client.dart';
import 'models.dart';

const _uuid = Uuid();

String newIdempotencyKey() => _uuid.v4();

class ApiException implements Exception {
  ApiException(this.status, this.message);
  final int status;
  final String message;
  @override
  String toString() => 'ApiException($status): $message';
}

/// Converse SSE stream event.
class ConverseEvent {
  ConverseEvent(this.type, this.raw);
  final String type;
  final Map<String, Object?> raw;

  JournalEntry? get entry {
    final e = raw['entry'];
    if (e is Map) return JournalEntry.fromJson(e.cast<String, Object?>());
    return null;
  }

  String get error => raw['error'] as String? ?? 'Unknown Converse error';
}

/// Clearly labeled mock marker for tests.
bool get isMockApi => _mockLabel != null;
String? _mockLabel;

/// Owner credentials are scoped to the configured service, including on web.
class _NoRedirectClient extends http.BaseClient {
  _NoRedirectClient(this.inner);
  final http.Client inner;
  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) {
    request.followRedirects = false;
    return inner.send(request);
  }

  @override
  void close() => inner.close();
}

class KinaiyaClient {
  KinaiyaClient({
    required this.sessionBase,
    required this.agentBase,
    required this.converseBase,
    required String ownerToken,
    http.Client? httpClient,
    http.Client Function()? memoryClientFactory,
  }) : _token = ownerToken,
       _http = _NoRedirectClient(httpClient ?? http.Client()),
       _memoryClientFactory = memoryClientFactory ?? http.Client.new;

  final String sessionBase;
  final String agentBase;
  final String converseBase;
  final String _token;
  final http.Client _http;
  final http.Client Function() _memoryClientFactory;

  /// Independent per-request transport; never shares generation/voice ownership.
  late final MemoryApi memory = MemoryApi(
    sessionBase: sessionBase,
    converseBase: converseBase,
    ownerToken: _token,
    clientFactory: _memoryClientFactory,
  );

  /// Test constructor wrapping a mock transport; always labeled.
  KinaiyaClient.mock({
    required this.sessionBase,
    required this.agentBase,
    required this.converseBase,
    required http.Client mockTransport,
    String label = 'mock',
    http.Client Function()? memoryClientFactory,
  }) : _token = 'mock-token-not-a-credential',
       _http = _NoRedirectClient(mockTransport),
       _memoryClientFactory = memoryClientFactory ?? http.Client.new {
    _mockLabel = label;
  }

  Map<String, String> get _auth => <String, String>{
    'Authorization': 'Bearer $_token',
    'Content-Type': 'application/json',
  };

  Future<Map<String, Object?>> _post(
    String base,
    String path,
    Map<String, Object?> body, {
    bool auth = true,
  }) async {
    final res = await _http.post(
      Uri.parse('$base$path'),
      headers: auth ? _auth : {'Content-Type': 'application/json'},
      body: jsonEncode(body),
    );
    return _decode(res);
  }

  Future<Map<String, Object?>> _get(
    String base,
    String path, {
    Map<String, String>? query,
    bool auth = true,
  }) async {
    var uri = Uri.parse('$base$path');
    if (query != null) uri = uri.replace(queryParameters: query);
    final res = await _http.get(uri, headers: auth ? _auth : null);
    return _decode(res);
  }

  Map<String, Object?> _decode(http.Response res) {
    if (res.statusCode >= 300) {
      throw ApiException(res.statusCode, _detail(res.body));
    }
    final v = jsonDecode(res.body);
    if (v is Map) return v.cast<String, Object?>();
    return <String, Object?>{'value': v};
  }

  static String _detail(String body) {
    try {
      final v = jsonDecode(body);
      if (v is Map && v['detail'] != null) return '${v['detail']}';
    } catch (_) {}
    return body.length > 300 ? body.substring(0, 300) : body;
  }

  // -- session service --------------------------------------------------

  Future<SessionInfo> createSession(String channel, {String name = ''}) async {
    final json = await _post(sessionBase, '/v1/sessions', <String, Object?>{
      'schema_version': schemaVersion,
      'channel': channel,
      'name': name,
    });
    return SessionInfo.fromJson(json);
  }

  Future<List<SessionInfo>> listSessions() async {
    final uri = Uri.parse('$sessionBase/v1/sessions');
    final res = await _http.get(uri, headers: _auth);
    if (res.statusCode >= 300) {
      throw ApiException(res.statusCode, _detail(res.body));
    }
    final v = jsonDecode(res.body);
    if (v is List) {
      return v
          .map((e) => SessionInfo.fromJson((e as Map).cast<String, Object?>()))
          .toList();
    }
    return const [];
  }

  Future<SessionInfo> getSession(String id) async {
    return SessionInfo.fromJson(await _get(sessionBase, '/v1/sessions/$id'));
  }

  Future<List<JournalEntry>> transcript({
    required String sessionId,
    int after = 0,
    int limit = 200,
  }) async {
    final uri = Uri.parse('$sessionBase/v1/transcript').replace(
      queryParameters: {
        'session_id': sessionId,
        'after': '$after',
        'limit': '${limit.clamp(1, 200)}',
      },
    );
    final res = await _http.get(uri, headers: _auth);
    if (res.statusCode >= 300) {
      throw ApiException(res.statusCode, _detail(res.body));
    }
    final v = jsonDecode(res.body);
    final list = v is List ? v : (v is Map ? v['entries'] : null);
    if (list is List) {
      return list
          .map((e) => JournalEntry.fromJson((e as Map).cast<String, Object?>()))
          .toList();
    }
    return const [];
  }

  Future<List<JournalEntry>> recent({
    required String sessionId,
    int limit = 100,
  }) async {
    final uri = Uri.parse('$sessionBase/v1/transcript/recent').replace(
      queryParameters: {
        'session_id': sessionId,
        'limit': '${limit.clamp(1, 200)}',
      },
    );
    final res = await _http.get(uri, headers: _auth);
    if (res.statusCode >= 300) {
      throw ApiException(res.statusCode, _detail(res.body));
    }
    final v = jsonDecode(res.body);
    final list = v is List ? v : (v is Map ? v['entries'] : null);
    if (list is List) {
      return list
          .map((e) => JournalEntry.fromJson((e as Map).cast<String, Object?>()))
          .toList();
    }
    return const [];
  }

  Future<PendingApprovalPage?> pendingApprovals({String? after}) async {
    try {
      return PendingApprovalPage.fromJson(
        await _get(
          agentBase,
          '/v1/approvals',
          query: {if (after != null) 'after': after},
        ),
      );
    } on ApiException catch (error) {
      if (error.status == 404) return null;
      rethrow;
    }
  }

  // -- Converse ----------------------------------------------------------

  Stream<ConverseEvent> streamTurn({
    required String sessionId,
    required String threadId,
    required int epoch,
    required String text,
    required String idempotencyKey,
    String inputMode = 'text',
    ContentScope contentScope = ContentScope.localOnly,
  }) async* {
    final req = http.Request(
      'POST',
      Uri.parse('$converseBase/v1/converse/turns/stream'),
    );
    req.headers.addAll(_auth);
    req.body = jsonEncode(<String, Object?>{
      'schema_version': schemaVersion,
      'session_id': sessionId,
      'thread_id': threadId,
      'epoch': epoch,
      'text': text,
      'input_mode': inputMode,
      'content_scope': contentScope.wireValue,
      'idempotency_key': idempotencyKey,
    });
    final streamed = await _http.send(req);
    if (streamed.statusCode >= 300) {
      final body = await streamed.stream.bytesToString();
      throw ApiException(streamed.statusCode, _detail(body));
    }
    var buffer = '';
    await for (final chunk in streamed.stream.transform(utf8.decoder)) {
      buffer += chunk;
      var idx = buffer.indexOf('\n\n');
      while (idx >= 0) {
        final frame = buffer.substring(0, idx);
        buffer = buffer.substring(idx + 2);
        for (final line in frame.split('\n')) {
          final t = line.trim();
          if (t.startsWith('data:')) {
            final payload =
                jsonDecode(t.substring(5).trim()) as Map<String, Object?>? ??
                const {};
            yield ConverseEvent(
              payload['type'] as String? ?? 'unknown',
              payload.cast<String, Object?>(),
            );
          }
        }
        idx = buffer.indexOf('\n\n');
      }
    }
  }

  Future<void> interruptSpeech(
    String sessionId, {
    int? expectedGeneration,
  }) async {
    await _post(converseBase, '/v1/converse/interrupt', {
      'session_id': sessionId,
      if (expectedGeneration != null) 'expected_generation': expectedGeneration,
    });
  }

  Future<void> reportDelivery({
    required String eventId,
    required int generationEpoch,
    required String chunkId,
    required String status,
    double playedSeconds = 0,
  }) async {
    await _post(converseBase, '/v1/converse/delivery', <String, Object?>{
      'schema_version': schemaVersion,
      'event_id': eventId,
      'generation_epoch': generationEpoch,
      'chunk_id': chunkId,
      'status': status,
      'played_seconds': playedSeconds,
    });
  }

  Future<bool> setConverseEnabled(bool enabled) async {
    final json = await _post(converseBase, '/v1/converse/enabled', {
      'enabled': enabled,
    });
    return json['enabled'] as bool? ?? enabled;
  }

  Future<Map<String, Object?>> converseHealth() =>
      _get(converseBase, '/health', auth: false);

  Future<Map<String, Object?>> transcribe(
    List<int> bytes,
    String filename, {
    ContentScope contentScope = ContentScope.localOnly,
  }) async {
    final req = http.MultipartRequest(
      'POST',
      Uri.parse('$converseBase/v1/converse/transcribe'),
    );
    req.headers['Authorization'] = 'Bearer $_token';
    req.fields['content_scope'] = contentScope.wireValue;
    req.files.add(
      http.MultipartFile.fromBytes('file', bytes, filename: filename),
    );
    final streamed = await _http.send(req);
    final body = await streamed.stream.bytesToString();
    if (streamed.statusCode >= 300) {
      throw ApiException(streamed.statusCode, _detail(body));
    }
    return (jsonDecode(body) as Map).cast<String, Object?>();
  }

  Future<Map<String, Object?>> transcribeWindow(Map<String, Object?> window) =>
      _post(converseBase, '/v1/converse/audio/windows', window);

  Stream<Map<String, Object?>> speech({
    required String eventId,
    required int generationEpoch,
  }) async* {
    final req = http.Request(
      'POST',
      Uri.parse('$converseBase/v1/converse/speech'),
    );
    req.headers.addAll(_auth);
    req.body = jsonEncode({
      'event_id': eventId,
      'generation_epoch': generationEpoch,
    });
    final streamed = await _http.send(req);
    if (streamed.statusCode >= 300) {
      final body = await streamed.stream.bytesToString();
      throw ApiException(streamed.statusCode, _detail(body));
    }
    await for (final line
        in streamed.stream
            .transform(utf8.decoder)
            .transform(const LineSplitter())) {
      final t = line.trim();
      if (t.isEmpty) continue;
      yield (jsonDecode(t) as Map).cast<String, Object?>();
    }
  }

  // -- Agent --------------------------------------------------------------

  Future<AgentTask> createTask({
    required String sessionId,
    required String threadId,
    required int epoch,
    required String objective,
    required String idempotencyKey,
    ContentScope contentScope = ContentScope.localOnly,
  }) async {
    final json = await _post(agentBase, '/v1/agent/tasks', <String, Object?>{
      'schema_version': schemaVersion,
      'session_id': sessionId,
      'thread_id': threadId,
      'epoch': epoch,
      'objective': objective,
      'content_scope': contentScope.wireValue,
      'idempotency_key': idempotencyKey,
    });
    return AgentTask.fromJson(json);
  }

  Future<List<AgentTask>> listTasks() async {
    final uri = Uri.parse('$agentBase/v1/agent/tasks');
    final res = await _http.get(uri, headers: _auth);
    if (res.statusCode >= 300) {
      throw ApiException(res.statusCode, _detail(res.body));
    }
    final v = jsonDecode(res.body);
    if (v is List) {
      return v
          .map((e) => AgentTask.fromJson((e as Map).cast<String, Object?>()))
          .toList();
    }
    return const [];
  }

  Future<AgentTask> getTask(String id) async {
    return AgentTask.fromJson(await _get(agentBase, '/v1/agent/tasks/$id'));
  }

  Future<AgentTask> steerTask({
    required String taskId,
    required String instruction,
    ContentScope contentScope = ContentScope.localOnly,
    required int expectedObjectiveRevision,
    required String idempotencyKey,
  }) async {
    final json = await _post(
      agentBase,
      '/v1/agent/tasks/$taskId/steer',
      <String, Object?>{
        'schema_version': schemaVersion,
        'instruction': instruction,
        'content_scope': contentScope.wireValue,
        'expected_objective_revision': expectedObjectiveRevision,
        'idempotency_key': idempotencyKey,
        'source_event_ids': <String>[],
      },
    );
    return AgentTask.fromJson(json);
  }

  Future<AgentTask> interruptTask({
    required String taskId,
    required int expectedObjectiveRevision,
    required String idempotencyKey,
    String reason = 'User requested cancellation',
  }) async {
    final json = await _post(
      agentBase,
      '/v1/agent/tasks/$taskId/interrupt',
      <String, Object?>{
        'schema_version': schemaVersion,
        'expected_objective_revision': expectedObjectiveRevision,
        'idempotency_key': idempotencyKey,
        'reason': reason,
      },
    );
    return AgentTask.fromJson(json);
  }

  Future<void> resolveApproval({
    required String approvalId,
    required bool approved,
    required String actionDigest,
    required int objectiveRevision,
  }) async {
    await _post(
      agentBase,
      '/v1/approvals/$approvalId/resolve',
      <String, Object?>{
        'schema_version': schemaVersion,
        'approved': approved,
        'action_digest': actionDigest,
        'objective_revision': objectiveRevision,
      },
    );
  }

  Future<Map<String, Object?>> agentCapabilities() =>
      _get(agentBase, '/v1/agents/capabilities');

  Future<Map<String, Object?>> agentHealth() =>
      _get(agentBase, '/health', auth: false);

  Future<Map<String, Object?>> resourceStatus() =>
      _get(agentBase, '/v1/resources');

  Future<Map<String, Object?>> mcpManagement() =>
      _get(agentBase, '/v1/mcp/management');

  Future<Map<String, Object?>> controlMcp(Map<String, Object?> body) => _post(
    agentBase,
    '/v1/mcp/control',
    body,
  ).timeout(const Duration(seconds: 140));

  // -- Agent-only command registry (Lead-owned; absence is honest) ---------

  /// Returns null when the endpoint is unavailable; never fakes execution.
  Future<List<CommandDef>?> fetchCommands() async {
    for (final base in <String>[agentBase, sessionBase]) {
      try {
        final json = await _get(base, '/v1/commands');
        final list = json['commands'];
        if (list is List) {
          return list
              .map(
                (e) => CommandDef.fromJson((e as Map).cast<String, Object?>()),
              )
              .toList();
        }
        return const [];
      } on ApiException catch (e) {
        if (e.status == 404) continue;
        rethrow;
      }
    }
    return null;
  }

  Future<Map<String, Object?>?> executeCommand({
    required String sessionId,
    required String threadId,
    required int epoch,
    required String input,
    ContentScope contentScope = ContentScope.localOnly,
    required String idempotencyKey,
  }) async {
    for (final base in <String>[agentBase, sessionBase]) {
      try {
        return await _post(base, '/v1/commands/execute', <String, Object?>{
          'schema_version': schemaVersion,
          'session_id': sessionId,
          'thread_id': threadId,
          'epoch': epoch,
          'input': input,
          'content_scope': contentScope.wireValue,
          'idempotency_key': idempotencyKey,
        });
      } on ApiException catch (e) {
        if (e.status == 404) continue;
        rethrow;
      }
    }
    return null;
  }

  void close() => _http.close();
}
