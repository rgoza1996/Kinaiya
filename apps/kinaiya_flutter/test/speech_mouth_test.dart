import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/audio/speech_audio.dart';

void main() {
  test('mouth frames retain valid canonical cue and bound openness', () {
    final frame = SpeechMouthFrame.fromJson({
      'active': true,
      'openness': 0.5,
      'viseme': 'PP',
    });
    expect(frame.active, isTrue);
    expect(frame.openness, 0.5);
    expect(frame.viseme, 'PP');
    expect(
      SpeechMouthFrame.fromJson({'active': true, 'openness': 4}).openness,
      1,
    );
    expect(
      SpeechMouthFrame.fromJson({'active': true, 'openness': -4}).openness,
      0,
    );
  });

  test('inactive and malformed frames fail closed without stale visemes', () {
    for (final value in <Object?>[
      null,
      [],
      'invalid',
      {'active': false, 'openness': 1, 'viseme': 'aa'},
      {'active': 'true', 'openness': 1},
      {'active': true, 'openness': '1'},
      {'active': true, 'openness': double.nan},
      {'active': true, 'openness': double.infinity},
    ]) {
      expect(SpeechMouthFrame.fromJson(value), same(SpeechMouthFrame.closed));
    }
  });

  test('unknown viseme keeps valid amplitude fallback', () {
    final frame = SpeechMouthFrame.fromJson({
      'active': true,
      'openness': 0.75,
      'viseme': 'invented',
    });
    expect(frame.openness, 0.75);
    expect(frame.active, isTrue);
    expect(frame.viseme, isNull);
  });
}
