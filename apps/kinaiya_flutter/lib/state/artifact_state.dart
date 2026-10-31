part of 'app_state.dart';

class _ArtifactIntent {
  _ArtifactIntent(
    this.key,
    this.value,
    this.artifact,
    this.component,
    this.binding,
  );
  final String key;
  final Object? value;
  final IntelligentArtifact artifact;
  final String component;
  final _ArtifactBinding binding;
  int get revision => artifact.revision;
}

/// Private RAM binding. Credentials are compared only in memory, never put in
/// URLs, logs, persisted fixtures, or a display label.
class _ArtifactBinding {
  _ArtifactBinding(
    this.profile,
    this.client,
    this.sessionId,
    this.threadId,
    this.epoch,
  );
  final ConnectionProfile? profile;
  final KinaiyaClient client;
  final String sessionId, threadId;
  final int epoch;

  bool matches(_ArtifactBinding other) {
    final a = profile, b = other.profile;
    final backend = a != null && b != null
        ? a.sessionUrl == b.sessionUrl &&
              a.agentUrl == b.agentUrl &&
              a.converseUrl == b.converseUrl &&
              a.ownerToken == b.ownerToken &&
              client.sessionBase == other.client.sessionBase &&
              client.agentBase == other.client.agentBase &&
              client.converseBase == other.client.converseBase
        : identical(client, other.client);
    return backend &&
        sessionId == other.sessionId &&
        threadId == other.threadId &&
        epoch == other.epoch;
  }
}

class _ArtifactData {
  final items = <String, IntelligentArtifact>{};
  final closed = <String>{};
  final refresh = <String>{};
  final deferred = <String, IntelligentArtifact>{};
  final intents = <String, _ArtifactIntent>{};
  // Keep unresolved original requests across reconnects, independently of the
  // current display. At capacity, refuse new writes rather than forget one.
  final retainedIntents = <String, _ArtifactIntent>{};
  String? selected, scope, error;
  bool pinned = false, editing = false, polling = false, actionPending = false;
  bool? supported;
  int after = 0, read = 0, highWater = 0, dismissedThrough = 0;
  DateTime? retryAfter;
}

/// Artifact data stays bound to the authenticated Converse owner. Reading or
/// selecting a result never starts inference, approves work, or starts audio.
extension IntelligentUiState on AppState {
  List<IntelligentArtifact> get converseArtifacts => List.unmodifiable(
    _artifacts.items.values.toList()
      ..sort((a, b) => b.createdAt.compareTo(a.createdAt)),
  );
  IntelligentArtifact? get activeArtifact =>
      _artifacts.items[_artifacts.selected];
  bool get artifactPinned => _artifacts.pinned;
  bool get artifactEditing => _artifacts.editing;
  bool get artifactActionPending => _artifacts.actionPending;
  bool get artifactActionUncertain =>
      _artifacts.intents.isNotEmpty && !_artifacts.actionPending;
  String? get artifactError => _artifacts.error;
  bool get artifactUpdateWaiting =>
      _artifacts.deferred.containsKey(_artifacts.selected);

  void _resetArtifacts() {
    final d = _artifacts;
    d.read++;
    d.items.clear();
    d.closed.clear();
    d.refresh.clear();
    d.deferred.clear();
    d.intents.clear();
    d.selected = d.scope = d.error = null;
    d.pinned = d.editing = d.polling = d.actionPending = false;
    d.supported = null;
    d.after = d.highWater = d.dismissedThrough = 0;
    d.retryAfter = null;
  }

  _ArtifactBinding _artifactBinding(KinaiyaClient api, SessionInfo session) =>
      _ArtifactBinding(
        connection,
        api,
        session.sessionId,
        session.threadId,
        session.epoch,
      );

  void _restoreArtifactIntents(KinaiyaClient api, SessionInfo session) {
    final d = _artifacts, binding = _artifactBinding(api, session);
    for (final intent in d.retainedIntents.values) {
      if (!intent.binding.matches(binding)) continue;
      final artifact = intent.artifact;
      d.intents['${artifact.artifactId}/${intent.component}'] = intent;
      d.items[artifact.artifactId] = artifact;
      d.selected = artifact.artifactId;
      d.highWater = artifact.taskSubmitSequence;
      d.error =
          'An earlier interaction is unconfirmed. Retry it to reconcile this view.';
    }
  }

  void _settleArtifactIntent(String key, _ArtifactIntent intent) {
    final d = _artifacts;
    if (identical(d.intents[key], intent)) d.intents.remove(key);
    if (identical(d.retainedIntents[intent.key], intent)) {
      d.retainedIntents.remove(intent.key);
    }
  }

