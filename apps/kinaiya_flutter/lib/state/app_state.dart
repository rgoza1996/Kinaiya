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
import 'converse_handoff.dart';

enum AppView { character, agent }

class ChatMsg {
  ChatMsg({
    required this.role,
    required this.text,
    this.eventId,
    this.grounded,
    this.note,
    this.utteranceId,
    this.windowIndex = -1,
    this.windowRevision = 0,
  });
  final String role; // 'user' | 'kinaiya' | 'system'
  final String text;
  final String? eventId;
  final bool? grounded;
  final String? note;
  final String? utteranceId;
  final int windowIndex;
  final int windowRevision;
}

class AppState extends ChangeNotifier {
  KinaiyaClient Function(ConnectionProfile profile)? clientFactory;

  ConnectionProfile? connection;
  KinaiyaClient? client;
  String? connectionError;
  bool connected = false;
  bool agentOnline = false;
  bool converseOnline = false;

  /// Owner session authentication, distinct from inference availability.
  /// True once listSessions succeeds for the live generation; deterministic
  /// owner controls (status/stop/help/permissions/approval/recovery) use
  /// this, not inference consent.
  bool sessionAuthenticated = false;

  /// Per-role positive verification plus disclosure consent. Each role fails
  /// closed on missing/malformed/unavailable/unconsented provider metadata
  /// while the other authorized role stays useful.
  bool agentInferenceAllowed = false;
  bool converseInferenceAllowed = false;

  /// Last verified provider metadata per role (generation-bound). Null when
  /// the role has no verified metadata for the live connection.
  Map<String, Object?>? agentProvider;
  Map<String, Object?>? converseProvider;
  bool streamingWindows = false;
  bool _trustedInjection = false;
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
  final _handoffs = ConverseHandoffTracker();
  final Map<String, int> _handoffDecisionCursors = {};
  int _handoffRecoveryOffset = 0;
  final _noticeEvents = StreamController<JournalEntry>.broadcast();
  Stream<JournalEntry> get outcomeNotices => _noticeEvents.stream;
  final _windowEvents = StreamController<JournalEntry>.broadcast();
  Stream<JournalEntry> get windowEvents => _windowEvents.stream;
  final _resourceStops = StreamController<int>.broadcast();
  Stream<int> get resourceStops => _resourceStops.stream;
  Timer? _resourcesPoll;
  bool _pollingResources = false;
  bool? resourcesConfigured;
  bool? resourceConverseDesired;
  bool resourceStatusStale = false;
  String resourcePhase = '';
  String? _resourceController;
  int _resourceRevision = -1;
  int? _resourceOwnerRevision;

  AppView view = AppView.character;

  SessionInfo? converseSession;
  SessionInfo? agentSession;

  final List<ChatMsg> converseHistory = [];
  final List<ChatMsg> agentHistory = [];
  String _converseDraft = '';
  String _agentDraft = '';
  ContentScope _converseDraftScope = ContentScope.localOnly;
  ContentScope _agentDraftScope = ContentScope.localOnly;
  String get converseDraft => _converseDraft;
  String get agentDraft => _agentDraft;

  // Restored/externally assigned drafts have no disclosure provenance.
  set converseDraft(String value) {
    _converseDraft = value;
    _converseDraftScope = ContentScope.localOnly;
  }

  set agentDraft(String value) {
    _agentDraft = value;
    _agentDraftScope = ContentScope.localOnly;
  }

  int converseCursor = 0;
  String? converseHistorySeededFor;
  int agentCursor = 0;

  bool converseEnabled = true;
  bool? converseOwnerEnabled;
  int? _converseOwnerRevision, _healthOwnerRevision;
  bool? _lastHealthEnabled;
  bool converseOwnerStatusStale = true;
  bool converseControlPending = false;
  bool? converseControlTarget;
  bool converseControlUncertain = false;
  int _converseControlRequest = 0;
  String? converseControlError;

  /// Intent is separate from resource availability. An outage is never sleep.
  bool get converseSleeping =>
      sessionAuthenticated &&
      client != null &&
      !converseOwnerStatusStale &&
      converseOwnerEnabled == false &&
      !converseControlPending &&
      !converseControlUncertain;

  bool get converseOwnerControlAvailable =>
      sessionAuthenticated &&
      client != null &&
      !converseOwnerStatusStale &&
      converseOwnerEnabled != null &&
      !converseControlPending &&
      !converseControlUncertain;
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

