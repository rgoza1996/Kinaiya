import 'models.dart';

const _invalid = FormatException('Invalid native playback stop');
const _maximum = 9007199254740991;

String _identity(Object? value) {
  if (value is! String || !RegExp(r'^[A-Za-z0-9_-]{1,128}$').hasMatch(value)) {
    throw _invalid;
  }
  return value;
}

int _generation(Object? value) {
  if (value is! int || value < 0 || value > _maximum) throw _invalid;
  return value;
}

/// Native generation_stop metadata is a negative playback fence. Ordinary
/// delivery acknowledgments, model text and another runtime grant no control.
class NativePlaybackStop {
  NativePlaybackStop.fromEntry(JournalEntry entry) {
    final p = entry.payload;
    if (entry.kind != 'delivery.updated' ||
        entry.origin != 'converse' ||
        p['control'] != 'generation_stop' ||
        p['status'] != 'interrupted' ||
        p['alignment'] != 'delivery_unknown' ||
        const {
          'event_id',
          'chunk_id',
          'played_seconds',
          'estimated_text_fraction',
        }.any(p.containsKey)) {
      throw _invalid;
    }
    sessionId = _identity(entry.sessionId);
    threadId = _identity(entry.threadId);
    epoch = _generation(entry.epoch);
    if (epoch == 0) throw _invalid;
    runtimeId = _identity(p['runtime_id']);
    previousGeneration = _generation(p['generation_epoch']);
    nextGeneration = _generation(p['next_generation_epoch']);
    if (nextGeneration <= previousGeneration) throw _invalid;
    final source = p['source_event_id'];
    if (source == null) {
      if (entry.causes.isNotEmpty) throw _invalid;
    } else if (entry.causes.length != 1 ||
        entry.causes.single != _identity(source)) {
      throw _invalid;
    }
  }

  late final String sessionId, threadId, runtimeId;
  late final int epoch, previousGeneration, nextGeneration;

  bool matches({
    required String sessionId,
    required String threadId,
    required int epoch,
    required String? runtimeId,
  }) =>
      this.sessionId == sessionId &&
      this.threadId == threadId &&
      this.epoch == epoch &&
      this.runtimeId == runtimeId;
}