  bool _artifactScope(
    SessionInfo session,
    int generation,
    KinaiyaClient api,
    int read,
  ) =>
      ownsConnection(generation, api) &&
      read == _artifacts.read &&
      converseSession?.sessionId == session.sessionId &&
      converseSession?.threadId == session.threadId &&
      converseSession?.epoch == session.epoch;

  void _observeArtifact(JournalEntry entry) {
    if (entry.origin == 'agent-supervisor' &&
        entry.kind == 'artifact.updated') {
      final id = entry.payload['artifact_id'];
      if (id is String &&
          id.isNotEmpty &&
          id.length <= 128 &&
          _artifacts.refresh.length < 32) {
        _artifacts.refresh.add(id);
      }
    }
  }

  void _adoptArtifact(IntelligentArtifact value, {bool action = false}) {
    final d = _artifacts;
    final prior = d.items[value.artifactId];
    if (prior != null &&
        (value.revision < prior.revision ||
            (value.revision == prior.revision &&
                value.viewRevision < prior.viewRevision))) {
      return;
    }
    if (!action &&
        (d.editing || d.intents.isNotEmpty) &&
        d.selected == value.artifactId &&
        prior != null &&
        (value.revision > prior.revision ||
            value.viewRevision > prior.viewRevision)) {
      d.deferred[value.artifactId] = value;
      return;
    }
    d.items[value.artifactId] = value;
    if (value.taskSubmitSequence > d.highWater) {
      d.highWater = value.taskSubmitSequence;
    }
    final current = d.items[d.selected];
    // Never let a late older task replace a newer task's stage. A recovered
    // artifact without native submit ordering remains available for manual open.
    if (!d.pinned &&
        !d.editing &&
        !d.actionPending &&
        d.intents.isEmpty &&
        !d.closed.contains(value.artifactId) &&
        value.taskSubmitSequence > d.dismissedThrough &&
        value.taskSubmitSequence >= d.highWater &&
        (current == null ||
            value.taskSubmitSequence > current.taskSubmitSequence ||
            (value.taskId == current.taskId &&
                value.createdAt >= current.createdAt))) {
      d.selected = value.artifactId;
    }
    while (d.items.length > 60) {
      final candidate = d.items.keys.firstWhere(
        (id) =>
            id != d.selected &&
            !d.intents.keys.any((key) => key.startsWith('$id/')),
        orElse: () => '',
      );
      if (candidate.isEmpty) break;
      d.items.remove(candidate);
      d.closed.remove(candidate);
      d.deferred.remove(candidate);
    }
  }

  Future<void> refreshArtifacts() async {
    final api = client, session = converseSession;
    final d = _artifacts;
    if (!sessionAuthenticated || api == null || session == null) return;
    final scope = _agentScope(session);
    if (d.scope != scope) {
      _resetArtifacts();
      d.scope = scope;
      _restoreArtifactIntents(api, session);
    }
    if (d.polling ||
        d.supported == false ||
        (d.retryAfter != null && _artifactNow().isBefore(d.retryAfter!))) {
      return;
    }
    d.polling = true;
    final generation = connectionGeneration, read = d.read;
    var pageRead = false;
    try {
      // One bounded page per tick; detail changes arrive as native references.
      final page = await api.artifacts.page(session, after: d.after);
      pageRead = true;
      if (!_artifactScope(session, generation, api, read)) return;
      d.supported = true;
      for (final entry in page.items) {
        _adoptArtifact(entry);
      }
      if (page.nextAfter != null) d.after = page.nextAfter!;
      for (final id in d.refresh.toList().take(2)) {
        try {
          final item = await api.artifacts.detail(session, id);
          if (!_artifactScope(session, generation, api, read)) return;
          _adoptArtifact(item);
          d.refresh.remove(id);
        } on ArtifactFailure catch (failure) {
          if (!_artifactScope(session, generation, api, read)) return;
          if (failure.status == 404) {
            d.refresh.remove(id);
          } else {
            rethrow;
          }
        }
      }
      d.retryAfter = null;
      if (d.intents.isEmpty) d.error = null;
      touch();
    } on ArtifactFailure catch (failure) {
      if (!_artifactScope(session, generation, api, read)) return;
      if (!pageRead && failure.status == 404 && d.items.isEmpty) {
        d.supported = false;
      } else {
        d.error = failure.message;
        d.retryAfter = _artifactNow().add(const Duration(seconds: 5));
      }
      touch();
    } catch (_) {
      if (_artifactScope(session, generation, api, read)) {
        d.error = 'Interactive views could not be refreshed.';
        touch();
      }
    } finally {
      if (read == d.read) d.polling = false;
    }
  }

