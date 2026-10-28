import '../character/pack.dart' show isHexColor;

class PresentationPreferences {
  const PresentationPreferences({
    this.reducedMotion = false,
    this.showCharacter = true,
    this.showCaptions = true,
    this.usePackPalette = true,
    this.primaryOverride,
    this.backgroundOverride,
  });

  final bool reducedMotion;
  final bool showCharacter;
  final bool showCaptions;
  final bool usePackPalette;
  final String? primaryOverride;
  final String? backgroundOverride;

  PresentationPreferences copyWith({
    bool? reducedMotion,
    bool? showCharacter,
    bool? showCaptions,
    bool? usePackPalette,
    String? primaryOverride,
    String? backgroundOverride,
    bool clearOverrides = false,
  }) => PresentationPreferences(
    reducedMotion: reducedMotion ?? this.reducedMotion,
    showCharacter: showCharacter ?? this.showCharacter,
    showCaptions: showCaptions ?? this.showCaptions,
    usePackPalette: usePackPalette ?? this.usePackPalette,
    primaryOverride: clearOverrides
        ? null
        : primaryOverride ?? this.primaryOverride,
    backgroundOverride: clearOverrides
        ? null
        : backgroundOverride ?? this.backgroundOverride,
  );

  bool get valid =>
      (primaryOverride == null || isHexColor(primaryOverride!)) &&
      (backgroundOverride == null || isHexColor(backgroundOverride!));

  Map<String, dynamic> toJson() => {
    'reduced_motion': reducedMotion,
    'show_character': showCharacter,
    'show_captions': showCaptions,
    'use_pack_palette': usePackPalette,
    'primary_override': primaryOverride,
    'background_override': backgroundOverride,
  };

  static PresentationPreferences fromJson(Object? value) {
    const keys = {
      'reduced_motion',
      'show_character',
      'show_captions',
      'use_pack_palette',
      'primary_override',
      'background_override',
    };
    if (value is! Map<String, dynamic> ||
        value.length != keys.length ||
        value.keys.any((key) => !keys.contains(key)) ||
        value['reduced_motion'] is! bool ||
        value['show_character'] is! bool ||
        value['show_captions'] is! bool ||
        value['use_pack_palette'] is! bool ||
        (value['primary_override'] != null &&
            value['primary_override'] is! String) ||
        (value['background_override'] != null &&
            value['background_override'] is! String)) {
      throw const FormatException('Invalid presentation preferences.');
    }
    final preferences = PresentationPreferences(
      reducedMotion: value['reduced_motion'] as bool,
      showCharacter: value['show_character'] as bool,
      showCaptions: value['show_captions'] as bool,
      usePackPalette: value['use_pack_palette'] as bool,
      primaryOverride: value['primary_override'] as String?,
      backgroundOverride: value['background_override'] as String?,
    );
    if (!preferences.valid) {
      throw const FormatException('Invalid presentation preferences.');
    }
    return preferences;
  }
}
