import '../api/models.dart';

/// A native routing receipt is evidence of dispatch, not dialogue or completion.
/// Source revisions and authenticated producer identities are checked here before
/// the presentation can announce any state. This owner never calls inference.
class ConverseHandoffTracker {
  final Map<String, _Source> _sources = {};
  final Map<(String, String, int, String), String> _windows = {};
  final Map<String, int> _sequences = {};
  final Set<String> _accepted = {};
  final Map<String, JournalEntry> _decisions = {};
  final Map<String, JournalEntry> _pending = {};
  final Map<String, HandoffNotice> _notices = {};
  List<JournalEntry> get pendingReceipts => List.unmodifiable(_pending.values);
  Iterable<HandoffNotice> get notices => _notices.values;

  void discardPending(String eventId) => _pending.remove(eventId);

  HandoffNotice? _defer(JournalEntry entry) {
    if (_identifier(entry.eventId)) {
      _pending.putIfAbsent(entry.eventId, () => entry);
    }
    while (_pending.length > 128) {
      _pending.remove(_pending.keys.first);
    }
    return null;
  }

  List<HandoffNotice> drainPending(SessionInfo current) {
    final notices = <HandoffNotice>[];
    for (final entry in pendingReceipts) {
      final notice = observe(entry, current);
      if (notice != null) notices.add(notice);
    }
    return notices;
  }

  void clear() {
    _sources.clear();
    _windows.clear();
    _sequences.clear();
    _accepted.clear();
    _decisions.clear();
    _pending.clear();
    _notices.clear();
  }

  HandoffNotice? noticeForSource(String sourceEventId, [SessionInfo? current]) {
    final notice = _notices[sourceEventId];
    if (notice == null) return null;
    if (current != null && !notice.matchesSession(current)) return null;
    final source = _sources[sourceEventId];
    if (source == null || source.revision != notice.sourceRevision) {
      return null;
    }
    if (source.utterance != null) {
      final latestId = _windows[source.windowKey];
      final latest = latestId == null ? null : _sources[latestId];
      if (latest == null || latest.text != source.text) return null;
    }
    return notice;
  }

  HandoffNotice? noticeForTurn({
    required SessionInfo current,
    required String role,
    String? eventId,
    String? utteranceId,
    int windowIndex = 0,
    int windowRevision = 1,
    String text = '',
  }) {
    if (role != 'user') return null;
    if (utteranceId != null) {
      final key = (
        current.sessionId,
        current.threadId,
        current.epoch,
        utteranceId,
      );
      final latestId = _windows[key];
      final latest = latestId == null ? null : _sources[latestId];
      if (latest == null ||
          !latest.matches(current) ||
          latest.windowIndex != windowIndex ||
          latest.revision != windowRevision ||
          latest.text != text) {
        return null;
      }
      final direct = eventId == null ? null : noticeForSource(eventId, current);
      if (direct != null) return direct;
      for (final notice in _notices.values) {
        if (notice.matchesSession(current) &&
            notice.utteranceId == utteranceId &&
            notice.sourceText == latest.text &&
            noticeForSource(notice.sourceEventId, current) != null) {
          return notice;
        }
      }
      return null;
    }
    if (eventId == null) return null;
    final notice = noticeForSource(eventId, current);
    if (notice == null || notice.sourceRevision != windowRevision) {
      return null;
    }
    return notice;
  }

  bool acceptedSource(SessionInfo session, String eventId) {
    if (!_identifier(eventId)) return false;
    _sources.putIfAbsent(eventId, () => _Source(session));
    _bound();
    return true;
  }

  void _bound() {
    while (_sources.length > 256) {
      final pinned = _pending.values
          .map((entry) => entry.payload['source_event_id'])
          .toSet();
      final old = _sources.keys.firstWhere((id) => !pinned.contains(id));
      _sources.remove(old);
      _sequences.remove(old);
      _accepted.remove(old);
      _notices.remove(old);
      _windows.removeWhere((_, source) => source == old);
    }
  }

