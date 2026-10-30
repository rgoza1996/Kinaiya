import '../character/pack.dart' show isHexColor;

class PresentationPreferences {
  const PresentationPreferences({
    this.reducedMotion = false,
    this.animateTransitions = true,
    this.showCharacter = true,
    this.showCaptions = true,
    this.usePackPalette = true,
    this.showConverseView = true,
    this.primaryOverride,
    this.backgroundOverride,
  });

  final bool reducedMotion;
  final bool animateTransitions;
  final bool showCharacter;
  final bool showCaptions;
  final bool usePackPalette;
  // Optional navigation presentation only; never toggles runtime enabled,
  // character visibility, captions, microphone, or provider state.
  final bool showConverseView;
  final String? primaryOverride;
  final String? backgroundOverride;

  PresentationPreferences copyWith({
    bool? reducedMotion,
    bool? animateTransitions,
    bool? showCharacter,
    bool? showCaptions,
    bool? usePackPalette,
    bool? showConverseView,
    String? primaryOverride,
    String? backgroundOverride,
    bool clearOverrides = false,
  }) => PresentationPreferences(
    reducedMotion: reducedMotion ?? this.reducedMotion,
    animateTransitions: animateTransitions ?? this.animateTransitions,
    showCharacter: showCharacter ?? this.showCharacter,
    showCaptions: showCaptions ?? this.showCaptions,
    usePackPalette: usePackPalette ?? this.usePackPalette,
    showConverseView: showConverseView ?? this.showConverseView,
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
    'animate_transitions': animateTransitions,
    'show_character': showCharacter,
    'show_captions': showCaptions,
    'use_pack_palette': usePackPalette,
    'show_converse_view': showConverseView,
    'primary_override': primaryOverride,
    'background_override': backgroundOverride,
  };

  static PresentationPreferences fromJson(Object? value) {
    // Read the two released shapes without discarding their choices. New writes
    // include both additive preferences; incomplete or unknown shapes fail closed.
    const oldKeys = {
      'reduced_motion',
      'show_character',
      'show_captions',
      'use_pack_palette',
      'primary_override',
      'background_override',
    };
    const converseKey = 'show_converse_view';
    const transitionsKey = 'animate_transitions';
    if (value is! Map<String, dynamic>) {
      throw const FormatException('Invalid presentation preferences.');
    }
    final isOldShape =
        value.length == oldKeys.length &&
        value.keys.every((key) => oldKeys.contains(key));
    final isConverseShape =
        value.length == oldKeys.length + 1 &&
        value.keys.every(
          (key) => oldKeys.contains(key) || key == converseKey,
        ) &&
        value.containsKey(converseKey);
    final isNewShape =
        value.length == oldKeys.length + 2 &&
        value.keys.every(
          (key) =>
              oldKeys.contains(key) ||
              key == converseKey ||
              key == transitionsKey,
        ) &&
        value.containsKey(converseKey) &&
        value.containsKey(transitionsKey);
    if ((!isOldShape && !isConverseShape && !isNewShape) ||
        value['reduced_motion'] is! bool ||
        value['show_character'] is! bool ||
        value['show_captions'] is! bool ||
        value['use_pack_palette'] is! bool ||
        (!isOldShape && value[converseKey] is! bool) ||
        (isNewShape && value[transitionsKey] is! bool) ||
        (value['primary_override'] != null &&
            value['primary_override'] is! String) ||
        (value['background_override'] != null &&
            value['background_override'] is! String)) {
      throw const FormatException('Invalid presentation preferences.');
    }
    final preferences = PresentationPreferences(
      reducedMotion: value['reduced_motion'] as bool,
      animateTransitions: isNewShape ? value[transitionsKey] as bool : true,
      showCharacter: value['show_character'] as bool,
      showCaptions: value['show_captions'] as bool,
      usePackPalette: value['use_pack_palette'] as bool,
      showConverseView: !isOldShape ? value[converseKey] as bool : true,
      primaryOverride: value['primary_override'] as String?,
      backgroundOverride: value['background_override'] as String?,
    );
    if (!preferences.valid) {
      throw const FormatException('Invalid presentation preferences.');
    }
    return preferences;
  }
}
