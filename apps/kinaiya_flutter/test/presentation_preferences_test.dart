import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/presentation/preferences.dart';

void main() {
  test('old six-key snapshot migrates with prior values and default true', () {
    final restored = PresentationPreferences.fromJson({
      'reduced_motion': true,
      'show_character': false,
      'show_captions': false,
      'use_pack_palette': false,
      'primary_override': '#123456',
      'background_override': '#112233',
    });
    expect(restored.reducedMotion, isTrue);
    expect(restored.showCharacter, isFalse);
    expect(restored.showCaptions, isFalse);
    expect(restored.usePackPalette, isFalse);
    expect(restored.showConverseView, isTrue);
    expect(restored.animateTransitions, isTrue);
    expect(restored.speechMouthMotion, isTrue);
    expect(restored.primaryOverride, '#123456');
    expect(restored.backgroundOverride, '#112233');
    expect(restored.valid, isTrue);
  });

  test('new nine-key true/false roundtrip and copyWith', () {
    const base = PresentationPreferences();
    expect(base.showConverseView, isTrue);
    expect(base.speechMouthMotion, isTrue);

    final disabled = base.copyWith(showConverseView: false);
    expect(disabled.showConverseView, isFalse);
    expect(base.showConverseView, isTrue);

    final restoredFalse = PresentationPreferences.fromJson(disabled.toJson());
    expect(restoredFalse.showConverseView, isFalse);

    final restoredTrue = PresentationPreferences.fromJson(base.toJson());
    expect(restoredTrue.showConverseView, isTrue);
    expect(base.toJson()['show_converse_view'], isTrue);
    expect(disabled.toJson()['show_converse_view'], isFalse);
    final instant = disabled.copyWith(
      animateTransitions: false,
      speechMouthMotion: false,
    );
    expect(instant.toJson().length, 9);
    final restored = PresentationPreferences.fromJson(instant.toJson());
    expect(restored.animateTransitions, isFalse);
    expect(restored.speechMouthMotion, isFalse);
    expect(restored.showConverseView, isFalse);
    expect(base.animateTransitions, isTrue);
    expect(base.toJson()['speech_mouth_motion'], isTrue);
    expect(instant.toJson()['speech_mouth_motion'], isFalse);
    expect(instant.copyWith(showCaptions: false).speechMouthMotion, isFalse);
  });

  test('released seven-key snapshot retains visibility and enables fades', () {
    final previous =
        const PresentationPreferences(showConverseView: false).toJson()
          ..remove('animate_transitions')
          ..remove('speech_mouth_motion');
    final restored = PresentationPreferences.fromJson(previous);
    expect(restored.showConverseView, isFalse);
    expect(restored.animateTransitions, isTrue);
    expect(restored.speechMouthMotion, isTrue);
  });

  test(
    'released eight-key snapshot retains choices and enables mouth motion',
    () {
      final previous = const PresentationPreferences(
        animateTransitions: false,
        showConverseView: false,
        showCharacter: false,
        reducedMotion: true,
      ).toJson()..remove('speech_mouth_motion');
      final restored = PresentationPreferences.fromJson(previous);
      expect(restored.animateTransitions, isFalse);
      expect(restored.showConverseView, isFalse);
      expect(restored.showCharacter, isFalse);
      expect(restored.reducedMotion, isTrue);
      expect(restored.speechMouthMotion, isTrue);
    },
  );

  test('mouth motion accepts only booleans and complete preference shapes', () {
    final current = const PresentationPreferences().toJson();
    for (final malformed in [null, 0, 'true', <String>[]]) {
      expect(
        () => PresentationPreferences.fromJson({
          ...current,
          'speech_mouth_motion': malformed,
        }),
        throwsFormatException,
      );
    }
    for (final missing in ['animate_transitions', 'show_converse_view']) {
      final incomplete = Map<String, dynamic>.of(current)..remove(missing);
      expect(
        () => PresentationPreferences.fromJson(incomplete),
        throwsFormatException,
      );
    }
  });

  test(
    'transition preference is strict and requires the complete new shape',
    () {
      final current = const PresentationPreferences().toJson();
      for (final malformed in [null, 0, 'true', <String>[]]) {
        expect(
          () => PresentationPreferences.fromJson({
            ...current,
            'animate_transitions': malformed,
          }),
          throwsFormatException,
        );
      }
      expect(
        () =>
            PresentationPreferences.fromJson({...current, 'unexpected': false}),
        throwsFormatException,
      );
      final incomplete = Map<String, dynamic>.of(current)
        ..remove('show_converse_view');
      expect(
        () => PresentationPreferences.fromJson(incomplete),
        throwsFormatException,
      );
    },
  );

  test('rejects unknown keys, missing old fields, non-boolean new field', () {
    final validOld = {
      'reduced_motion': false,
      'show_character': true,
      'show_captions': true,
      'use_pack_palette': true,
      'primary_override': null,
      'background_override': null,
    };
    // Unknown extra key on old shape.
    expect(
      () => PresentationPreferences.fromJson({...validOld, 'extra': true}),
      throwsFormatException,
    );
    // Unknown extra key on new shape.
    expect(
      () => PresentationPreferences.fromJson({
        ...validOld,
        'show_converse_view': true,
        'extra': true,
      }),
      throwsFormatException,
    );
    // Missing old field in both shapes.
    final missing = Map<String, dynamic>.of(validOld)..remove('show_captions');
    expect(
      () => PresentationPreferences.fromJson(missing),
      throwsFormatException,
    );
    expect(
      () => PresentationPreferences.fromJson({
        ...missing,
        'show_converse_view': true,
      }),
      throwsFormatException,
    );
    // Non-boolean new field.
    expect(
      () => PresentationPreferences.fromJson({
        ...validOld,
        'show_converse_view': 'true',
      }),
      throwsFormatException,
    );
    expect(
      () => PresentationPreferences.fromJson({
        ...validOld,
        'show_converse_view': null,
      }),
      throwsFormatException,
    );
    // Malformed old field still refused.
    expect(
      () => PresentationPreferences.fromJson({
        ...validOld,
        'reduced_motion': 'false',
      }),
      throwsFormatException,
    );
  });
}
