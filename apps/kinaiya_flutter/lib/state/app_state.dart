/// Shared application state: connection, sessions, drafts, history.
///
/// Drafts and history survive view changes and same-backend reconnects
/// (same endpoints and owner credential, compared in memory only). A
/// different backend starts isolated: no sessions, tasks, approvals,
/// drafts or history are reused. The command registry is connection-scoped
/// and invalidated on every (re)connect until reloaded. The
/// owner Bearer [REDACTED] stays in memory only. Audio staleness is guarded by a
/// local turn nonce plus the server generation epoch; Agent work is never
/// cancelled by speech interruption.
library;

import 'dart:async';
import 'package:flutter/foundation.dart';

import '../api/client.dart';
import '../api/models.dart';
import '../config/connection.dart';

enum AppView { character, agent }

class ChatMsg {
  ChatMsg({
    required this.role,
    required this.text,
    this.eventId,
    this.grounded,
    this.note,
  });
  final String role; // 'user' | 'kinaiya' | 'system'
  final String text;
  final String? eventId;
  final bool? grounded;
  final String? note;
}

class AppState extends ChangeNotifier {
  KinaiyaClient Function(ConnectionProfile profile)? clientFactory;

  ConnectionProfile? connection;
  KinaiyaClient? client;
  String? connectionError;
  bool connected = false;
  bool agentOnline = false;
  bool converseOnline = false;
  int _connectionGeneration = 0;

  /// Live connection epoch. Views capture this before an await and drop
  /// late completions when it changes (reconnect/disconnect/dispose).
  int get connectionGeneration => _connectionGeneration;

  /// True while [generation]/[api] still describe the live connection.
  /// Late read completions must not replace current state when false.
  /// Already-sent requests cannot be recalled; their late cleanup must
  /// stay attributable to this original generation instead.
  bool ownsConnection(int generation, KinaiyaClient? api) =>
      generation == _connectionGeneration && identical(api, client);

  Timer? _noticesPoll;
  bool _pollingNotices = false;
  int _noticeCursor = 0;
  String? _noticeSession;
  final Set<String> _seenNotices = {};
  final _noticeEvents = StreamController<JournalEntry>.broadcast();
  Stream<JournalEntry> get outcomeNotices => _noticeEvents.stream;
  final _resourceStops = StreamController<int>.broadcast();
  Stream<int> get resourceStops => _resourceStops.stream;
  Timer? _resourcesPoll;
  bool _pollingResources = false;
  bool? resourcesConfigured;
  bool resourceStatusStale = false;
  String resourcePhase = '';
  String? _resourceController;
  int _resourceRevision = -1;

  AppView view = AppView.character;

  SessionInfo? converseSession;
  SessionInfo? agentSession;

  final List<ChatMsg> converseHistory = [];
  final List<ChatMsg> agentHistory = [];
  String converseDraft = '';
  String agentDraft = '';
  int converseCursor = 0;
  String? converseHistorySeededFor;
  int agentCursor = 0;

  bool converseEnabled = true;
  bool busyConverse = false;
  bool busyAgent = false;

  /// Local turn nonce; audio from older nonces is dropped on arrival.
  int turnNonce = 0;
  int speechEpoch = 0;

  /// Separate voice-session generation for user-armed listening clips.
  /// Voice turns never reuse or clobber text-turn nonces.
  int voiceNonce = 0;
  int voiceEpoch = 0;

  List<AgentTask> tasks = [];
  List<ApprovalRequest> approvals = [];
  List<CommandDef>? commands; // null: registry unavailable (honest).
  bool commandsAvailable = false;

  String agentNotice = '';

  /// External refresh trigger (views must not call notifyListeners directly).
  void touch() => notifyListeners();

  void setView(AppView next) {
    view = next;
    notifyListeners();
  }

  void setDraft(AppView which, String value) {
    if (which == AppView.character) {
      converseDraft = value;
    } else {
      agentDraft = value;
    }
    notifyListeners();
  }

  /// Compares service endpoints and the owner credential in memory only.
  /// Never logs, serializes or exposes the token; equality only.
  bool _sameBackend(ConnectionProfile a, ConnectionProfile b) =>
      a.sessionUrl == b.sessionUrl &&
      a.agentUrl == b.agentUrl &&
      a.converseUrl == b.converseUrl &&
      a.ownerToken == b.ownerToken;

