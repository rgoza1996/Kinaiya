/// Buffered recording consent and playback ownership, separate from live capture.
library;

import 'dart:typed_data';

import '../api/models.dart';

const nativeAudioImportHint =
    'Native audio import: 16 kHz mono PCM16 WAV, 30 seconds maximum.';

class NativeAudioSupport {
  const NativeAudioSupport.unknown()
    : known = false,
      configured = false,
      available = false;
  const NativeAudioSupport._(this.known, this.configured, this.available);

  factory NativeAudioSupport.fromHealth(Map<String, Object?> health) {
    if (!health.containsKey('native_audio')) {
      return const NativeAudioSupport._(true, false, false);
    }
    final raw = health['native_audio'];
    if (raw is! Map<String, Object?> ||
        raw['configured'] is! bool ||
        raw['available'] is! bool ||
        raw['mode'] != 'buffered_native_audio' ||
        raw['input_sample_rate'] is! int ||
        raw['input_sample_rate'] != 16000 ||
        raw['max_seconds'] is! int ||
        raw['max_seconds'] != 30 ||
        (raw['available'] == true && raw['configured'] != true)) {
      // A malformed native declaration must never select legacy transcription.
      return const NativeAudioSupport._(true, true, false);
    }
    return NativeAudioSupport._(
      true,
      raw['configured'] == true,
      raw['available'] == true,
    );
  }

  final bool known, configured, available;

  bool sameAs(NativeAudioSupport other) =>
      known == other.known &&
      configured == other.configured &&
      available == other.available;
}

/// Accept only the exact WAV shape supported by the initial native clip API.
/// Copy before any await; unsupported input is refused without resampling.
List<int> canonicalNativeAudioWav(List<int> bytes) {
  Never bad() => throw const FormatException(nativeAudioImportHint);
  if (bytes.length < 46 ||
      bytes.length > 960044 ||
      bytes.any((value) => value < 0 || value > 255)) {
    bad();
  }
  final copy = Uint8List.fromList(bytes);
  final data = ByteData.sublistView(copy);
  int u16(int offset) => data.getUint16(offset, Endian.little);
  int u32(int offset) => data.getUint32(offset, Endian.little);
  final size = copy.length - 44;
  if (u32(0) != 0x46464952 ||
      u32(4) != copy.length - 8 ||
      u32(8) != 0x45564157 ||
      u32(12) != 0x20746d66 ||
      u32(16) != 16 ||
      u16(20) != 1 ||
      u16(22) != 1 ||
      u32(24) != 16000 ||
      u32(28) != 32000 ||
      u16(32) != 2 ||
      u16(34) != 16 ||
      u32(36) != 0x61746164 ||
      u32(40) != size ||
      size.isOdd) {
    bad();
  }
  return List<int>.unmodifiable(copy);
}

/// Local observations only: none is an Agent acceptance or cleanup receipt.
enum NativeAudioNotice {
  classifying('Decisions is evaluating this input.'),
  busy('This audio input is waiting for capacity.'),
  waiting('Converse is waiting for more input.'),
  preparing('A Converse response is pending.'),
  agentUnknown('Agent availability is unconfirmed.'),
  routingUnknown(
    'Decisions could not confirm a route. Check Decisions for details.',
  ),
  unconfirmed(
    'Native audio status is unconfirmed. Check conversation history or stop waiting.',
  ),
  admissionUnknown(
    'Native audio clip admission was not confirmed. Check conversation history before submitting again.',
  ),
  noReply('Converse will not reply to this input.', terminal: true);

  const NativeAudioNotice(this.message, {this.terminal = false});
  final String message;
  final bool terminal;
}

class NativeAudioClipOwner {
  NativeAudioClipOwner({
    required this.client,
    required this.connectionGeneration,
    required this.sourceGeneration,
    required SessionInfo session,
    required this.utteranceId,
    required this.requestId,
    required this.turnNonce,
    required this.contentScope,
  }) : sessionId = session.sessionId,
       threadId = session.threadId,
       epoch = session.epoch;

