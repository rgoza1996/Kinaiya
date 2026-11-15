import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/audio/speech_audio.dart';

void main() {
  test('played RMS is independent of optional mouth cues', () {
    final frame = SpeechPlaybackFrame.fromJson({
      'active': true,
      'rms': .035,
      'viseme': 'PP',
      'openness': 0,
    });
    expect(frame.active, isTrue);
    expect(frame.rms, .035);
    expect(SpeechPlaybackFrame.fromJson({'active': true, 'rms': 0}).rms, 0);
  });

  test(
    'invalid or inactive measurements quiet instead of retaining activity',
    () {
      for (final value in <Object?>[
        null,
        [],
        'invalid',
        {},
        {'active': false, 'rms': 1},
        {'active': 'true', 'rms': 1},
        {'active': true, 'rms': '1'},
        for (final rms in [-1, 1.01, double.nan, double.infinity])
          {'active': true, 'rms': rms},
      ]) {
        expect(
          SpeechPlaybackFrame.fromJson(value),
          same(SpeechPlaybackFrame.quiet),
        );
      }
    },
  );
}