  /// Kept task rows remain inspectable after an outage; they are not fresh
  /// evidence for current character activity until the next successful read.
  bool tasksStatusStale = true;
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
      _converseDraftScope = value.trim().isEmpty
          ? ContentScope.localOnly
          : _converseDraft.trim().isEmpty
          ? selectedContentScope
          : _converseDraftScope.combine(selectedContentScope);
      _converseDraft = value;
    } else {
      _agentDraftScope = value.trim().isEmpty
          ? ContentScope.localOnly
          : _agentDraft.trim().isEmpty
          ? selectedContentScope
          : _agentDraftScope.combine(selectedContentScope);
      _agentDraft = value;
    }
    notifyListeners();
  }

  ContentScope get selectedContentScope =>
      connection?.backendProfile.contentScope ?? ContentScope.localOnly;

  ContentScope draftScope(AppView which) =>
      which == AppView.character ? _converseDraftScope : _agentDraftScope;

  static bool providerAllowsScope(Object? info, ContentScope scope) {
    if (info is! Map || info['local'] is! bool) return false;
    final provider = info['provider'];
    final model = info['model'];
    if (provider is! String ||
        provider.trim().isEmpty ||
        model is! String ||
        model.trim().isEmpty) {
      return false;
    }
    if (info['local'] == true) return true;
    if (provider == 'meta' || model.toLowerCase().contains('contributor')) {
      return scope == ContentScope.publicSynthetic;
    }
    return scope != ContentScope.localOnly;
  }

  bool agentCanDisclose(ContentScope scope) =>
      agentCanInfer &&
      ((_trustedInjection && agentProvider == null) ||
          providerAllowsScope(agentProvider, scope));
  bool converseCanDisclose(ContentScope scope) =>
      converseCanInfer &&
      ((_trustedInjection && converseProvider == null) ||
          providerAllowsScope(converseProvider, scope));

  /// Compares service endpoints and the owner credential in memory only.
  /// Never logs, serializes or exposes the token; equality only.
  bool _sameBackend(ConnectionProfile a, ConnectionProfile b) =>
      a.sessionUrl == b.sessionUrl &&
      a.agentUrl == b.agentUrl &&
      a.converseUrl == b.converseUrl &&
      a.ownerToken == b.ownerToken;

  /// Positive per-role provider verification plus disclosure consent.
  /// Local metadata (local == true with nonempty provider/model) allows
  /// local inference under any profile. Cloud metadata (local == false)
  /// requires a matching disclosure profile, or metaContributor with exactly
  /// provider `meta` and model `muse-spark-1.3-contributor` (never
  /// substring matches). Private-cloud consent excludes Contributor targets;
  /// the pinned input scope is checked separately. Clean permits only local. Missing, malformed,
  /// or unconsented metadata fails closed.
  static bool providerAllows(Object? info, BackendProfile consent) {
    if (info is! Map) return false;
    final local = info['local'];
    final provider = info['provider'];
    final model = info['model'];
    if (local is! bool) return false;
    if (provider is! String || provider.trim().isEmpty) return false;
    if (model is! String || model.trim().isEmpty) return false;
    if (local) return true;
    switch (consent) {
      case BackendProfile.clean:
        return false;
      case BackendProfile.cloudDevelopment:
        return true;
      case BackendProfile.metaContributor:
        return provider == 'meta' && model == 'muse-spark-1.3-contributor';
      case BackendProfile.privateCloudAuthorized:
        return provider != 'meta' &&
            !model.toLowerCase().contains('contributor');
    }
  }

  /// Authenticated inference gates. Deterministic owner controls use
  /// [sessionAuthenticated] alone; inference submissions require the
  /// per-role flag as well.
  bool get agentCanInfer => sessionAuthenticated && agentInferenceAllowed;
  bool get converseCanInfer => sessionAuthenticated && converseInferenceAllowed;

  void connect(ConnectionProfile profile, {KinaiyaClient? injected}) {
    final previous = connection;
    final backendChanged = previous != null && !_sameBackend(previous, profile);
    tasksStatusStale = true;
    client?.close();
    _noticesPoll?.cancel();
    _resourcesPoll?.cancel();
    resourcesConfigured = null;
    resourceConverseDesired = null;
    resourceStatusStale = false;
    resourcePhase = '';
    _resourceController = null;
    _resourceRevision = -1;
    _resourceOwnerRevision = null;
    _resetConverseOwner();
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
      _handoffs.clear();
      _handoffDecisionCursors.clear();
      _handoffRecoveryOffset = 0;
      agentNotice = '';
    }
    final generation = ++_connectionGeneration;
    connection = profile;
    _trustedInjection = injected != null;
    client = injected ?? clientFactory?.call(profile);
    // Per-role consent resets on every (re)connect until verified.
    sessionAuthenticated = false;
    agentInferenceAllowed = false;
    converseInferenceAllowed = false;
    agentProvider = null;
    converseProvider = null;
    streamingWindows = false;
    agentOnline = false;
    converseOnline = false;
    connected = false;
    if (injected != null) {
      // Explicitly trusted test seam (see existing tests): injected
      // transports bypass live health verification.
      sessionAuthenticated = true;
      agentInferenceAllowed = true;
      converseInferenceAllowed = true;
      agentOnline = true;
      converseOnline = true;
      connected = true;
    }
    connectionError = client == null
        ? 'Connection is not configured yet.'
        : injected != null
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
      if (generation != _connectionGeneration || !identical(api, client)) {
        return;
      }
      sessionAuthenticated = true;
      // Per-role health plus provider metadata, still generation-bound.
      // Each role fails closed independently; a healthy consented role
      // stays useful when the other role is missing, malformed,
      // unavailable or unconsented.
      Object? agentInfo;
      Object? converseInfo;
      final states = await Future.wait([
        api
            .agentCapabilities()
            .then((_) async {
              final health = await api.agentHealth();
              agentInfo = health['provider'];
              return true;
            })
            .timeout(const Duration(seconds: 5))
            .catchError((Object _) => false),
        api
            .converseHealth()
            .then((v) {
              if (generation == _connectionGeneration &&
                  identical(api, client)) {
                applyConverseHealth(v);
                streamingWindows = v['streaming_windows'] == true;
              }
              converseInfo = v['provider'];
              return true;
            })
            .timeout(const Duration(seconds: 5))
            .catchError((Object _) => false),
      ]);
      if (generation != _connectionGeneration || !identical(api, client)) {
        return;
      }
      agentOnline = states[0];
      converseOnline = states[1];
      agentProvider = agentInfo is Map<String, Object?>
          ? agentInfo as Map<String, Object?>
          : agentInfo is Map
          ? (agentInfo as Map).cast<String, Object?>()
          : null;
      converseProvider = converseInfo is Map<String, Object?>
          ? converseInfo as Map<String, Object?>
          : converseInfo is Map
          ? (converseInfo as Map).cast<String, Object?>()
          : null;
      final consent = connection!.backendProfile;
      agentInferenceAllowed = agentOnline && providerAllows(agentInfo, consent);
      converseInferenceAllowed =
          converseOnline && providerAllows(converseInfo, consent);
      connected =
          sessionAuthenticated &&
          (agentInferenceAllowed || converseInferenceAllowed);
      if (!connected) {
        if (agentOnline || converseOnline) {
          // At least one service answered but no role earned consent:
          // disclosure, malformed, or missing provider metadata.
          connectionError =
              'Cloud inference is configured. Choose matching disclosure consent in Connection setup before sending text or audio.';
        } else {
          connectionError =
              'Last check (${DateTime.now().toUtc().toIso8601String()}): session authenticated. Agent unavailable; Converse unavailable.';
        }
      } else {
        final agentLabel = !agentOnline
            ? 'unavailable'
            : agentInferenceAllowed
            ? 'ready'
            : 'blocked (check disclosure consent)';
        final converseLabel = !converseOnline
            ? 'unavailable'
            : converseInferenceAllowed
            ? (converseEnabled ? 'ready' : 'off')
            : 'blocked (check disclosure consent)';
        connectionError =
            'Last check (${DateTime.now().toUtc().toIso8601String()}): session authenticated. Agent $agentLabel; Converse $converseLabel.';
      }
      _noticesPoll?.cancel();
      _resourcesPoll?.cancel();
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
      sessionAuthenticated = false;
      agentInferenceAllowed = false;
      converseInferenceAllowed = false;
      agentOnline = false;
      converseOnline = false;
      agentProvider = null;
      converseProvider = null;
      streamingWindows = false;
      connected = false;
      connectionError =
          'Session authentication or connection failed. Check setup.';
    }
    notifyListeners();
  }

  /// Revalidates Agent inference after a deterministic `/model` profile
  /// switch. Clears the previous grant first so no further Agent input is
  /// accepted until the new provider metadata is positively verified.
  /// Generation-bound: a reconnect during the check drops the stale result.
  void invalidateAgentInference() {
    agentInferenceAllowed = false;
    agentProvider = null;
    connected =
        sessionAuthenticated &&
        (agentInferenceAllowed || converseInferenceAllowed);
    notifyListeners();
  }

  Future<void> revalidateAgentInference() async {
    final api = client;
    final profile = connection;
    final generation = _connectionGeneration;
    if (api == null || profile == null) return;
    invalidateAgentInference();
    try {
      await api.agentCapabilities().timeout(const Duration(seconds: 5));
      final health = await api.agentHealth().timeout(
        const Duration(seconds: 5),
      );
      if (!ownsConnection(generation, api)) return;
      agentOnline = true;
      final info = health['provider'];
      agentProvider = info is Map<String, Object?>
          ? info
          : info is Map
          ? info.cast<String, Object?>()
          : null;
      agentInferenceAllowed = providerAllows(info, profile.backendProfile);
      connected =
          sessionAuthenticated &&
          (agentInferenceAllowed || converseInferenceAllowed);
      final agentLabel = agentInferenceAllowed
          ? 'ready'
          : 'blocked (check disclosure consent)';
      final converseLabel = !converseOnline
          ? 'unavailable'
          : converseInferenceAllowed
          ? (converseEnabled ? 'ready' : 'off')
          : 'blocked (check disclosure consent)';
      connectionError =
          'Last check (${DateTime.now().toUtc().toIso8601String()}): session authenticated. Agent $agentLabel; Converse $converseLabel.';
    } catch (_) {
      if (!ownsConnection(generation, api)) return;
      agentOnline = false;
      agentInferenceAllowed = false;
      agentProvider = null;
      connected =
          sessionAuthenticated &&
          (agentInferenceAllowed || converseInferenceAllowed);
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
      if (!ownsConnection(generation, api) ||
          converseSession?.sessionId != session.sessionId ||
          converseSession?.threadId != session.threadId ||
          converseSession?.epoch != session.epoch) {
        return;
      }
      for (final entry in entries) {
        if (entry.sessionId != session.sessionId ||
            entry.threadId != session.threadId ||
            entry.epoch != session.epoch) {
          continue;
        }
        _observeHandoff(entry, session);
        if (entry.sequence > _noticeCursor) _noticeCursor = entry.sequence;
        if (entry.epoch == session.epoch &&
            (entry.kind == 'conversation.window' ||
                entry.kind == 'routing.window_decided' ||
                entry.kind == 'routing.window_status' ||
                entry.kind == 'converse.window_state' ||
                entry.kind == 'delivery.updated' ||
                entry.payload['window_source_id'] != null)) {
          if (!_seenNotices.add(entry.eventId)) continue;
          if (_seenNotices.length > 512) {
            _seenNotices.remove(_seenNotices.first);
          }
          if (entry.kind == 'conversation.window') {
            final id = entry.payload['utterance_id'] as String?;
            final index = converseHistory.indexWhere(
              (m) => m.utteranceId == id,
            );
            final message = ChatMsg(
              role: 'user',
              text: entry.text,
              eventId: entry.eventId,
              utteranceId: id,
              windowIndex:
                  (entry.payload['window_index'] as num?)?.toInt() ?? 0,
              windowRevision: (entry.payload['revision'] as num?)?.toInt() ?? 1,
              note: 'Live transcript',
            );
            if (index < 0) {
              converseHistory.add(message);
            } else if (message.windowIndex >
                    converseHistory[index].windowIndex ||
                (message.windowIndex == converseHistory[index].windowIndex &&
                    message.windowRevision >
                        converseHistory[index].windowRevision)) {
              converseHistory[index] = message;
            }
          } else if (entry.kind == 'conversation.converse' &&
              !converseHistory.any((m) => m.eventId == entry.eventId)) {
            converseHistory.add(
              ChatMsg(
                role: 'kinaiya',
                text: entry.text,
                eventId: entry.eventId,
                grounded: false,
              ),
            );
          }
          _windowEvents.add(entry);
          continue;
        }
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
      await _recoverHandoffs(api, session, generation);
      if (!ownsConnection(generation, api)) return;
      notifyListeners();
    } catch (_) {
      // Durable cursor stays put on transport errors; next poll retries.
    } finally {
      _pollingNotices = false;
    }
  }

  void _applyHandoff(HandoffNotice notice) {
    final id = 'handoff:${notice.sourceEventId}';
    final index = converseHistory.indexWhere(
      (message) => message.eventId == id,
    );
    final message = ChatMsg(
      role: 'system',
      text: notice.text,
      note: 'Agent handoff',
      eventId: id,
    );
    if (index < 0) {
      converseHistory.add(message);
    } else {
      converseHistory[index] = message;
    }
  }

  void _observeHandoff(JournalEntry entry, SessionInfo session) {
    final notice = _handoffs.observe(entry, session);
    if (notice != null) _applyHandoff(notice);
    for (final recovered in _handoffs.drainPending(session)) {
      _applyHandoff(recovered);
    }
  }

  /// Resolve native causal dependencies only. Never replay historical audio,
  /// dialogue or Agent work. Each poll spends at most eight authenticated GETs;
  /// typed decision cursors survive short/byte-capped pages and transient failure.
  Future<void> _recoverHandoffs(
    KinaiyaClient api,
    SessionInfo session,
    int generation,
  ) async {
    bool current() =>
        ownsConnection(generation, api) &&
        converseSession?.sessionId == session.sessionId &&
        converseSession?.threadId == session.threadId &&
        converseSession?.epoch == session.epoch;
    final pending = _handoffs.pendingReceipts;
    final pendingIds = pending.map((entry) => entry.eventId).toSet();
    _handoffDecisionCursors.removeWhere((id, _) => !pendingIds.contains(id));
    if (pending.isEmpty) return;
    var budget = 8;
    final start = _handoffRecoveryOffset++ % pending.length;
    for (var n = 0; n < pending.length && budget >= 3; n++) {
      if (!current()) return;
      final receipt = pending[(start + n) % pending.length];
      if (receipt.causes.length != 1) {
        _handoffs.discardPending(receipt.eventId);
        continue;
      }
      final sourceId = receipt.payload['source_event_id'];
      if (sourceId is! String) continue;
      try {
        budget--;
        final source = await api
            .handoffEntry(sourceId, resolve: true)
            .timeout(const Duration(seconds: 3));
        if (!current()) return;
        if (source.eventId != sourceId ||
            source.sessionId != session.sessionId ||
            source.threadId != session.threadId ||
            source.epoch != session.epoch ||
            source.origin != 'authenticated-user' ||
            !const {
              'conversation.user',
              'conversation.window',
            }.contains(source.kind) ||
            source.sequence != receipt.payload['source_sequence'] ||
            (source.payload['revision'] ?? 1) !=
                receipt.payload['source_revision']) {
          _handoffs.discardPending(receipt.eventId);
          continue;
        }
        budget--;
        final cause = await api
            .handoffEntry(receipt.causes.single)
            .timeout(const Duration(seconds: 3));
        if (!current()) return;
        if (cause.eventId != receipt.causes.single ||
            cause.sessionId != session.sessionId ||
            cause.threadId != session.threadId ||
            cause.epoch != session.epoch ||
            cause.sequence >= receipt.sequence) {
          _handoffs.discardPending(receipt.eventId);
          continue;
        }

        if (source.kind == 'conversation.window') {
          if (!((receipt.kind == 'routing.window_status' &&
                  cause.eventId == sourceId &&
                  cause.kind == 'conversation.window') ||
              (receipt.kind == 'routing.window_dispatch' &&
                  cause.kind == 'routing.window_decided' &&
                  cause.origin == 'decisions'))) {
            _handoffs.discardPending(receipt.eventId);
            continue;
          }
          final utterance = source.payload['utterance_id'];
          if (utterance is! String) continue;
          budget--;
          final latest = await api
              .handoffLatestWindow(
                sessionId: session.sessionId,
                epoch: session.epoch,
                utteranceId: utterance,
              )
              .timeout(const Duration(seconds: 3));
          if (!current()) return;
          if (latest == null ||
              latest.sessionId != session.sessionId ||
              latest.threadId != session.threadId ||
              latest.epoch != session.epoch ||
              latest.payload['utterance_id'] != utterance ||
              latest.kind != 'conversation.window' ||
              latest.origin != 'authenticated-user' ||
              latest.sequence < source.sequence ||
              latest.payload['window_index'] is! int ||
              (latest.payload['window_index'] as int) < 0 ||
              latest.payload['revision'] is! int ||
              (latest.payload['revision'] as int) < 1 ||
              latest.payload['text'] is! String) {
            continue;
          }
          _handoffs.observe(source, session);
          _handoffs.observe(cause, session);
          _handoffs.observe(latest, session);
          final notice = _handoffs.observe(receipt, session);
          if (notice != null) _applyHandoff(notice);
          continue;
        }
        if (source.kind != 'conversation.user' ||
            !((cause.kind == 'conversation.user' &&
                    cause.eventId == sourceId &&
                    cause.origin == 'authenticated-user' &&
                    receipt.payload['source_revision'] == 1) ||
                (cause.kind == 'conversation.revision' &&
                    const {
                      'authenticated-user',
                      'converse',
                    }.contains(cause.origin) &&
                    cause.payload['target_event_id'] == sourceId &&
                    cause.payload['revision'] == source.payload['revision']))) {
          _handoffs.discardPending(receipt.eventId);
          continue;
        }
        _handoffs.observe(source, session);
        _handoffs.observe(cause, session);
        var cursor = _handoffDecisionCursors[receipt.eventId] ?? cause.sequence;
        while (budget > 0 && current()) {
          budget--;
          final page = await api
              .handoffDecisions(
                sessionId: session.sessionId,
                threadId: session.threadId,
                after: cursor,
              )
              .timeout(const Duration(seconds: 3));
          if (!current()) return;
          if (page.isEmpty) {
            _handoffs.discardPending(receipt.eventId);
            break;
          }
          var sequence = cursor;
          for (final entry in page) {
            if (entry.sequence <= sequence ||
                entry.sessionId != session.sessionId ||
                entry.threadId != session.threadId ||
                entry.epoch != session.epoch ||
                entry.kind != 'routing.decided' ||
                entry.origin != 'decisions') {
              throw const FormatException('Invalid native decision page.');
            }
            sequence = entry.sequence;
          }
          for (final entry in page) {
            if (entry.sequence >= receipt.sequence) break;
            _handoffs.observe(entry, session);
          }
          cursor = sequence;
          _handoffDecisionCursors[receipt.eventId] = cursor;
          final notice = _handoffs.observe(receipt, session);
          if (notice != null) _applyHandoff(notice);
          if (!_handoffs.pendingReceipts.any(
            (entry) => entry.eventId == receipt.eventId,
          )) {
            break;
          }
          if (cursor >= receipt.sequence) {
            _handoffs.discardPending(receipt.eventId);
            break;
          }
        }
      } catch (_) {
        if (!current()) return;
        // Native lookup failure leaves the pending receipt and cursor for a
        // later bounded read. No acknowledgment or task is invented.
      }
    }
  }

  void _resetConverseOwner() {
    converseOwnerEnabled = null;
    _converseOwnerRevision = _healthOwnerRevision = null;
    _lastHealthEnabled = null;
    converseOwnerStatusStale = true;
    converseControlPending = converseControlUncertain = false;
    converseControlTarget = null;
    converseControlError = null;
  }

  bool _acceptConverseOwner(bool enabled, int revision) {
    final previous = _converseOwnerRevision;
    if (revision < 0 ||
        revision > 9007199254740991 ||
        (previous != null &&
            (revision < previous ||
                (revision == previous && converseOwnerEnabled != enabled)))) {
      return false;
    }
    _converseOwnerRevision = revision;
    converseOwnerEnabled = enabled;
    converseOwnerStatusStale = false;
    if (converseControlUncertain && enabled == converseControlTarget) {
      converseControlUncertain = false;
      converseControlError = null;
      if (!converseControlPending) converseControlTarget = null;
    }
    return true;
  }

  /// Apply only a health read already bound to the live client/generation.
  /// Legacy effective-only health cannot establish intentional sleep.
  void applyConverseHealth(Map<String, Object?> health) {
    final observedRevision = health['owner_intent_revision'];
    if (observedRevision is int &&
        _converseOwnerRevision != null &&
        observedRevision < _converseOwnerRevision!) {
      return;
    }
    final wasEnabled = converseEnabled;
    converseOwnerStatusStale = true;
    Never invalid(String message) {
      _lastHealthEnabled = null;
      if (wasEnabled) _stopUnavailableConverse();
      converseEnabled = false;
      throw FormatException(message);
    }

    if (health['enabled'] is! bool) {
      invalid('Invalid Converse availability');
    }
    if (health.containsKey('owner_enabled') ||
        health.containsKey('owner_intent_revision')) {
      final owner = health['owner_enabled'];
      final revision = health['owner_intent_revision'];
      if (owner is! bool ||
          revision is! int ||
          health['resource_enabled'] is! bool ||
          health['enabled'] != (owner && health['resource_enabled'] == true) ||
          !_acceptConverseOwner(owner, revision)) {
        invalid('Invalid Converse owner intent');
      }
      _healthOwnerRevision = revision;
    }
    _lastHealthEnabled = health['enabled'] == true;
    _applyEffectiveConverseHealth();
    if (wasEnabled && !converseEnabled) {
      _stopUnavailableConverse();
    }
  }

  void _stopUnavailableConverse() {
    converseEnabled = false;
    turnNonce++;
    voiceNonce++;
    speechEpoch++;
    busyConverse = false;
    _resourceStops.add(_converseOwnerRevision ?? -1);
  }

  void _applyEffectiveConverseHealth() {
    converseEnabled =
        _lastHealthEnabled == true &&
        converseOwnerEnabled != false &&
        !converseControlPending &&
        !converseControlUncertain &&
        !resourceStatusStale &&
        (resourcesConfigured != true || resourcePhase == 'ready');
  }

  /// Read after an uncertain write; this never repeats the mutation.
  Future<bool> refreshConverseAvailability() async {
    final api = client;
    final generation = _connectionGeneration;
    if (api == null || !sessionAuthenticated) return false;
    try {
      final health = await api.converseHealth().timeout(
        const Duration(seconds: 5),
      );
      if (!ownsConnection(generation, api)) return false;
      applyConverseHealth(health);
      // An opposite snapshot cannot prove a timed-out request will not apply
      // later. Resolve only when its requested choice is actually observed.
      if (converseControlUncertain) {
        if (converseOwnerStatusStale ||
            converseOwnerEnabled != converseControlTarget) {
          notifyListeners();
          return false;
        }
        converseControlUncertain = false;
        applyConverseHealth(health);
        if (!converseControlPending) converseControlTarget = null;
      }
      converseControlError = null;
      notifyListeners();
      return !converseOwnerStatusStale;
    } catch (_) {
      if (!ownsConnection(generation, api)) return false;
      converseOwnerStatusStale = true;
      _lastHealthEnabled = null;
      if (converseEnabled) _stopUnavailableConverse();
      notifyListeners();
      return false;
    }
  }

  void _completeConverseControl(Map<String, Object?> result, bool enabled) {
    final revision = result['owner_intent_revision'];
    if (result['owner_enabled'] != enabled ||
        revision is! int ||
        revision < 0 ||
        revision > 9007199254740991 ||
        result['resource_enabled'] is! bool ||
        result['enabled'] != (enabled && result['resource_enabled'] == true)) {
      throw const FormatException('Owner choice was not confirmed');
    }
    final uncertain = converseControlUncertain;
    converseControlUncertain = false;
    try {
      if (revision < (_converseOwnerRevision ?? -1)) {
        // A newer observed owner action wins. The original HTTP operation has
        // now completed, so a fresh read may reconcile effective availability.
        unawaited(refreshConverseAvailability());
      } else {
        applyConverseHealth(result);
      }
    } catch (_) {
      converseControlUncertain = uncertain;
      rethrow;
    }
    converseControlError = null;
  }

  bool _converseControlRejected(Object error) =>
      error is ApiException &&
      const [400, 401, 403, 404, 405, 422].contains(error.status);

  /// Owner choice only: no Agent mutation, provider switch or microphone arm.
  /// beforeSleep invalidates the existing local audio owners synchronously.
  Future<void> setConverseEnabled(
    bool enabled, {
    VoidCallback? beforeSleep,
  }) async {
    final api = client;
    final generation = _connectionGeneration;
    if (api == null || !converseOwnerControlAvailable) return;
    converseControlPending = true;
    converseControlTarget = enabled;
    final request = ++_converseControlRequest;
    converseControlError = null;
    if (!enabled) {
      converseEnabled = false;
      turnNonce++;
      voiceNonce++;
      speechEpoch++;
      busyConverse = false;
      beforeSleep?.call();
    }
    notifyListeners();
    var timedOut = false;
    try {
      final response = api.setConverseEnabled(enabled);
      // Future.timeout does not recall an HTTP request. Keep its completion
      // attributable to this operation rather than allowing a blind retry.
      unawaited(
        response.then(
          (result) {
            if (!timedOut ||
                !ownsConnection(generation, api) ||
                request != _converseControlRequest) {
              return;
            }
            try {
              _completeConverseControl(result, enabled);
              converseControlTarget = null;
              notifyListeners();
            } catch (_) {
              /* The original outcome remains unconfirmed. */
            }
          },
          onError: (Object error) {
            if (!timedOut ||
                !ownsConnection(generation, api) ||
                request != _converseControlRequest ||
                !_converseControlRejected(error)) {
              return;
            }
            converseControlUncertain = false;
            converseControlTarget = null;
            unawaited(refreshConverseAvailability());
            notifyListeners();
          },
        ),
      );
      final result = await response.timeout(const Duration(seconds: 8));
      if (!ownsConnection(generation, api)) return;
      _completeConverseControl(result, enabled);
    } catch (error) {
      if (!ownsConnection(generation, api)) return;
      timedOut = error is TimeoutException;
      converseOwnerStatusStale = true;
      // These explicit rejections precede native owner mutation. Transport
      // failures and 503 may follow persistence and remain uncertain.
      final rejected = _converseControlRejected(error);
      converseControlUncertain = !rejected;
      converseControlError =
          '${enabled ? 'Wake' : 'Sleep'} is unconfirmed. Check host status before trying again.';
      final confirmed = await refreshConverseAvailability();
      if (!ownsConnection(generation, api)) return;
      if (!confirmed || converseOwnerEnabled != enabled) {
        converseControlError = rejected && confirmed
            ? '${enabled ? 'Wake' : 'Sleep'} was not applied. Host state was checked.'
            : '${enabled ? 'Wake' : 'Sleep'} is unconfirmed. Check host status before trying again.';
      }
    } finally {
      if (ownsConnection(generation, api)) {
        converseControlPending = false;
        if (!converseControlUncertain) converseControlTarget = null;
        _applyEffectiveConverseHealth();
        notifyListeners();
      }
    }
  }

  Future<void> refreshResources({bool force = false}) async {
    final api = client;
    if (api == null || _pollingResources) {
      return;
    }
    if (resourcesConfigured == false && !force) {
      await refreshConverseAvailability();
      return;
    }
    _pollingResources = true;
    final generation = _connectionGeneration;
    try {
      final status = await api.resourceStatus().timeout(
        const Duration(seconds: 4),
      );
      if (!ownsConnection(generation, api)) return;
      if (status['configured'] == false) {
        resourcesConfigured = false;
        resourceConverseDesired = null;
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
      final ownerRevision = policy['owner_intent_revision'];
      if (ownerRevision != null &&
          (ownerRevision is! int ||
              ownerRevision < 0 ||
              ownerRevision > 9007199254740991)) {
        throw const FormatException('Invalid Converse owner revision');
      }
      final controllerChanged = _resourceController != controller;
      if (controllerChanged) {
        _resourceController = controller;
        _resourceRevision = -1;
        _resourceOwnerRevision = null;
      }
      if (revision < _resourceRevision) {
        throw const FormatException('Obsolete resource status');
      }
      if (_resourceOwnerRevision != null &&
          (ownerRevision is! int || ownerRevision < _resourceOwnerRevision!)) {
        throw const FormatException('Obsolete Converse owner intent');
      }
      // Owner intent can change immediately while a restoration still holds
      // the controller lock. Its revision is independent of worker progress.
      if (revision == _resourceRevision &&
          ownerRevision == _resourceOwnerRevision &&
          !wasStale &&
          !converseOwnerStatusStale) {
        await _refreshRestoredConverse();
        return;
      }
      _resourceRevision = revision;
      _resourceOwnerRevision = ownerRevision is int ? ownerRevision : null;
      if (ownerRevision is int && sessionAuthenticated) {
        if (controllerChanged && _healthOwnerRevision == null) {
          _converseOwnerRevision = null;
        }
        if (!_acceptConverseOwner(
          policy['desired_converse'] as bool,
          ownerRevision,
        )) {
          throw const FormatException('Obsolete Converse owner intent');
        }
      }
      resourcePhase = policy['phase'] as String? ?? 'failed';
      resourceConverseDesired = policy['desired_converse'] as bool;
      converseEnabled =
          resourcePhase == 'ready' &&
          policy['desired_converse'] == true &&
          converseOwnerEnabled != false &&
          _lastHealthEnabled != false &&
          !converseControlPending &&
          !converseControlUncertain;
      if (!converseEnabled) {
        turnNonce++;
        voiceNonce++;
        speechEpoch++;
        busyConverse = false;
        _resourceStops.add(revision);
      }
      notifyListeners();
      await _refreshRestoredConverse();
    } catch (_) {
      if (!ownsConnection(generation, api)) return;
      if (!resourceStatusStale && resourcesConfigured == true) {
        turnNonce++;
        voiceNonce++;
        speechEpoch++;
        busyConverse = false;
        converseEnabled = false;
        _resourceStops.add(-1);
      }
      resourceStatusStale = true;
      if (resourcesConfigured == true) converseOwnerStatusStale = true;
      notifyListeners();
    } finally {
      _pollingResources = false;
    }
  }

  Future<void> _refreshRestoredConverse() async {
    if (resourcePhase == 'ready' &&
        resourceConverseDesired == true &&
        converseOwnerEnabled != false &&
        _lastHealthEnabled == false &&
        !converseControlPending &&
        !converseControlUncertain) {
      // Resource readiness is not a receipt that Converse reopened its gate.
      // Recheck actual availability, including on unchanged ready snapshots.
      await refreshConverseAvailability();
    }
  }

  void disconnect() {
    _connectionGeneration++;
    _noticesPoll?.cancel();
    _resourcesPoll?.cancel();
    client?.close();
    client = null;
    connected = false;
    tasksStatusStale = true;
    resourceConverseDesired = null;
    _resetConverseOwner();
    sessionAuthenticated = false;
    agentInferenceAllowed = false;
    converseInferenceAllowed = false;
    agentOnline = false;
    converseOnline = false;
    agentProvider = null;
    converseProvider = null;
    streamingWindows = false;
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
    _windowEvents.close();
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

  /// Retain the source identity supplied by native typed-turn acceptance.
  /// Acceptance of conversation input itself is not Agent acceptance.
  void acceptedConverseSource(
    SessionInfo session,
    Object? eventId,
    int userIndex,
  ) {
    if (eventId is! String ||
        converseSession?.sessionId != session.sessionId ||
        converseSession?.threadId != session.threadId ||
        converseSession?.epoch != session.epoch) {
      return;
    }
    if (!_handoffs.acceptedSource(session, eventId)) return;
    if (userIndex >= 0 && userIndex < converseHistory.length) {
      final old = converseHistory[userIndex];
      if (old.role == 'user') {
        converseHistory[userIndex] = ChatMsg(
          role: old.role,
          text: old.text,
          eventId: eventId,
          note: old.note,
          grounded: old.grounded,
          utteranceId: old.utteranceId,
          windowIndex: old.windowIndex,
          windowRevision: old.windowRevision,
        );
      }
    }
    notifyListeners();
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
    tasksStatusStale = false;
    notifyListeners();
  }

  void markTasksUnavailable() {
    if (tasksStatusStale) return;
    tasksStatusStale = true;
    notifyListeners();
  }

  /// Work presentation uses only native task lifecycle observations. Objective
  /// prose and completed tool receipts cannot establish a current specific tool.
  String get characterWorkActivity {
    if (!sessionAuthenticated || tasksStatusStale) return 'idle';
    if (tasks.any(
      (task) => const {
        'blocked',
        'awaiting_approval',
        'paused',
        'cancel_requested',
      }.contains(task.state),
    )) {
      return 'blocked';
    }
    if (tasks.any(
      (task) => const {
        'accepted',
        'queued',
        'running',
        'verifying',
      }.contains(task.state),
    )) {
      return 'working.generic';
    }
    return 'idle';
  }

  void setCommands(List<CommandDef>? next) {
    commands = next;
    commandsAvailable = next != null;
    notifyListeners();
  }
}