  final Object client;
  final int connectionGeneration, sourceGeneration, epoch;
  final String sessionId, threadId, utteranceId, requestId;
  final ContentScope contentScope;
  int turnNonce;
  String? _sourceId;
  int? _sourceSequence, _sourceRevision;
  (String, String, int, String)? _replyBinding;
  final _pending = <String, JournalEntry>{};
  final _pendingStatus = <String, JournalEntry>{};
  final _notices = <NativeAudioNotice>{};
  int _statusSequence = 0;
  bool _statusOverflowed = false;

  bool get admitted => _sourceId != null;
  bool get hasReply => _replyBinding != null;
  bool get statusOverflowed => _statusOverflowed;

  bool current({
    required Object? currentClient,
    required int connection,
    required int source,
    required SessionInfo? session,
    required int nonce,
  }) =>
      identical(client, currentClient) &&
      connectionGeneration == connection &&
      sourceGeneration == source &&
      sessionId == session?.sessionId &&
      threadId == session?.threadId &&
      epoch == session?.epoch &&
      turnNonce == nonce;

  bool candidate(JournalEntry entry) =>
      entry.sessionId == sessionId &&
      entry.threadId == threadId &&
      entry.epoch == epoch &&
      entry.kind == 'conversation.converse' &&
      entry.origin == 'converse' &&
      entry.eventId.isNotEmpty &&
      entry.sequence > 0 &&
      entry.payload['native_audio'] == true &&
      entry.payload['text'] is String &&
      (entry.payload['text'] as String).length <= 16000 &&
      entry.payload['window_utterance_id'] == utteranceId &&
      entry.payload['generation_epoch'] is int &&
      (entry.payload['generation_epoch'] as int) > 0 &&
      entry.payload['window_source_id'] is String &&
      (entry.payload['window_source_id'] as String).isNotEmpty &&
      entry.payload['chunk_id'] is String &&
      (entry.payload['chunk_id'] as String).isNotEmpty &&
      entry.payload['runtime_id'] is String &&
      (entry.payload['runtime_id'] as String).isNotEmpty;

  bool accepts(JournalEntry entry) =>
      admitted &&
      candidate(entry) &&
      entry.payload['window_source_id'] == _sourceId;

  (String, String, int, String) _replyIdentity(JournalEntry entry) => (
    entry.eventId,
    entry.payload['runtime_id'] as String,
    entry.payload['generation_epoch'] as int,
    entry.payload['chunk_id'] as String,
  );

  /// A buffered clip owns one atomic native reply. Replays must keep its
  /// exact identity; another tuple cannot reset playback or its epoch.
  bool bindReply(JournalEntry entry) {
    if (!accepts(entry)) return false;
    final next = _replyIdentity(entry);
    if (_replyBinding != null) return _replyBinding == next;
    _replyBinding = next;
    return true;
  }

  bool ownsReply(JournalEntry entry) =>
      _replyBinding != null &&
      accepts(entry) &&
      _replyBinding == _replyIdentity(entry);