  void connect(ConnectionProfile profile, {KinaiyaClient? injected}) {
    final previous = connection;
    final backendChanged = previous != null && !_sameBackend(previous, profile);
    client?.close();
    _noticesPoll?.cancel();
    _resourcesPoll?.cancel();
    resourcesConfigured = null;
    resourceStatusStale = false;
    resourcePhase = '';
    _resourceController = null;
    _resourceRevision = -1;
    // The registry is honest only for its own connection: invalidate on
    // every (re)connect until the new registry loads.
    commands = null;
    commandsAvailable = false;
    // A new connection starts idle: already-sent Agent/Converse requests
    // cannot be recalled, but their late completions stay attributable to
    // the old generation (see ownsConnection) and must not own this one.
    // Task polling re-discovers backend work; the busy flags do not travel.
    busyAgent = false;
    busyConverse = false;
    turnNonce++;
    voiceNonce++;
    speechEpoch = 0;
    voiceEpoch = 0;
    if (backendChanged) {
      // A different backend must not reuse another backend's sessions,
      // tasks, approvals, drafts or history. Same-backend reconnect keeps
      // drafts/history by design (see disconnect).
      converseSession = null;
      agentSession = null;
      tasks = [];
      approvals = [];
      converseHistory.clear();
      agentHistory.clear();
      converseDraft = '';
      agentDraft = '';
      converseCursor = 0;
      agentCursor = 0;
      converseHistorySeededFor = null;
      _noticeSession = null;
      _noticeCursor = 0;
      _seenNotices.clear();
      agentNotice = '';
    }
    final generation = ++_connectionGeneration;
    connection = profile;
    client = injected ?? clientFactory?.call(profile);
    connected = injected != null;
    agentOnline = converseOnline = connected;
    connectionError = client == null
        ? 'Connection is not configured yet.'
        : connected
        ? null
        : 'Checking services…';
    notifyListeners();
    if (client != null && injected == null) {
      unawaited(_verifyConnection(generation));
    }
  }

  /// Reloads the Agent command registry for the current connection.
  /// Bounded read-only retries (no side effects, never executes).
  /// Stale completions from an old generation/client are dropped.
  Future<void> refreshCommands({int maxAttempts = 3}) async {
    final api = client;
    final generation = _connectionGeneration;
    if (api == null) return;
    final attempts = maxAttempts.clamp(1, 5);
    for (var attempt = 0; attempt < attempts; attempt++) {
      if (!ownsConnection(generation, api)) return;
      try {
        final next = await api.fetchCommands().timeout(
          const Duration(seconds: 3),
        );
        if (!ownsConnection(generation, api)) return;
        commands = next;
        commandsAvailable = next != null;
        notifyListeners();
        return;
      } catch (_) {
        if (!ownsConnection(generation, api)) return;
        if (attempt + 1 >= attempts) {
          commands = null;
          commandsAvailable = false;
          notifyListeners();
          return;
        }
        await Future<void>.delayed(Duration(milliseconds: 50 * (attempt + 1)));
      }
    }
  }

  Future<void> _verifyConnection(int generation) async {
    final api = client;
    if (api == null) return;
    try {
      await api.listSessions().timeout(const Duration(seconds: 10));
      final clouds = <Map>[];
      final states = await Future.wait([
        api
            .agentCapabilities()
            .then((_) async {
              final health = await api.agentHealth();
              final info = health['provider'];
              if (info is Map && info['local'] == false) clouds.add(info);
              return true;
            })
            .timeout(const Duration(seconds: 5))
            .catchError((Object _) => false),
        api
            .converseHealth()
            .then((v) {
              if (generation == _connectionGeneration) {
                converseEnabled = v['enabled'] == true;
              }
              final info = v['provider'];
              if (info is Map && info['local'] == false) clouds.add(info);
              return true;
            })
            .timeout(const Duration(seconds: 5))
            .catchError((Object _) => false),
      ]);
      if (generation != _connectionGeneration) return;
      agentOnline = states[0];
      converseOnline = states[1];
      final consent = connection!.backendProfile;
      final cloudAllowed =
          clouds.isEmpty ||
          consent == BackendProfile.cloudDevelopment ||
          (consent == BackendProfile.metaContributor &&
              clouds.every(
                (v) =>
                    v['provider'] == 'meta' &&
                    '${v['model']}'.contains('contributor'),
              ));
      connected = (agentOnline || converseOnline) && cloudAllowed;
      if (!cloudAllowed) {
        connectionError =
            'Cloud inference is configured. Choose matching disclosure consent in Connection setup before sending text or audio.';
        notifyListeners();
        return;
      }
      connectionError =
          'Last check (${DateTime.now().toUtc().toIso8601String()}): session authenticated. Agent ${agentOnline ? 'ready' : 'unavailable'}; Converse ${converseOnline ? (converseEnabled ? 'ready' : 'off') : 'unavailable'}.';
      _noticesPoll = Timer.periodic(
        const Duration(seconds: 1),
        (_) => refreshNotices(),
      );
      _resourcesPoll = Timer.periodic(
        const Duration(seconds: 2),
        (_) => refreshResources(),
      );
      unawaited(refreshResources());
    } catch (_) {
      if (generation != _connectionGeneration) return;
      connected = false;
      connectionError =
          'Session authentication or connection failed. Check setup.';
    }
    notifyListeners();
  }

