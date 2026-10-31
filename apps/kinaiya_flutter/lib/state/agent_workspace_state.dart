part of 'app_state.dart';

String _agentScope(SessionInfo session) =>
    '${session.sessionId}/${session.threadId}/${session.epoch}';

class _AgentWorkspaceSnapshot {
  _AgentWorkspaceSnapshot(this.draft, this.scope, this.history, this.cursor);
  final String draft;
  final ContentScope scope;
  final List<ChatMsg> history;
  final int cursor;
}

class _AgentWorkspaceData {
  List<AgentHarness> harnesses = const [];
  List<SessionInfo> sessions = const [];
  final snapshots = <String, _AgentWorkspaceSnapshot>{};
  String? harness,
      nextCursor,
      catalogCursor,
      catalogError,
      sessionError,
      historyError;
  bool catalogLoading = false, sessionsLoading = false, historyLoading = false;
  bool creating = false, creationUncertain = false;
  bool creationRefreshed = false;
  String? uncertainHarness;
  bool configured = false;
  Future<void>? catalogRefresh;
  bool identitySelecting = false;
  bool historyMore = false;
  int catalogRead = 0, sessionRead = 0, selection = 0, historyRead = 0;
  SessionInfo? lastSession;
}

/// Read/selection state is independent of model admission and global approvals.
extension AgentWorkspaceState on AppState {
  List<AgentHarness> get agentHarnesses => _agentWorkspace.harnesses;
  List<SessionInfo> get agentWorkspaceSessions => _agentWorkspace.sessions;
  String? get selectedAgentHarness => _agentWorkspace.harness;
  bool get agentHarnessesLoading => _agentWorkspace.catalogLoading;
  bool get agentSessionsLoading => _agentWorkspace.sessionsLoading;
  bool get agentHistoryLoading => _agentWorkspace.historyLoading;
  bool get agentIdentitySelecting => _agentWorkspace.identitySelecting;
  bool get agentSessionCreating => _agentWorkspace.creating;
  bool get agentCreationUncertain => _agentWorkspace.creationUncertain;
  bool get canAcknowledgeAgentCreation =>
      _agentWorkspace.creationRefreshed &&
      !_agentWorkspace.sessionsLoading &&
      _agentWorkspace.sessionError == null;
  bool get agentSessionsHaveMore => _agentWorkspace.nextCursor != null;
  bool get agentHistoryHasMore => _agentWorkspace.historyMore;
  String? get agentHarnessError => _agentWorkspace.catalogError;
  String? get agentSessionsError => _agentWorkspace.sessionError;
  String? get agentHistoryError => _agentWorkspace.historyError;
  int get agentSelectionGeneration => _agentWorkspace.selection;

  AgentHarness? get selectedAgentHarnessInfo {
    for (final entry in agentHarnesses) {
      if (entry.versionId == selectedAgentHarness) return entry;
    }
    return null;
  }

  bool get canCreateAgentSession =>
      sessionAuthenticated &&
      client != null &&
      selectedAgentHarnessInfo?.selectable == true &&
      !agentSessionCreating &&
      !agentCreationUncertain &&
      !agentHarnessesLoading;

  List<AgentTask> get selectedAgentTasks {
    final session = agentSession;
    if (session == null) return const [];
    return tasks
        .where(
          (task) =>
              task.sessionId == session.sessionId &&
              task.threadId == session.threadId &&
              task.epoch == session.epoch,
        )
        .toList();
  }

  bool ownsAgentSelection(int selection, SessionInfo? session) =>
      selection == agentSelectionGeneration &&
      (session == null
          ? agentSession == null
          : agentSession != null &&
                _agentScope(session) == _agentScope(agentSession!));

  void _saveAgentWorkspace() {
    final session = agentSession;
    if (session == null) return;
    final data = _agentWorkspace;
    data.lastSession = session;
    final key = _agentScope(session);
    data.snapshots.remove(key);
    data.snapshots[key] = _AgentWorkspaceSnapshot(
      _agentDraft,
      _agentDraftScope,
      List.of(agentHistory),
      agentCursor,
    );
    // Session snapshots are RAM-only and bounded; the journal stays authoritative.
    if (data.snapshots.length > 100) {
      data.snapshots.remove(data.snapshots.keys.first);
    }
  }