  /// Called only with entries from the current authenticated journal read.
  HandoffNotice? observe(JournalEntry entry, SessionInfo current) {
    if (entry.sessionId != current.sessionId ||
        entry.threadId != current.threadId ||
        entry.epoch != current.epoch) {
      return null;
    }
    if (entry.kind == 'conversation.user' ||
        entry.kind == 'conversation.window') {
      if (entry.origin != 'authenticated-user' ||
          entry.sequence < 1 ||
          !_identifier(entry.eventId)) {
        return null;
      }
      final revision = entry.payload['revision'] ?? 1;
      if (revision is! int || revision < 1) return null;
      final window = entry.kind == 'conversation.window';
      final utterance = entry.payload['utterance_id'];
      final index = entry.payload['window_index'];
      if (window && (!_identifier(utterance) || index is! int || index < 0)) {
        return null;
      }
      // Historical native user entries may seed a resumed session as well as
      // the accepted SSE binding for a newly submitted typed turn.
      final source = _sources.putIfAbsent(
        entry.eventId,
        () => _Source(current),
      );
      if (!source.matches(current)) return null;
      if (source.sequence != null && source.sequence != entry.sequence) {
        return null;
      }
      source.sequence = entry.sequence;
      source.text = entry.payload['text'] is String
          ? entry.payload['text'] as String
          : '';
      if (revision > source.revision) {
        source.revision = revision;
        _accepted.remove(entry.eventId);
        _notices.remove(entry.eventId);
        source.causeEventId = null;
        source.causeSequence = null;
      }
      if (revision == 1) {
        source.causeEventId = entry.eventId;
        source.causeSequence = entry.sequence;
      }
      if (window) {
        source.utterance = utterance as String;
        source.windowIndex = index as int;
        final prior = _sources[_windows[source.windowKey]];
        if (prior == null ||
            source.windowIndex > prior.windowIndex ||
            (source.windowIndex == prior.windowIndex &&
                source.revision > prior.revision)) {
          _windows[source.windowKey] = entry.eventId;
          _notices.removeWhere(
            (_, notice) =>
                notice.matchesSession(current) &&
                notice.utteranceId == source.utterance &&
                notice.sourceText != source.text,
          );
        }
      }
      _bound();
      return null;
    }
    if (entry.kind == 'conversation.revision') {
      if (entry.origin != 'authenticated-user' && entry.origin != 'converse') {
        return null;
      }
      final source = _sources[entry.payload['target_event_id']];
      final revision = entry.payload['revision'];
      if (source != null &&
          source.matches(current) &&
          revision is int &&
          revision >= source.revision &&
          entry.sequence > (source.sequence ?? 0)) {
        if (revision > source.revision) {
          source.revision = revision;
          source.causeEventId = null;
          _accepted.remove(entry.payload['target_event_id']);
          _notices.remove(entry.payload['target_event_id']);
        }
        if (source.causeEventId == null ||
            source.causeEventId == entry.eventId) {
          source.causeEventId = entry.eventId;
          source.causeSequence = entry.sequence;
        }
      }
      return null;
    }
    if (entry.origin != 'decisions') return null;
    if (entry.kind == 'routing.decided' ||
        entry.kind == 'routing.window_decided') {
      final decisionId = entry.payload['decision_id'];
      if (!_identifier(decisionId)) return null;
      _decisions.putIfAbsent(decisionId as String, () => entry);
      final pinned = _pending.values
          .map((event) => event.payload['decision_id'])
          .toSet();
      while (_decisions.length > 256) {
        _decisions.remove(
          _decisions.keys.firstWhere((id) => !pinned.contains(id)),
        );
      }
      return null;
    }
    if (!const {
      'routing.dispatch',
      'routing.window_dispatch',
      'routing.window_status',
    }.contains(entry.kind)) {
      return null;
    }
    _pending.remove(entry.eventId);
    final id = entry.payload['source_event_id'];
    final source = _sources[id];
    if (id is! String ||
        !_identifier(id) ||
        entry.payload['source_sequence'] is! int ||
        (entry.payload['source_sequence'] as int) < 1 ||
        entry.payload['source_revision'] is! int ||
        (entry.payload['source_revision'] as int) < 1 ||
        entry.sequence <= (entry.payload['source_sequence'] as int) ||
        entry.causes.length != 1 ||
        !_identifier(entry.causes.single)) {
      return null;
    }
    if (source == null ||
        source.sequence == null ||
        (entry.payload['source_revision'] as int) > source.revision ||
        (source.utterance != null && !_windows.containsKey(source.windowKey))) {
      return _defer(entry);
    }
    if (!source.matches(current) ||
        source.sequence == null ||
        entry.payload['source_sequence'] is! int ||
        entry.payload['source_revision'] is! int ||
        entry.payload['source_sequence'] != source.sequence ||
        entry.payload['source_revision'] != source.revision ||
        entry.sequence <= source.sequence! ||
        entry.sequence <= (_sequences[id] ?? 0) ||
        (source.utterance != null &&
            _sources[_windows[source.windowKey]]?.text != source.text)) {
      return null;
    }
    if ((entry.kind == 'routing.dispatch') != (source.utterance == null)) {
      return null;
    }
    String? text;
    var status = HandoffStatus.unknown;
    String? routeValue;
    String? nativeTaskId;
    String? targetTaskId;
    if (entry.kind == 'routing.window_status') {
      if (!_singleCause(entry, id)) return null;
      if (entry.payload['phase'] == 'agent_unavailable' &&
          entry.payload['reason'] == 'agent_status_unavailable' &&
          !_accepted.contains(id)) {
        status = HandoffStatus.unavailable;
        text =
            'Agent status is unavailable. This request has no confirmed acceptance.';
      }
    } else {
      final decisionId = entry.payload['decision_id'];
      if (!_identifier(decisionId)) return null;
      final decision = _decisions[decisionId];
      if (decision == null) return _defer(entry);
      final window = entry.kind == 'routing.window_dispatch';
      final expectedKind = window
          ? 'routing.window_decided'
          : 'routing.decided';
      if (decision.kind != expectedKind ||
          decision.origin != 'decisions' ||
          decision.sessionId != current.sessionId ||
          decision.threadId != current.threadId ||
          decision.epoch != current.epoch ||
          decision.sequence >= entry.sequence ||
          decision.sequence <= (source.causeSequence ?? source.sequence!) ||
          decision.payload['source_revision'] is! int ||
          decision.payload['source_revision'] != source.revision ||
          decision.payload['target_task_id'] !=
              entry.payload['target_task_id']) {
        return null;
      }
      if (window) {
        if (!_singleCause(decision, id) ||
            !_singleCause(entry, decision.eventId) ||
            decision.payload['source_event_id'] != id ||
            decision.payload['source_sequence'] is! int ||
            decision.payload['source_sequence'] != source.sequence ||
            decision.payload['agent_route'] != entry.payload['agent_route']) {
          return null;
        }
      } else {
        final ids = decision.payload['source_event_ids'];
        final cause = source.causeEventId;
        if (cause == null) return _defer(entry);
        if (ids is! List ||
            ids.length != 1 ||
            ids.single != id ||
            !_singleCause(decision, cause) ||
            !_singleCause(entry, cause) ||
            decision.payload['route'] != entry.payload['route']) {
          return null;
        }
      }
      final route = window
          ? switch (decision.payload['agent_route']) {
              'QUEUE_UNTIL_TASK_DONE' => 'queue',
              'STEER_CURRENT_TASK' => 'steer',
              'INTERRUPT_CURRENT_TASK' => 'interrupt',
              _ => null,
            }
          : decision.payload['route'];
      if (!const {'queue', 'steer', 'interrupt'}.contains(route)) return null;
      routeValue = route as String?;
      targetTaskId = entry.payload['target_task_id'] is String
          ? entry.payload['target_task_id'] as String
          : null;
      final state = entry.payload['state'];
      if ((entry.kind == 'routing.dispatch' && state == 'accepted') ||
          (entry.kind == 'routing.window_dispatch' && state == 'applied')) {
        final task = entry.payload['native_task_id'];
        final target = entry.payload['target_task_id'];
        // A queued window may wait on an older target task while native
        // submission accepts a new task. Controls must match their target.
        if (!_identifier(task) ||
            (target != null && !_identifier(target)) ||
            (route != 'queue' && target != task) ||
            !const {
              'accepted',
              'queued',
              'running',
              'verifying',
              'awaiting_approval',
              'blocked',
              'succeeded',
              'failed',
              'cancel_requested',
              'cancelled',
              'paused',
            }.contains(entry.payload['native_task_state'])) {
          return null;
        }
        _accepted.add(id);
        status = window ? HandoffStatus.applied : HandoffStatus.accepted;
        nativeTaskId = task as String;
        text =
            'Agent accepted the ${route == 'queue'
                ? 'request'
                : route == 'steer'
                ? 'update'
                : 'interruption'} · Task $task';
      } else if (!_accepted.contains(id)) {
        switch (state) {
          case 'queued':
            status = HandoffStatus.queued;
            text = 'Waiting for Agent acceptance.';
          case 'rejected':
            status = HandoffStatus.rejected;
            text = 'Agent did not accept this request.';
          case 'unknown':
            status = HandoffStatus.unknown;
            text =
                'Agent acceptance is unconfirmed. Check Agent Chat before retrying.';
          case 'not_applied':
            status = HandoffStatus.notApplied;
            text = 'This request was not applied to Agent.';
        }
      }
    }
    if (text == null) return null;
    _sequences[id] = entry.sequence;
    final notice = HandoffNotice(
      id,
      text,
      status: status,
      sourceRevision: source.revision,
      sessionId: current.sessionId,
      threadId: current.threadId,
      epoch: current.epoch,
      utteranceId: source.utterance,
      windowIndex: source.windowIndex >= 0 ? source.windowIndex : null,
      sourceText: source.text,
      route: routeValue,
      nativeTaskId: nativeTaskId,
      targetTaskId: targetTaskId,
    );
    _notices[id] = notice;
    return notice;
  }
}

