import '../character/pack.dart' show isHexColor;

class PresentationPreferences {
  const PresentationPreferences({
    this.reducedMotion = false,
    this.animateTransitions = true,
    this.speechMouthMotion = true,
    this.intelligentUi = true,
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
  final bool intelligentUi;
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
    bool? intelligentUi,
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
    intelligentUi: intelligentUi ?? this.intelligentUi,
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
    'intelligent_ui': intelligentUi,
    'show_character': showCharacter,
    'show_captions': showCaptions,
    'use_pack_palette': usePackPalette,
    'show_converse_view': showConverseView,
    'primary_override': primaryOverride,
    'background_override': backgroundOverride,
  };

  static PresentationPreferences fromJson(Object? value) {
    // Released versions extend one complete prefix. Reject gaps/unknown keys
    // while preserving every older saved choice during additive migration.
    const baseKeys = {
      'reduced_motion',
      'show_character',
      'show_captions',
      'use_pack_palette',
      'primary_override',
      'background_override',
    };
    const additions = [
      'show_converse_view',
      'animate_transitions',
      'speech_mouth_motion',
      'intelligent_ui',
    ];
    if (value is! Map<String, dynamic> ||
        value.length < baseKeys.length ||
        value.length > baseKeys.length + additions.length) {
      throw const FormatException('Invalid presentation preferences.');
    }
    final count = value.length - baseKeys.length;
    final expected = {...baseKeys, ...additions.take(count)};
    if (!expected.every(value.containsKey) ||
        value.keys.any((key) => !expected.contains(key)) ||
        [
          'reduced_motion',
          'show_character',
          'show_captions',
          'use_pack_palette',
          ...additions.take(count),
        ].any((key) => value[key] is! bool) ||
        (value['primary_override'] != null &&
            value['primary_override'] is! String) ||
        (value['background_override'] != null &&
            value['background_override'] is! String)) {
      throw const FormatException('Invalid presentation preferences.');
    }
    final preferences = PresentationPreferences(
      reducedMotion: value['reduced_motion'] as bool,
      animateTransitions: value['animate_transitions'] as bool? ?? true,
      speechMouthMotion: value['speech_mouth_motion'] as bool? ?? true,
      intelligentUi: value['intelligent_ui'] as bool? ?? true,
      showCharacter: value['show_character'] as bool,
      showCaptions: value['show_captions'] as bool,
      usePackPalette: value['use_pack_palette'] as bool,
      showConverseView: value['show_converse_view'] as bool? ?? true,
      primaryOverride: value['primary_override'] as String?,
      backgroundOverride: value['background_override'] as String?,
    );
    if (!preferences.valid) {
      throw const FormatException('Invalid presentation preferences.');
    }
    return preferences;
  }
}
