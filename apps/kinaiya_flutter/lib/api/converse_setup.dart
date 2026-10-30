/// Host declarations describe configuration, never model residency or consent.
library;

class ConverseModelRole {
  const ConverseModelRole({
    required this.label,
    required this.configured,
    required this.reason,
    required this.source,
  });
  final String? label;
  final bool configured;
  final String reason, source;
}

class ConverseSetup {
  const ConverseSetup({required this.roles, required this.reasoning});
  final Map<String, ConverseModelRole> roles;
  final String reasoning;

  static ConverseSetup fromJson(Map<String, Object?> data) {
    Never invalid() => throw const FormatException('Invalid model setup.');
    String text(Object? value, {int limit = 256}) {
      if (value is! String ||
          value.isEmpty ||
          value.length > limit ||
          value.runes.any((c) => c < 32 || c == 127)) {
        invalid();
      }
      return value;
    }

    if (data['contract_version'] != 1 ||
        data['revision'] is! String ||
        !RegExp(r'^[0-9a-f]{64}$').hasMatch(data['revision'] as String)) {
      invalid();
    }
    final raw = data['roles'];
    if (raw is! Map || raw.length != 4) invalid();
    final roles = <String, ConverseModelRole>{};
    for (final name in ['conversation', 'stt', 'tts', 'decisions']) {
      final role = raw[name];
      if (role is! Map ||
          role['configured'] is! bool ||
          role['mutable'] != false ||
          !const ['runtime', 'host_configuration'].contains(role['source'])) {
        invalid();
      }
      final configured = role['configured'] as bool;
      final options = role['options'];
      if (options is! List || options.length != (configured ? 1 : 0)) invalid();
      if (configured) {
        text(role['selected_id']);
        text(role['label']);
        final option = options.single;
        if (option is! Map ||
            option['id'] != role['selected_id'] ||
            option['label'] != role['label']) {
          invalid();
        }
      } else if (role['selected_id'] != null || role['label'] != null) {
        invalid();
      }
      roles[name] = ConverseModelRole(
        label: role['label'] as String?,
        configured: configured,
        source: role['source'] as String,
        reason: text(role['reason'], limit: 512),
      );
    }
    final inputs = data['inputs'];
    final reasoning = data['reasoning'];
    bool textOnly(Object? value) =>
        value is List && value.length == 1 && value.single == 'text';
    if (inputs is! Map ||
        !textOnly(inputs['supported']) ||
        !textOnly(inputs['selected']) ||
        inputs['direct_audio'] != false ||
        inputs['stt_required'] != true ||
        reasoning is! Map ||
        reasoning['mutable'] != false) {
      invalid();
    }
    final selected = text(reasoning['selected']);
    final levels = reasoning['options'];
    // A host may declare an adapter-specific level. This is a bounded literal
    // label, never a grant to enable unsupported frontend choices.
    if (levels is! List || levels.length != 1 || levels.single != selected) {
      invalid();
    }
    return ConverseSetup(roles: Map.unmodifiable(roles), reasoning: selected);
  }
}