  void _invalidateAgentWorkspace({bool clear = false}) {
    final data = _agentWorkspace;
    data.catalogRead++;
    data.catalogRefresh = null;
    data.sessionRead++;
    data.selection++;
    data.historyRead++;
    data.catalogLoading = data.sessionsLoading = data.historyLoading =
        data.creating = false;
    data.identitySelecting = false;
    data.harnesses = const [];
    data.sessions = const [];
    data.catalogCursor = data.nextCursor = null;
    data.catalogError = data.sessionError = data.historyError = null;
    if (clear) {
      data.harness = null;
      data.lastSession = null;
      data.creationUncertain = false;
      data.creationRefreshed = false;
      data.uncertainHarness = null;
      data.snapshots.clear();
    }
  }

  Future<void> refreshAgentHarnesses() {
    final api = client,
        generation = connectionGeneration,
        data = _agentWorkspace;
    if (api == null || !sessionAuthenticated) return Future<void>.value();
    final pending = data.catalogRefresh;
    if (pending != null) return pending;
    // Publish ownership before touch(): connection/auth listeners may reenter
    // synchronously. One refresh shares one bounded transport admission.
    final done = Completer<void>();
    data.catalogRefresh = done.future;
    _readAgentHarnesses(api, generation, data).then(
      (_) {
        if (identical(data.catalogRefresh, done.future)) {
          data.catalogRefresh = null;
        }
        done.complete();
      },
      onError: (Object error, StackTrace stack) {
        if (identical(data.catalogRefresh, done.future)) {
          data.catalogRefresh = null;
        }
        done.completeError(error, stack);
      },
    );
    return done.future;
  }

  Future<void> _readAgentHarnesses(
    KinaiyaClient api,
    int generation,
    _AgentWorkspaceData data,
  ) async {
    final read = ++data.catalogRead;
    bool current() =>
        ownsConnection(generation, api) && read == data.catalogRead;
    data.catalogLoading = true;
    data.catalogError = null;
    touch();
    try {
      final found = <AgentHarness>[];
      String? cursor;
      bool? configured;
      // Bound a refresh; explicit failure is preferable to a silently partial catalog.
      for (var page = 0; page < 20; page++) {
        final result = await api.agentHarnesses(after: cursor);
        if (!current()) return;
        if (configured != null && configured != result.configured) {
          throw const FormatException();
        }
        configured = result.configured;
        for (final item in result.items) {
          if (found.any((old) => old.versionId == item.versionId) ||
              (cursor != null && item.versionId.compareTo(cursor) <= 0)) {
            throw const FormatException();
          }
          found.add(item);
        }
        cursor = result.nextCursor;
        if (cursor == null) break;
        if (page == 19) throw const FormatException();
      }
      data.harnesses = List.unmodifiable(found);
      data.configured = configured ?? false;
      final previousHarness = data.harness;
      data.harness ??=
          agentSession?.harnessVersion ??
          data.lastSession?.harnessVersion ??
          (agentSession != null || data.lastSession != null
              ? 'unassociated'
              : found.where((x) => x.active).firstOrNull?.versionId ??
                    found.firstOrNull?.versionId);
      if (data.harness != previousHarness) data.selection++;
    } catch (_) {
      if (!current()) return;
      data.harnesses = const [];
      data.catalogError =
          'Harness catalog unavailable. Refresh to inspect this host.';
    } finally {
      if (current()) {
        data.catalogLoading = false;
        touch();
      }
    }
    if (current() && data.harness != null) {
      await refreshAgentSessions();
      final previous = data.lastSession;
      if (current() &&
          agentSession == null &&
          previous != null &&
          (previous.harnessVersion ?? 'unassociated') == data.harness) {
        await selectAgentSession(previous.sessionId);
      } else if (current() &&
          data.catalogError == null &&
          agentSession != null &&
          agentHistory.isEmpty &&
          agentCursor == 0) {
        await refreshAgentHistory();
      }
    }
  }

  Future<void> selectAgentHarness(String version) async {
    final data = _agentWorkspace;
    if (version != 'unassociated' &&
        !agentHarnesses.any((x) => x.versionId == version)) {
      return;
    }
    if (version != data.harness) {
      _saveAgentWorkspace();
      data.harness = version;
      data.selection++;
      data.historyRead++;
      data.historyLoading = false;
      data.identitySelecting = false;
      data.historyError = null;
      data.historyMore = false;
      agentSession = null;
      agentHistory.clear();
      agentCursor = 0;
      _agentDraft = '';
      _agentDraftScope = ContentScope.localOnly;
      touch();
    }
    await revalidateAgentInference();
    await refreshAgentSessions();
  }