bool _singleCause(JournalEntry entry, String expected) =>
    entry.causes.length == 1 && entry.causes.single == expected;

bool _identifier(Object? value) =>
    value is String && RegExp(r'^[A-Za-z0-9_.:-]{1,256}$').hasMatch(value);

class _Source {
  _Source(this.session);
  final SessionInfo session;
  int? sequence, causeSequence;
  String? causeEventId;
  int revision = 1, windowIndex = -1;
  String? utterance;
  String text = '';
  (String, String, int, String) get windowKey =>
      (session.sessionId, session.threadId, session.epoch, utterance!);
  bool matches(SessionInfo other) =>
      session.sessionId == other.sessionId &&
      session.threadId == other.threadId &&
      session.epoch == other.epoch;
}

enum HandoffStatus {
  queued,
  accepted,
  applied,
  rejected,
  notApplied,
  unknown,
  unavailable,
}

enum HandoffTaskDestinationStatus { none, known, stale, missing, unauthorized }

enum HandoffTaskLifecycleKind {
  queued,
  running,
  awaitingApproval,
  blocked,
  failed,
  succeededWithEvidence,
  succeededUnverified,
  unknown,
}

class HandoffTaskProgressSummary {
  const HandoffTaskProgressSummary({
    required this.kind,
    required this.stateLabel,
    required this.detailText,
    this.actionLabel,
    this.pendingApprovalsCount = 0,
    this.evidenceCount = 0,
  });

