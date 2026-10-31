import '../character/pack.dart' show isHexColor;

class PresentationPreferences {
  const PresentationPreferences({
    this.reducedMotion = false,
    this.animateTransitions = true,
    this.speechMouthMotion = true,
    this.showCharacter = true,
    this.showCaptions = true,
    this.usePackPalette = true,
    this.showConverseView = true,
    this.primaryOverride,
    this.backgroundOverride,
  });

  final bool reducedMotion;
  final bool animateTransitions;
  final bool speechMouthMotion;
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
    bool? speechMouthMotion,
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
    speechMouthMotion: speechMouthMotion ?? this.speechMouthMotion,
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
    'speech_mouth_motion': speechMouthMotion,
    'show_character': showCharacter,
    'show_captions': showCaptions,
    'use_pack_palette': usePackPalette,
    'show_converse_view': showConverseView,
    'primary_override': primaryOverride,
    'background_override': backgroundOverride,
  };

  static PresentationPreferences fromJson(Object? value) {
    // Accept each complete released shape without discarding its choices.
    // Additive settings default on; incomplete or unknown shapes fail closed.
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
    const mouthMotionKey = 'speech_mouth_motion';
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
    final isTransitionsShape =
        value.length == oldKeys.length + 2 &&
        value.keys.every(
          (key) =>
              oldKeys.contains(key) ||
              key == converseKey ||
              key == transitionsKey,
        ) &&
        value.containsKey(converseKey) &&
        value.containsKey(transitionsKey);
    final isNewShape =
        value.length == oldKeys.length + 3 &&
        value.keys.every(
          (key) =>
              oldKeys.contains(key) ||
              key == converseKey ||
              key == transitionsKey ||
              key == mouthMotionKey,
        ) &&
        value.containsKey(converseKey) &&
        value.containsKey(transitionsKey) &&
        value.containsKey(mouthMotionKey);
    if ((!isOldShape &&
            !isConverseShape &&
            !isTransitionsShape &&
            !isNewShape) ||
        value['reduced_motion'] is! bool ||
        value['show_character'] is! bool ||
        value['show_captions'] is! bool ||
        value['use_pack_palette'] is! bool ||
        (!isOldShape && value[converseKey] is! bool) ||
        ((isTransitionsShape || isNewShape) &&
            value[transitionsKey] is! bool) ||
        (isNewShape && value[mouthMotionKey] is! bool) ||
        (value['primary_override'] != null &&
            value['primary_override'] is! String) ||
        (value['background_override'] != null &&
            value['background_override'] is! String)) {
      throw const FormatException('Invalid presentation preferences.');
    }
    final preferences = PresentationPreferences(
      reducedMotion: value['reduced_motion'] as bool,
      animateTransitions: isTransitionsShape || isNewShape
          ? value[transitionsKey] as bool
          : true,
      speechMouthMotion: isNewShape ? value[mouthMotionKey] as bool : true,
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
