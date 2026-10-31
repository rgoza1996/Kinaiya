/// Local microphone detection settings, separate from models and appearance.
library;

enum VadSensitivity {
  low('Lower · noisy room'),
  balanced('Balanced'),
  high('Higher · soft speech');

  const VadSensitivity(this.label);
  final String label;
}

class VadSettings {
  const VadSettings({
    this.adaptive = true,
    this.sensitivity = VadSensitivity.balanced,
    this.minSpeechMs = 300,
    this.bargeMs = 220,
    this.endSilenceMs = 1500,
  });

  final bool adaptive;
  final VadSensitivity sensitivity;
  final int minSpeechMs, bargeMs, endSilenceMs;

  bool get valid =>
      minSpeechMs >= 100 &&
      minSpeechMs <= 2000 &&
      bargeMs >= 100 &&
      bargeMs <= 2000 &&
      endSilenceMs >= 400 &&
      endSilenceMs <= 5000;

  VadSettings copyWith({
    bool? adaptive,
    VadSensitivity? sensitivity,
    int? minSpeechMs,
    int? bargeMs,
    int? endSilenceMs,
  }) => VadSettings(
    adaptive: adaptive ?? this.adaptive,
    sensitivity: sensitivity ?? this.sensitivity,
    minSpeechMs: minSpeechMs ?? this.minSpeechMs,
    bargeMs: bargeMs ?? this.bargeMs,
    endSilenceMs: endSilenceMs ?? this.endSilenceMs,
  );

  Map<String, Object> toBridge() => {
    'adaptive': adaptive,
    'sensitivity': sensitivity.name,
    'minSpeechMs': minSpeechMs,
    'bargeMs': bargeMs,
    'endSilenceMs': endSilenceMs,
  };

  factory VadSettings.fromBridge(Object? value) {
    if (value is! Map ||
        value['schemaVersion'] != 1 ||
        value['adaptive'] is! bool ||
        value['minSpeechMs'] is! int ||
        value['bargeMs'] is! int ||
        value['endSilenceMs'] is! int) {
      throw const FormatException('Invalid microphone settings');
    }
    final sensitivity = VadSensitivity.values
        .where((item) => item.name == value['sensitivity'])
        .firstOrNull;
    if (sensitivity == null) {
      throw const FormatException('Invalid microphone sensitivity');
    }
    final settings = VadSettings(
      adaptive: value['adaptive'] as bool,
      sensitivity: sensitivity,
      minSpeechMs: value['minSpeechMs'] as int,
      bargeMs: value['bargeMs'] as int,
      endSilenceMs: value['endSilenceMs'] as int,
    );
    if (!settings.valid) {
      throw const FormatException('Invalid microphone limits');
    }
    return settings;
  }
}
