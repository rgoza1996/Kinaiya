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
    expect(restored.primaryOverride, '#123456');
    expect(restored.backgroundOverride, '#112233');
    expect(restored.valid, isTrue);
  });

  test('new seven-key true/false roundtrip and copyWith', () {
    const base = PresentationPreferences();
    expect(base.showConverseView, isTrue);

    final disabled = base.copyWith(showConverseView: false);
    expect(disabled.showConverseView, isFalse);
    expect(base.showConverseView, isTrue);

    final restoredFalse = PresentationPreferences.fromJson(disabled.toJson());
    expect(restoredFalse.showConverseView, isFalse);

    final restoredTrue = PresentationPreferences.fromJson(base.toJson());
    expect(restoredTrue.showConverseView, isTrue);
    expect(base.toJson()['show_converse_view'], isTrue);
    expect(disabled.toJson()['show_converse_view'], isFalse);
  });

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