  Future<void> refreshNotices() async {
    final api = client, session = converseSession;
    if (api == null || session == null || _pollingNotices) return;
    _pollingNotices = true;
    final generation = _connectionGeneration;
    try {
      if (_noticeSession != session.sessionId) {
        _noticeSession = session.sessionId;
        _noticeCursor = 0;
      }
      final entries = await api
          .transcript(
            sessionId: session.sessionId,
            after: _noticeCursor,
            limit: 100,
          )
          .timeout(const Duration(seconds: 5));
      if (generation != _connectionGeneration ||
          converseSession?.sessionId != session.sessionId) {
        return;
      }
      for (final entry in entries) {
        if (entry.sequence > _noticeCursor) _noticeCursor = entry.sequence;
        if (entry.epoch != session.epoch ||
            entry.origin != 'converse' ||
            entry.kind != 'conversation.converse' ||
            entry.payload['outcome_event_id'] == null ||
            !_seenNotices.add(entry.eventId)) {
          continue;
        }
        if (_seenNotices.length > 512) _seenNotices.remove(_seenNotices.first);
        if (!converseHistory.any((m) => m.eventId == entry.eventId)) {
          converseHistory.add(
            ChatMsg(
              role: 'kinaiya',
              text: entry.text,
              eventId: entry.eventId,
              grounded: true,
              note: 'Verified Agent result',
            ),
          );
        }
        _noticeEvents.add(entry);
      }
      notifyListeners();
    } catch (_) {
      // Durable cursor stays put on transport errors; next poll retries.
    } finally {
      _pollingNotices = false;
    }
  }

  Future<void> refreshResources({bool force = false}) async {
    final api = client;
    if (api == null ||
        _pollingResources ||
        (resourcesConfigured == false && !force)) {
      return;
    }
    _pollingResources = true;
    final generation = _connectionGeneration;
    try {
      final status = await api.resourceStatus().timeout(
        const Duration(seconds: 4),
      );
      if (generation != _connectionGeneration) return;
      if (status['configured'] == false) {
        resourcesConfigured = false;
        resourceStatusStale = false;
        notifyListeners();
        return;
      }
      final policy = status['policy'];
      if (policy is! Map ||
          policy['revision'] is! int ||
          policy['desired_converse'] is! bool ||
          policy['phase'] is! String ||
          status['controller_id'] is! String) {
        throw const FormatException('Invalid resource status');
      }
      resourcesConfigured = true;
      final wasStale = resourceStatusStale;
      resourceStatusStale = false;
      final controller = status['controller_id'] as String;
      final revision = (policy['revision'] as num).toInt();
      if (_resourceController != controller) {
        _resourceController = controller;
        _resourceRevision = -1;
      }
      if (revision < _resourceRevision) {
        throw const FormatException('Obsolete resource status');
      }
      if (revision == _resourceRevision && !wasStale) return;
      _resourceRevision = revision;
      resourcePhase = policy['phase'] as String? ?? 'failed';
      converseEnabled =
          resourcePhase == 'ready' && policy['desired_converse'] == true;
      if (!converseEnabled) {
        turnNonce++;
        voiceNonce++;
        speechEpoch++;
        busyConverse = false;
        _resourceStops.add(revision);
      }
      notifyListeners();
    } catch (_) {
      if (generation != _connectionGeneration) return;
      if (!resourceStatusStale && resourcesConfigured == true) {
        turnNonce++;
        voiceNonce++;
        speechEpoch++;
        busyConverse = false;
        converseEnabled = false;
        _resourceStops.add(-1);
      }
      resourceStatusStale = true;
      notifyListeners();
    } finally {
      _pollingResources = false;
    }
  }