  /// Before admission, these observations have no reply or terminal authority.
  bool statusCandidate(JournalEntry entry) {
    final payload = entry.payload;
    final source = payload['source_event_id'];
    if (entry.sessionId != sessionId ||
        entry.threadId != threadId ||
        entry.epoch != epoch ||
        entry.eventId.isEmpty ||
        entry.eventId.length > 128 ||
        entry.sequence < 1 ||
        source is! String ||
        source.isEmpty ||
        source.length > 128 ||
        entry.causes.length != 1 ||
        entry.causes.single != source) {
      return false;
    }
    for (final key in const ['error_source', 'delivery']) {
      final value = payload[key];
      if (value != null && (value is! String || value.length > 128)) {
        return false;
      }
    }
    if (payload['append_unconfirmed'] != null &&
        payload['append_unconfirmed'] is! bool) {
      return false;
    }
    if (entry.kind == 'converse.window_state') {
      final runtime = payload['runtime_id'];
      return entry.origin == 'converse' &&
          payload['utterance_id'] == utteranceId &&
          runtime is String &&
          runtime.isNotEmpty &&
          runtime.length <= 128 &&
          const {
            'waiting',
            'preparing',
            'busy',
            'error',
          }.contains(payload['phase']);
    }
    if (entry.origin != 'decisions' ||
        payload['source_sequence'] is! int ||
        (payload['source_sequence'] as int) < 1 ||
        entry.sequence <= (payload['source_sequence'] as int) ||
        payload['source_revision'] is! int ||
        (payload['source_revision'] as int) < 1) {
      return false;
    }
    if (entry.kind == 'routing.window_status') {
      final reason = payload['reason'];
      return reason is String &&
          reason.isNotEmpty &&
          reason.length <= 120 &&
          const {
            'classifying',
            'classified',
            'retrying',
            'busy',
            'agent_unavailable',
            'expired',
            'error',
          }.contains(payload['phase']);
    }
    final target = payload['target_task_id'];
    final revision = payload['expected_objective_revision'];
    final targetValid =
        target == null && revision == null ||
        target is String &&
            target.isNotEmpty &&
            target.length <= 128 &&
            revision is int &&
            revision > 0;
    return entry.kind == 'routing.window_decided' &&
        targetValid &&
        ((payload['agent_route'] != 'STEER_CURRENT_TASK' &&
                payload['agent_route'] != 'INTERRUPT_CURRENT_TASK') ||
            target != null) &&
        (payload['agent_route'] != 'IGNORE_FOR_IMMEDIATE_ROUTING' ||
            target == null) &&
        const {
          'IGNORE',
          'WAIT_FOR_NEXT_WINDOW',
          'RESPOND_INTERRUPT_USER',
          'RESPOND_WITHOUT_INTERRUPTING_USER',
        }.contains(payload['conversation']) &&
        const {
          'IGNORE_FOR_IMMEDIATE_ROUTING',
          'STEER_CURRENT_TASK',
          'INTERRUPT_CURRENT_TASK',
          'QUEUE_UNTIL_TASK_DONE',
        }.contains(payload['agent_route']);
  }

  bool acceptsStatus(JournalEntry entry) =>
      admitted &&
      statusCandidate(entry) &&
      entry.payload['source_event_id'] == _sourceId &&
      entry.sequence > _sourceSequence! &&
      (entry.kind == 'converse.window_state' ||
          (entry.payload['source_sequence'] == _sourceSequence &&
              entry.payload['source_revision'] == _sourceRevision));

  void deferStatus(JournalEntry entry) {
    if (!statusCandidate(entry)) return;
    final key = '${entry.payload['source_event_id']}:${entry.kind}';
    final old = _pendingStatus[key];
    if (old != null && old.sequence >= entry.sequence) return;
    if (old == null && _pendingStatus.length >= 4) {
      _statusOverflowed = true;
      return;
    }
    // Retain bounded immutable scalars, not a caller's mutable payload/extras.
    final payload = <String, Object?>{
      'source_event_id': entry.payload['source_event_id'],
      for (final field in const [
        'error_source',
        'delivery',
        'append_unconfirmed',
      ])
        if (entry.payload.containsKey(field)) field: entry.payload[field],
      if (entry.kind == 'converse.window_state') ...{
        'utterance_id': utteranceId,
        'runtime_id': entry.payload['runtime_id'],
        'phase': entry.payload['phase'],
      } else ...{
        'source_sequence': entry.payload['source_sequence'],
        'source_revision': entry.payload['source_revision'],
        if (entry.kind == 'routing.window_status') ...{
          'phase': entry.payload['phase'],
          'reason': entry.payload['reason'],
        } else ...{
          for (final field in const [
            'conversation',
            'agent_route',
            'target_task_id',
            'expected_objective_revision',
          ])
            field: entry.payload[field],
        },
      },
    };
    _pendingStatus[key] = JournalEntry(
      eventId: entry.eventId,
      sequence: entry.sequence,
      sessionId: sessionId,
      threadId: threadId,
      epoch: epoch,
      kind: entry.kind,
      origin: entry.origin,
      payload: Map<String, Object?>.unmodifiable(payload),
      causes: List<String>.unmodifiable([entry.causes.single]),
    );
  }

