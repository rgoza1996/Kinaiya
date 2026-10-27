/// Pure turn-audio helpers: nonce ownership, serialized speech playback,
/// interrupt-safe elapsed estimates, and the 220ms barge-in sustain rule.
///
/// All cleanup ownership belongs to the turn nonce that started the work:
/// a stale callback (older nonce) must never reset newer speaking/busy
/// state. No platform code here, so unit tests run on any host.
library;

/// True when the [owner] turn still owns cleanup against [current].
/// Every `finally`/callback that clears speaking/busy state must gate on
/// this so a stale turn cannot clobber a newer one (nested barge-in).
bool turnOwnsCleanup(int owner, int current) => owner == current;

/// Estimated played seconds for an interrupted entry.
///
/// Returns the elapsed fraction clamped to `[0, scheduledSeconds]`, or
/// null when unknown (negative clock skew, non-positive schedule). Callers
/// report this estimate (or unknown) on interrupt: synthesis-complete alone
/// is never reported as played, and exact words are never claimed.
double? estimatePlayedSeconds({
  required DateTime start,
  required DateTime now,
  required double scheduledSeconds,
}) {
  if (scheduledSeconds <= 0) return null;
  final elapsed = now.difference(start).inMicroseconds / 1e6;
  if (elapsed.isNaN || elapsed < 0) return null;
  if (elapsed > scheduledSeconds) return scheduledSeconds;
  return elapsed;
}

/// Scheduled duration of one PCM frame carried as base64 int16 mono.
double pcmFrameSeconds({required int base64Length, required int sampleRate}) {
  if (sampleRate <= 0 || base64Length <= 0) return 0;
  final bytes = (base64Length * 3) ~/ 4;
  return (bytes ~/ 2) / sampleRate;
}

/// Serializes speech entries so independently produced sentences cannot
/// interleave: each task starts only after the previous one settles, in
/// enqueue order. A failing task does not break the chain.
class SpeechQueue {
  Future<void> _tail = Future.value();

  Future<void> enqueue(Future<void> Function() task) {
    final next = _tail.then((_) => task());
    // Keep the chain alive even when a task throws; the error still
    // surfaces to the enqueueing caller via `next`.
    _tail = next.catchError((Object _) {});
    return next;
  }
}

/// Sustained-voice barge-in detector shared with the JS bridge rule:
/// voice present during playback for [sustainMs] (default 220ms) fires.
/// Mirrors `BARGE_SUSTAIN_MS` in `web/audio_bridge.js`; Node tests pin the
/// JS side, Flutter tests pin this side.
class BargeDetector {
  BargeDetector({this.sustainMs = 220});
  final int sustainMs;

  int? _sinceMs;

  /// Returns true on the first sample at/after the sustained threshold.
  /// Call [reset] after acting on a barge so repeats need fresh sustain.
  bool feed({required bool voice, required bool playing, required int nowMs}) {
    if (voice && playing) {
      _sinceMs ??= nowMs;
      return nowMs - _sinceMs! >= sustainMs;
    }
    _sinceMs = null;
    return false;
  }

  void reset() {
    _sinceMs = null;
  }
}
