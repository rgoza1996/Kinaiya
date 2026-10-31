/// Locally sampled mouth state, aligned to the speech playback clock.
library;

/// Optional chunk-relative cues use these symbolic visemes. Providers without
/// cues continue to use the played PCM's volume; these are not phoneme guesses.
const speechVisemes = <String>{
  'sil',
  'PP',
  'FF',
  'TH',
  'DD',
  'kk',
  'CH',
  'SS',
  'nn',
  'RR',
  'aa',
  'E',
  'ih',
  'oh',
  'ou',
};

class SpeechMouthFrame {
  const SpeechMouthFrame({
    required this.openness,
    this.viseme,
    this.active = false,
  });

  static const closed = SpeechMouthFrame(openness: 0);

  /// Smoothed amplitude or provider cue openness, between zero and one.
  final double openness;
  final String? viseme;

  /// True only while the playback clock is inside a scheduled audio chunk.
  /// Silence within that chunk still has zero [openness].
  final bool active;

  /// Invalid bridge responses fail to a closed mouth instead of stale speech.
  factory SpeechMouthFrame.fromJson(Object? value) {
    if (value is! Map || value['active'] != true) return closed;
    final openness = value['openness'];
    if (openness is! num || !openness.isFinite) return closed;
    final viseme = value['viseme'];
    return SpeechMouthFrame(
      openness: openness.clamp(0, 1).toDouble(),
      viseme: viseme is String && speechVisemes.contains(viseme)
          ? viseme
          : null,
      active: true,
    );
  }
}

/// Optional capability so existing audio implementations/fakes stay compatible.
abstract interface class SpeechMouthAudio {
  Future<SpeechMouthFrame> mouthFrame();

  /// Cues are at most 256 ordered, non-overlapping
  /// `{start_ms, end_ms, viseme}` entries relative to this PCM chunk. Malformed
  /// or unavailable cues fall back to the chunk's amplitude envelope.
  Future<bool> queuePcmWithVisemes(String base64, int sampleRate, Object? cues);
}
