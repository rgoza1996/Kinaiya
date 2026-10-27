/// Shared application state: connection, sessions, drafts, history.
///
/// Drafts, sessions and history survive view changes and reconnects. The
/// owner Bearer [REDACTED] stays in memory only. Audio staleness is guarded by a
/// local turn nonce plus the server generation epoch; Agent work is never
/// cancelled by speech interruption.
library;

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
    connection = profile;
    client = injected ?? clientFactory?.call(profile);
    connected = client != null;
    connectionError = connected ? null : 'Connection is not configured yet.';
    notifyListeners();
  }

  void disconnect() {
    client?.close();
    client = null;
    connected = false;
    converseSession = null;
    agentSession = null;
    // Drafts and history are preserved across reconnect by design.
    notifyListeners();
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