  Future<void> refreshAgentSessions({bool loadMore = false}) async {
    final api = client,
        generation = connectionGeneration,
        data = _agentWorkspace;
    final harness = data.harness;
    if (api == null || !sessionAuthenticated || harness == null) return;
    if (loadMore && (data.sessionsLoading || data.nextCursor == null)) return;
    final cursor = loadMore ? data.nextCursor : null;
    final read = ++data.sessionRead;
    bool current() =>
        ownsConnection(generation, api) &&
        read == data.sessionRead &&
        data.harness == harness;
    data.sessionsLoading = true;
    data.sessionError = null;
    if (!loadMore) {
      data.sessions = const [];
      data.nextCursor = null;
    }
    touch();
    try {
      final page = await api.agentSessions(
        harnessVersion: harness,
        after: cursor,
      );
      if (!current()) return;
      final rows = loadMore
          ? List<SessionInfo>.of(data.sessions)
          : <SessionInfo>[];
      for (final session in page.items) {
        if ((session.harnessVersion ?? 'unassociated') != harness ||
            rows.any((x) => x.sessionId == session.sessionId)) {
          throw const FormatException();
        }
        rows.add(session);
      }
      if (cursor != null && cursor == page.nextCursor) {
        throw const FormatException();
      }
      data.sessions = List.unmodifiable(rows);
      data.nextCursor = page.nextCursor;
      if (data.creationUncertain &&
          !data.creating &&
          harness == data.uncertainHarness &&
          !loadMore) {
        data.creationRefreshed = true;
      }
    } catch (_) {
      if (current()) {
        data.sessionError =
            'Sessions unavailable. Refresh to try this read again.';
      }
    } finally {
      if (current()) {
        data.sessionsLoading = false;
        touch();
      }
    }
  }

  Future<void> selectAgentSession(String id) async {
    final api = client,
        generation = connectionGeneration,
        data = _agentWorkspace;
    if (api == null || !sessionAuthenticated) return;
    _saveAgentWorkspace();
    final selection = ++data.selection;
    data.historyRead++;
    data.historyLoading = true;
    data.identitySelecting = true;
    data.historyError = null;
    // Until authoritative identity arrives, the old destination cannot receive new work.
    agentSession = null;
    agentHistory.clear();
    agentCursor = 0;
    _agentDraft = '';
    _agentDraftScope = ContentScope.localOnly;
    final restoreRevision = ++_agentDraftRevision;
    touch();
    bool current() =>
        ownsConnection(generation, api) && data.selection == selection;
    try {
      final session = await api
          .agentWorkspaceSession(id)
          .timeout(const Duration(seconds: 10));
      if (!current()) return;
      if (session.sessionId != id ||
          session.channel != 'agent' ||
          session.threadId.isEmpty ||
          session.epoch < 1) {
        throw const FormatException();
      }
      agentSession = session;
      data.identitySelecting = false;
      data.lastSession = session;
      data.harness = session.harnessVersion ?? 'unassociated';
      final saved = data.snapshots[_agentScope(session)];
      if (saved != null) {
        if (_agentDraftRevision == restoreRevision) {
          _agentDraft = saved.draft;
          _agentDraftScope = saved.scope;
        }
        agentHistory.addAll(saved.history);
        agentCursor = saved.cursor;
      }
      touch();
      await revalidateAgentInference();
      if (!current()) return;
      await refreshAgentHistory();
    } catch (_) {
      if (current()) {
        data.historyError =
            'Session could not be verified. Select it again to retry.';
      }
    } finally {
      if (current()) {
        data.historyLoading = false;
        data.identitySelecting = false;
        touch();
      }
    }
  }

