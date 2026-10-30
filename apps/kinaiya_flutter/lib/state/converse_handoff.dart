import '../api/models.dart';

/// A native routing receipt is evidence of dispatch, not dialogue or completion.
/// Source revisions and authenticated producer identities are checked here before
/// the presentation can announce any state. This owner never calls inference.
class ConverseHandoffTracker {
  final Map<String, _Source> _sources = {};
  final Map<(String, String, int, String), String> _windows = {};
  final Map<String, int> _sequences = {};
  final Set<String> _accepted = {};

  void clear() {
    _sources.clear();
    _windows.clear();
    _sequences.clear();
    _accepted.clear();
  }

  bool acceptedSource(SessionInfo session, String eventId) {
    if (!_identifier(eventId)) return false;
    _sources.putIfAbsent(eventId, () => _Source(session));
    _bound();
    return true;
  }

  void _bound() {
    while (_sources.length > 256) {
      final old = _sources.keys.first;
      _sources.remove(old);
      _sequences.remove(old);
      _accepted.remove(old);
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
      if (revision > source.revision) source.revision = revision;
      if (window) {
        source.utterance = utterance as String;
        source.windowIndex = index as int;
        final prior = _sources[_windows[source.windowKey]];
        if (prior == null ||
            source.windowIndex > prior.windowIndex ||
            (source.windowIndex == prior.windowIndex &&
                source.revision > prior.revision)) {
          _windows[source.windowKey] = entry.eventId;
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
          revision > source.revision) {
        source.revision = revision;
        _accepted.remove(entry.payload['target_event_id']);
      }
      return null;
    }
    if (entry.origin != 'decisions' ||
        !const {
          'routing.dispatch',
          'routing.window_dispatch',
          'routing.window_status',
        }.contains(entry.kind)) {
      return null;
    }
    final id = entry.payload['source_event_id'];
    final source = _sources[id];
    if (id is! String ||
        source == null ||
        !source.matches(current) ||
        source.sequence == null ||
        entry.payload['source_sequence'] is! int ||
        entry.payload['source_revision'] is! int ||
        entry.payload['source_sequence'] != source.sequence ||
        entry.payload['source_revision'] != source.revision ||
        entry.sequence <= source.sequence! ||
        entry.sequence <= (_sequences[id] ?? 0) ||
        (source.utterance != null && _windows[source.windowKey] != id)) {
      return null;
    }
    String? text;
    if (entry.kind == 'routing.window_status') {
      if (entry.payload['phase'] == 'agent_unavailable' &&
          entry.payload['reason'] == 'agent_status_unavailable' &&
          !_accepted.contains(id)) {
        text = 'Agent is unavailable. This request has not been accepted.';
      }
    } else {
      final route = entry.kind == 'routing.dispatch'
          ? entry.payload['route']
          : switch (entry.payload['agent_route']) {
              'QUEUE_UNTIL_TASK_DONE' => 'queue',
              'STEER_CURRENT_TASK' => 'steer',
              'INTERRUPT_CURRENT_TASK' => 'interrupt',
              _ => null,
            };
      if (!const {'queue', 'steer', 'interrupt'}.contains(route) ||
          !_identifier(entry.payload['decision_id'])) {
        return null;
      }
      final state = entry.payload['state'];
      if ((entry.kind == 'routing.dispatch' && state == 'accepted') ||
          (entry.kind == 'routing.window_dispatch' && state == 'applied')) {
        final task = entry.payload['native_task_id'];
        final target = entry.payload['target_task_id'];
        if (!_identifier(task) ||
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
        text =
            'Agent accepted the ${route == 'queue'
                ? 'request'
                : route == 'steer'
                ? 'update'
                : 'interruption'} · Task $task';
      } else if (!_accepted.contains(id)) {
        text = switch (state) {
          'queued' => 'Waiting for Agent acceptance.',
          'rejected' => 'Agent did not accept this request.',
          'unknown' =>
            'Agent acceptance is unconfirmed. Check Agent Chat before retrying.',
          'not_applied' => 'This request was not applied to Agent.',
          _ => null,
        };
      }
    }
    if (text == null) return null;
    _sequences[id] = entry.sequence;
    return HandoffNotice(id, text);
  }
}

bool _identifier(Object? value) =>
    value is String && RegExp(r'^[A-Za-z0-9_.:-]{1,256}$').hasMatch(value);

class _Source {
  _Source(this.session);
  final SessionInfo session;
  int? sequence;
  int revision = 1, windowIndex = -1;
  String? utterance;
  (String, String, int, String) get windowKey =>
      (session.sessionId, session.threadId, session.epoch, utterance!);
  bool matches(SessionInfo other) =>
      session.sessionId == other.sessionId &&
      session.threadId == other.threadId &&
      session.epoch == other.epoch;
}

class HandoffNotice {
  const HandoffNotice(this.sourceEventId, this.text);
  final String sourceEventId, text;
}
