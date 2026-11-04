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

class NativeAudioClipOwner {
  NativeAudioClipOwner({
    required this.client,
    required this.connectionGeneration,
    required this.sourceGeneration,
    required SessionInfo session,
    required this.utteranceId,
    required this.requestId,
    required this.turnNonce,
  }) : sessionId = session.sessionId,
       threadId = session.threadId,
       epoch = session.epoch;

  final Object client;
  final int connectionGeneration, sourceGeneration, epoch;
  final String sessionId, threadId, utteranceId, requestId;
  int turnNonce;
  String? _sourceId;
  (String, String, int, String)? _replyBinding;
  final _pending = <String, JournalEntry>{};

  bool get admitted => _sourceId != null;

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
        entry.payload['text'] is! String ||
        (entry.payload['text'] as String).length > 16000 ||
        (_sourceId != null && _sourceId != entry.eventId)) {
      throw const FormatException(
        'Native audio clip admission was not confirmed.',
      );
    }
    _sourceId = entry.eventId;
  }

  List<JournalEntry> takePending() {
    final result = _pending.values.toList()
      ..sort((a, b) => a.sequence.compareTo(b.sequence));
    _pending.clear();
    return result;
  }
}