  Future<void> refreshAgentHistory({bool loadMore = false}) async {
    final api = client,
        generation = connectionGeneration,
        session = agentSession;
    final data = _agentWorkspace, selection = agentSelectionGeneration;
    if (api == null || session == null) return;
    final read = ++data.historyRead;
    bool current() =>
        ownsConnection(generation, api) &&
        ownsAgentSelection(selection, session) &&
        read == data.historyRead;
    data.historyLoading = true;
    data.historyError = null;
    touch();
    try {
      final entries = await api
          .agentWorkspaceHistory(
            sessionId: session.sessionId,
            after: agentCursor,
          )
          .timeout(const Duration(seconds: 10));
      if (!current()) return;
      var cursor = agentCursor;
      final messages = <ChatMsg>[];
      for (final entry in entries) {
        if (entry.sessionId != session.sessionId || entry.sequence <= cursor) {
          throw const FormatException();
        }
        cursor = entry.sequence;
        // Historical reset epochs are deliberately not projected into current chat.
        if (entry.threadId != session.threadId ||
            entry.epoch != session.epoch) {
          continue;
        }
        if (entry.origin != 'agent-supervisor') continue;
        if (entry.kind == 'agent.command' && entry.payload['op'] == 'submit') {
          final text = entry.payload['objective'];
          if (text is String) {
            messages.add(
              ChatMsg(role: 'user', text: text, eventId: entry.eventId),
            );
          }
        } else if (entry.kind == 'agent.outcome') {
          final summary = entry.payload['summary'];
          final error = entry.payload['error'];
          final state = entry.payload['state'];
          messages.add(
            ChatMsg(
              role: 'kinaiya',
              text: summary is String && summary.isNotEmpty
                  ? summary
                  : error is String
                  ? error
                  : 'Task state: $state',
              eventId: entry.eventId,
              note:
                  'Recorded Agent outcome; inspect task evidence for completion.',
            ),
          );
        }
      }
      // Revalidate the native scope after the journal await; reset races cannot publish old history.
      final checked = await api
          .agentWorkspaceSession(session.sessionId)
          .timeout(const Duration(seconds: 10));
      if (!current()) return;
      if (_agentScope(checked) != _agentScope(session) ||
          checked.channel != 'agent' ||
          checked.harnessVersion != session.harnessVersion) {
        throw const FormatException();
      }
      for (final msg in messages) {
        if (agentHistory.any((old) => old.eventId == msg.eventId)) continue;
        final optimistic = agentHistory.indexWhere(
          (old) =>
              old.eventId == null &&
              old.role == msg.role &&
              old.text == msg.text,
        );
        if (optimistic >= 0) {
          agentHistory[optimistic] = msg;
        } else {
          agentHistory.add(msg);
        }
      }
      agentCursor = cursor;
      data.historyMore = entries.length == 200;
      _saveAgentWorkspace();
    } catch (_) {
      if (current()) {
        data.historyError =
            'History unavailable or session changed. Select the session to verify it again.';
      }
    } finally {
      if (current()) {
        data.historyLoading = false;
        data.identitySelecting = false;
        touch();
      }
    }
  }

  Future<SessionInfo?> createSelectedAgentSession({
    String name = 'New session',
  }) async {
    if (!canCreateAgentSession) return null;
    final api = client!,
        generation = connectionGeneration,
        data = _agentWorkspace;
    final harness = data.harness!, selection = data.selection;
    // Admission is uncertain until the exact native receipt arrives. Reconnect
    // preserves this latch, since a timeout/retired callback cannot undo creation.
    data.creating = true;
    data.creationUncertain = true;
    data.creationRefreshed = false;
    data.uncertainHarness = harness;
    touch();
    try {
      final session = await api.createAgentSession(harness, name: name);
      if (!ownsConnection(generation, api)) return null;
      if (session.harnessVersion != harness) throw const FormatException();
      data.creationUncertain = false;
      if (data.selection != selection || data.harness != harness) return null;
      await refreshAgentSessions();
      if (ownsConnection(generation, api) && data.selection == selection) {
        // An unassigned composer draft belongs to the newly created scope;
        // drafts from an existing session remain with that existing session.
        if (agentSession == null) {
          data.snapshots[_agentScope(session)] = _AgentWorkspaceSnapshot(
            _agentDraft,
            _agentDraftScope,
            const [],
            0,
          );
        }
        await selectAgentSession(session.sessionId);
        if (ownsConnection(generation, api) &&
            agentSession != null &&
            _agentScope(agentSession!) == _agentScope(session)) {
          return session;
        }
      }
    } on ApiException catch (error) {
      if (!ownsConnection(generation, api)) return null;
      data.creationUncertain = error.status >= 500;
      data.sessionError = data.creationUncertain
          ? 'Session creation unconfirmed. Refresh and inspect sessions before creating another.'
          : 'The host refused session creation. Refresh harness eligibility.';
    } catch (_) {
      if (!ownsConnection(generation, api)) return null;
      data.creationUncertain = true;
      data.sessionError =
          'Session creation unconfirmed. Refresh and inspect sessions before creating another.';
    } finally {
      if (ownsConnection(generation, api)) {
        data.creating = false;
        touch();
      }
    }
    return null;
  }

  void acknowledgeAgentCreationUncertainty() {
    if (!canAcknowledgeAgentCreation) return;
    _agentWorkspace.creationUncertain = false;
    touch();
  }
}