  void disconnect() {
    _connectionGeneration++;
    _noticesPoll?.cancel();
    _resourcesPoll?.cancel();
    client?.close();
    client = null;
    connected = false;
    turnNonce++;
    voiceNonce++;
    speechEpoch = 0;
    voiceEpoch = 0;
    converseSession = null;
    agentSession = null;
    // Registry is connection-scoped: a disconnected view must report it
    // unavailable instead of showing the previous backend's commands.
    commands = null;
    commandsAvailable = false;
    // Drafts and history are preserved across same-backend reconnect by
    // design. Cross-backend isolation is applied in connect(), which can
    // compare endpoints and credentials; disconnect alone keeps them so a
    // same-backend reconnect restores them.
    notifyListeners();
  }

  @override
  void dispose() {
    _connectionGeneration++;
    _noticesPoll?.cancel();
    _resourcesPoll?.cancel();
    client?.close();
    _noticeEvents.close();
    _resourceStops.close();
    super.dispose();
  }

  /// Reuses an existing named session for [channel] when the backend lists
  /// one, otherwise creates it. Explicit IDs persist across same-backend
  /// reconnect. A stale acquisition (old generation/client) never mutates
  /// the new connection: it throws instead of installing a foreign session.
  /// Already-sent session requests cannot be recalled.
  Future<SessionInfo> ensureSession(String channel, {String name = ''}) async {
    final api = client;
    final generation = _connectionGeneration;
    if (api == null) throw StateError('Not connected');
    final listed = await api.listSessions();
    if (!ownsConnection(generation, api)) {
      throw StateError('Connection changed during session acquisition');
    }
    SessionInfo? match;
    for (final s in listed) {
      if (s.channel == channel && (name.isEmpty || s.name == name)) {
        match = s;
        break;
      }
    }
    // Recheck before the side-effecting create: a stale list must not
    // create a session on the old client into the new connection.
    if (!ownsConnection(generation, api)) {
      throw StateError('Connection changed during session acquisition');
    }
    final session = match ?? await api.createSession(channel, name: name);
    if (!ownsConnection(generation, api)) {
      throw StateError('Connection changed during session acquisition');
    }
    if (channel == 'converse') {
      if (converseSession?.sessionId != session.sessionId) {
        try {
          final seed = await api.recent(sessionId: session.sessionId, limit: 1);
          if (!ownsConnection(generation, api)) {
            throw StateError('Connection changed during session acquisition');
          }
          _noticeSession = session.sessionId;
          _noticeCursor = seed.isEmpty ? 0 : seed.last.sequence;
        } catch (_) {
          if (!ownsConnection(generation, api)) rethrow;
          /* A later cursor poll can retry. */
        }
      }
      if (!ownsConnection(generation, api)) {
        throw StateError('Connection changed during session acquisition');
      }
      converseSession = session;
    } else {
      if (!ownsConnection(generation, api)) {
        throw StateError('Connection changed during session acquisition');
      }
      agentSession = session;
    }
    notifyListeners();
    return session;
  }

  void appendHistory(AppView which, ChatMsg msg) {
    (which == AppView.character ? converseHistory : agentHistory).add(msg);
    notifyListeners();
  }

  /// Drops late audio: true when this chunk belongs to the live turn.
  bool audioIsLive(int nonce, int generationEpoch) =>
      nonce == turnNonce && generationEpoch == speechEpoch;

  void beginTurn(int generationEpoch) {
    turnNonce++;
    speechEpoch = generationEpoch;
  }

  /// Starts a new armed-listening voice clip with its own generation.
  /// Returns the voice nonce owning this clip's cleanup.
  int beginVoiceTurn(int generationEpoch) {
    voiceNonce++;
    voiceEpoch = generationEpoch;
    return voiceNonce;
  }

  /// Drops stale voice callbacks: true only for the live voice turn.
  bool voiceIsLive(int nonce, int generationEpoch) =>
      nonce == voiceNonce && generationEpoch == voiceEpoch;

  void recordApprovals(List<ApprovalRequest> next) {
    approvals = next;
    notifyListeners();
  }

  void removeApproval(String id) {
    approvals.removeWhere((a) => a.approvalId == id);
    notifyListeners();
  }

  void setTasks(List<AgentTask> next) {
    tasks = next;
    notifyListeners();
  }

  void setCommands(List<CommandDef>? next) {
    commands = next;
    commandsAvailable = next != null;
    notifyListeners();
  }
}