  final HandoffTaskLifecycleKind kind;
  final String stateLabel;
  final String detailText;
  final String? actionLabel;
  final int pendingApprovalsCount;
  final int evidenceCount;
}

class HandoffNotice {
  const HandoffNotice(
    this.sourceEventId,
    this.text, {
    this.status = HandoffStatus.unknown,
    this.sourceRevision = 1,
    this.sessionId = '',
    this.threadId = '',
    this.epoch = 0,
    this.utteranceId,
    this.windowIndex,
    this.sourceText = '',
    this.route,
    this.nativeTaskId,
    this.targetTaskId,
  });

  final String sourceEventId, text;
  final HandoffStatus status;
  final int sourceRevision;
  final String sessionId, threadId;
  final int epoch;
  final String? utteranceId;
  final int? windowIndex;
  final String sourceText;
  final String? route;
  final String? nativeTaskId;
  final String? targetTaskId;

  bool get hasConfirmedTaskIdentifier =>
      (status == HandoffStatus.accepted || status == HandoffStatus.applied) &&
      nativeTaskId != null &&
      nativeTaskId!.isNotEmpty;

  String get statusLabel => switch (status) {
    HandoffStatus.queued => 'Queued',
    HandoffStatus.accepted => 'Accepted',
    HandoffStatus.applied => 'Applied',
    HandoffStatus.rejected => 'Rejected',
    HandoffStatus.notApplied => 'Not applied',
    HandoffStatus.unknown => 'Unconfirmed',
    HandoffStatus.unavailable => 'Unavailable',
  };

  String get semanticSummary => 'Agent handoff $statusLabel: $text';

  bool matchesSession(SessionInfo current) =>
      sessionId == current.sessionId &&
      threadId == current.threadId &&
      epoch == current.epoch;
}