  bool markNotice(NativeAudioNotice notice) => _notices.add(notice);

  NativeAudioNotice? observeStatus(JournalEntry entry) {
    if (!acceptsStatus(entry) ||
        hasReply ||
        entry.sequence <= _statusSequence) {
      return null;
    }
    _statusSequence = entry.sequence;
    final payload = entry.payload;
    final NativeAudioNotice notice;
    if (payload['error_source'] != null ||
        payload['append_unconfirmed'] == true ||
        payload['delivery'] == 'delivery_unknown') {
      notice = NativeAudioNotice.unconfirmed;
    } else if (entry.kind == 'routing.window_decided') {
      notice = payload['conversation'] == 'IGNORE'
          ? NativeAudioNotice.noReply
          : payload['conversation'] == 'WAIT_FOR_NEXT_WINDOW'
          ? NativeAudioNotice.waiting
          : NativeAudioNotice.preparing;
    } else if (entry.kind == 'converse.window_state') {
      notice = payload['phase'] == 'waiting'
          ? NativeAudioNotice.waiting
          : payload['phase'] == 'preparing'
          ? NativeAudioNotice.preparing
          : payload['phase'] == 'busy'
          ? NativeAudioNotice.busy
          : NativeAudioNotice.unconfirmed;
    } else {
      notice = switch (payload['phase']) {
        'classifying' || 'retrying' => NativeAudioNotice.classifying,
        'classified' => NativeAudioNotice.preparing,
        'busy' => NativeAudioNotice.busy,
        'agent_unavailable' => NativeAudioNotice.agentUnknown,
        _ => NativeAudioNotice.routingUnknown,
      };
    }
    return markNotice(notice) ? notice : null;
  }

  void defer(JournalEntry entry) {
    if (!candidate(entry)) return;
    if (_pending.length >= 4 && !_pending.containsKey(entry.eventId)) {
      throw const FormatException(
        'Native audio response admission is ambiguous.',
      );
    }
    _pending[entry.eventId] = entry;
  }

  void confirm(JournalEntry entry) {
    if (entry.sessionId != sessionId ||
        entry.threadId != threadId ||
        entry.epoch != epoch ||
        entry.origin != 'authenticated-user' ||
        entry.kind != 'conversation.window' ||
        entry.eventId.isEmpty ||
        entry.sequence < 1 ||
        entry.payload['utterance_id'] != utteranceId ||
        entry.payload['capture_ended'] != true ||
        entry.payload['user_speaking'] != false ||
        entry.payload['window_index'] != 0 ||
        entry.payload['window_index'] is! int ||
        entry.payload['revision'] != 1 ||
        entry.payload['revision'] is! int ||
        entry.payload['content_scope'] != contentScope.wireValue ||
        entry.payload['text'] is! String ||
        (entry.payload['text'] as String).length > 16000 ||
        (_sourceId != null &&
            (_sourceId != entry.eventId ||
                _sourceSequence != entry.sequence ||
                _sourceRevision != entry.payload['revision']))) {
      throw const FormatException(
        'Native audio clip admission was not confirmed.',
      );
    }
    _sourceId = entry.eventId;
    _sourceSequence = entry.sequence;
    _sourceRevision = entry.payload['revision'] as int;
  }

  List<JournalEntry> takePending() {
    final result = [..._pending.values, ..._pendingStatus.values]
      ..sort((a, b) => a.sequence.compareTo(b.sequence));
    _pending.clear();
    _pendingStatus.clear();
    return result;
  }
}
