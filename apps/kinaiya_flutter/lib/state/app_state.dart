/// Shared application state: connection, sessions, drafts, history.
///
/// Drafts, sessions and history survive view changes and reconnects. The
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
  Timer? _noticesPoll;
  bool _pollingNotices = false;
  int _noticeCursor = 0;
  String? _noticeSession;
  final Set<String> _seenNotices = {};
  final _noticeEvents = StreamController<JournalEntry>.broadcast();
  Stream<JournalEntry> get outcomeNotices => _noticeEvents.stream;

  AppView view = AppView.character;

  SessionInfo? converseSession;
  SessionInfo? agentSession;

  final List<ChatMsg> converseHistory = [];
  final List<ChatMsg> agentHistory = [];
  String converseDraft = '';
  String agentDraft = '';
  int converseCursor = 0;
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

  void connect(ConnectionProfile profile, {KinaiyaClient? injected}) {
    client?.close();
    _noticesPoll?.cancel();
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
          'Session authenticated. Agent ${agentOnline ? 'ready' : 'unavailable'}; Converse ${converseOnline ? (converseEnabled ? 'ready' : 'off') : 'unavailable'}.';
      _noticesPoll = Timer.periodic(
        const Duration(seconds: 1),
        (_) => refreshNotices(),
      );
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

  void disconnect() {
    _connectionGeneration++;
    _noticesPoll?.cancel();
    client?.close();
    client = null;
    connected = false;
    converseSession = null;
    agentSession = null;
    // Drafts and history are preserved across reconnect by design.
    notifyListeners();
  }

  @override
  void dispose() {
    _noticesPoll?.cancel();
    client?.close();
    _noticeEvents.close();
    super.dispose();
  }

  /// Reuses an existing named session for [channel] when the backend lists
  /// one, otherwise creates it. Explicit IDs persist across reconnect.
  Future<SessionInfo> ensureSession(String channel, {String name = ''}) async {
    final api = client;
    if (api == null) throw StateError('Not connected');
    final existing = await api.listSessions();
    SessionInfo? match;
    for (final s in existing) {
      if (s.channel == channel && (name.isEmpty || s.name == name)) {
        match = s;
        break;
      }
    }
    final session = match ?? await api.createSession(channel, name: name);
    if (channel == 'converse') {
      if (converseSession?.sessionId != session.sessionId) {
        try {
          final existing = await api.recent(
            sessionId: session.sessionId,
            limit: 1,
          );
          _noticeSession = session.sessionId;
          _noticeCursor = existing.isEmpty ? 0 : existing.last.sequence;
        } catch (_) {
          /* A later cursor poll can retry. */
        }
      }
      converseSession = session;
    } else {
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
