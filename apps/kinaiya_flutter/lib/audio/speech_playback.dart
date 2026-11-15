/// Measurements of local output, independent of optional mouth/viseme cues.
library;

class SpeechPlaybackFrame {
  const SpeechPlaybackFrame({required this.rms, this.active = false});

  static const quiet = SpeechPlaybackFrame(rms: 0);

  /// RMS of the played PCM window, in the normalized sample range 0..1.
  final double rms;

  /// The output clock is inside a scheduled chunk in a running audio context.
  /// Neither this flag nor [rms] establishes physical audibility or delivery.
  final bool active;

  factory SpeechPlaybackFrame.fromJson(Object? value) {
    if (value is! Map || value['active'] != true) return quiet;
    final rms = value['rms'];
    if (rms is! num || !rms.isFinite || rms < 0 || rms > 1) return quiet;
    return SpeechPlaybackFrame(rms: rms.toDouble(), active: true);
  }
}

/// Optional read-only capability. Reading must not acquire a microphone,
/// resume an audio context, schedule audio or acknowledge speech delivery.
abstract interface class SpeechPlaybackAudio {
  Future<SpeechPlaybackFrame> playbackFrame();
}