  void chooseArtifact(String id) {
    final d = _artifacts;
    if (d.editing ||
        d.actionPending ||
        d.intents.isNotEmpty ||
        !d.items.containsKey(id)) {
      return;
    }
    d.selected = id;
    d.closed.remove(id);
    d.error = null;
    touch();
  }

  void closeArtifact() {
    final d = _artifacts;
    if (d.editing || d.actionPending || d.intents.isNotEmpty) return;
    if (d.selected != null) d.closed.add(d.selected!);
    d.selected = null;
    d.pinned = false;
    d.dismissedThrough = d.highWater;
    touch();
  }

  void pinArtifact(bool value) {
    _artifacts.pinned = value;
    touch();
  }

  void editArtifact(bool value) {
    final d = _artifacts;
    if (d.editing == value) return;
    d.editing = value;
    if (!value && !d.actionPending && d.intents.isEmpty) {
      final pending = d.deferred.remove(d.selected);
      if (pending != null) _adoptArtifact(pending);
    }
    touch();
  }

  Future<void> interactWithArtifact(String component, Object? value) async {
    final api = client, session = converseSession, artifact = activeArtifact;
    final d = _artifacts;
    if (api == null ||
        session == null ||
        artifact == null ||
        artifact.sessionId != session.sessionId ||
        artifact.threadId != session.threadId ||
        artifact.epoch != session.epoch ||
        d.actionPending ||
        !sessionAuthenticated) {
      return;
    }
    final generation = connectionGeneration, read = d.read;
    final key = '${artifact.artifactId}/$component';
    final prior = d.intents[key];
    if (prior == null && d.intents.isNotEmpty) {
      d.error =
          'Reconcile the earlier interaction before changing another value.';
      touch();
      return;
    }
    if (prior != null &&
        (prior.revision != artifact.revision || prior.value != value)) {
      d.error =
          'An earlier interaction is unconfirmed. Retry that value before changing it.';
      touch();
      return;
    }
    if (prior == null && d.retainedIntents.length >= 32) {
      d.error =
          'Unconfirmed interactions have reached the local limit. Reconcile one before continuing.';
      touch();
      return;
    }
    final intent =
        prior ??
        _ArtifactIntent(
          newIdempotencyKey(),
          value,
          artifact,
          component,
          _artifactBinding(api, session),
        );
    d.intents[key] = intent;
    d.retainedIntents[intent.key] = intent;
    d.actionPending = true;
    d.error = null;
    touch();
    try {
      final next = await api.artifacts.action(
        session,
        intent.artifact,
        component,
        value,
        intent.key,
      );
      if (!_artifactScope(session, generation, api, read)) return;
      _adoptArtifact(next, action: true);
      _settleArtifactIntent(key, intent);
    } on ArtifactFailure catch (failure) {
      if (!_artifactScope(session, generation, api, read)) return;
      if (failure.status != null &&
          failure.status! < 500 &&
          failure.status != 408) {
        _settleArtifactIntent(key, intent);
        d.refresh.add(artifact.artifactId);
      }
      d.error = failure.message;
    } catch (_) {
      if (_artifactScope(session, generation, api, read)) {
        d.error =
            'Interaction result is unconfirmed. Retry the same value to reconcile it.';
      }
    } finally {
      if (_artifactScope(session, generation, api, read)) {
        d.actionPending = false;
        if (!d.editing && d.intents.isEmpty) {
          final pending = d.deferred.remove(d.selected);
          if (pending != null) _adoptArtifact(pending);
        }
        touch();
      }
    }
  }

  void retryArtifactInteraction() {
    final artifact = activeArtifact;
    if (artifact == null || _artifacts.actionPending) return;
    for (final entry in _artifacts.intents.entries) {
      final prefix = '${artifact.artifactId}/';
      if (entry.key.startsWith(prefix)) {
        unawaited(
          interactWithArtifact(
            entry.key.substring(prefix.length),
            entry.value.value,
          ),
        );
        return;
      }
    }
  }

  bool prefillArtifactQuestion(String message) {
    final artifact = activeArtifact;
    if (artifact == null || message.isEmpty || message.length > 4000) {
      return false;
    }
    final text = _converseDraft.isEmpty ? message : '$_converseDraft\n$message';
    if (text.length > 16000) return false;
    final priorScope = _converseDraft.trim().isEmpty
        ? selectedContentScope
        : _converseDraftScope;
    _converseDraftScope = priorScope.combine(
      ContentScope.fromWire(artifact.contentScope),
    );
    _converseDraft = text;
    touch();
    return true;
  }
}
