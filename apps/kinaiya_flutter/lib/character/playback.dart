/// Deterministic local presentation. Signals carry confirmed runtime facts;
/// neither variants nor fallback animations infer emotions or task activity.
library;

import 'pack.dart';

class CharacterSignals {
  const CharacterSignals({
    this.speaking = false,
    this.listening = false,
    this.warming = false,
    this.working = false,
    this.blocked = false,
    this.activity = 'idle',
  });
  final bool speaking, listening, warming, working, blocked;
  final String activity;

  String get semantic {
    if (speaking) return 'speaking';
    if (listening) return 'listening';
    if (activity == 'alert.critical') return activity;
    if (warming) return 'warming';
    if (blocked) return 'blocked';
    if (activity != 'idle') {
      return characterSemanticIds.contains(activity) ? activity : 'idle';
    }
    return working ? 'working.generic' : 'idle';
  }

  Set<String> get conditions => Set.unmodifiable({
    if (speaking) 'speaking',
    if (listening) 'listening',
    if (warming) 'warming',
    if (working || activity.startsWith('working.')) 'working',
    if (blocked) 'blocked',
    if (!speaking && !listening) 'quiet',
  });
}

class CharacterFrame {
  const CharacterFrame({
    required this.animationId,
    required this.semantic,
    required this.animationElapsed,
    required this.approach,
  });
  final String animationId;

  /// Requested semantic remains honest even during hysteresis or fallback.
  final String semantic;
  final Duration animationElapsed;
  final double approach;
}

class _Choice {
  const _Choice(this.animation, [this.variant, this.key]);
  final String animation;
  final PackVariant? variant;
  final String? key;
}

class CharacterPlayback {
  CharacterPlayback(this.pack);
  final CharacterPack pack;

  // At most one timestamp per declared variant (64 in validated packs).
  final Map<String, int> _recent = {};
  Duration _lastNow = Duration.zero;
  Duration _started = Duration.zero;
  Duration? _warmingStarted;
  Duration? _workEntered;
  String? _activeSemantic;
  String? _requestedSemantic;
  _Choice? _choice;
  int _ordinal = 0;

  Duration _clock(Duration now) {
    if (now < _lastNow) return _lastNow;
    _lastNow = now;
    return now;
  }

  /// Accessibility can restart a looping display phase without clearing
  /// variant identity, cooldowns or work hysteresis. One-shots stay clamped.
  void restartAnimation(Duration now) {
    final time = _clock(now);
    if (pack.animations[_choice?.animation]?.loop ?? false) _started = time;
  }

  CharacterFrame update(
    CharacterSignals signals,
    Duration now, {
    bool reducedMotion = false,
  }) {
    final time = _clock(now);
    final requested = signals.semantic;
    final conditions = signals.conditions;
    if (requested == 'warming') {
      if (_requestedSemantic != 'warming') _warmingStarted = time;
    } else {
      _warmingStarted = null;
    }
    _requestedSemantic = requested;

    var desired = requested;
    if (!reducedMotion &&
        _activeSemantic != null &&
        desired != _activeSemantic &&
        _activeSemantic!.startsWith('working.') &&
        desired.startsWith('working.') &&
        _workEntered != null &&
        time - _workEntered! < const Duration(milliseconds: 650)) {
      desired = _activeSemantic!;
    }
    final changed = desired != _activeSemantic;
    final ineligible =
        _choice?.variant?.when.any(
          (condition) => !conditions.contains(condition),
        ) ??
        false;
    final current = pack.animations[_choice?.animation];
    final duration = _cycleDuration(current);
    final elapsed = time - _started;
    final boundary =
        !reducedMotion && current?.loop == true && elapsed >= duration;
    if (_choice == null || changed || ineligible || boundary) {
      final oldChoice = _choice;
      final choice = _choose(desired, conditions, time);
      // One observed boundary makes one decision. Hidden/delayed frames never
      // replay skipped cycles or consume a burst of cooldown selections.
      if (boundary &&
          !changed &&
          !ineligible &&
          oldChoice?.animation == choice.animation &&
          oldChoice?.key == choice.key) {
        _started +=
            duration * (elapsed.inMicroseconds ~/ duration.inMicroseconds);
      } else {
        _started = time;
      }
      _choice = choice;
      // Repeating a cycle within one work activity does not extend this hold:
      // otherwise short looping animations could starve the pending activity.
      if (changed && desired.startsWith('working.')) _workEntered = time;
      if (!desired.startsWith('working.')) _workEntered = null;
      _activeSemantic = desired;
    }

    final approach = _warmingStarted == null
        ? 1.0
        : ((time - _warmingStarted!).inMicroseconds / 1200000).clamp(0.0, 1.0);
    return CharacterFrame(
      animationId: _choice!.animation,
      semantic: requested,
      animationElapsed: time - _started,
      approach: approach,
    );
  }

  Duration _cycleDuration(PackAnimation? animation) {
    final micros =
        (animation?.frames.length ?? 1) * (animation?.frameMs ?? 1) * 1000;
    return Duration(microseconds: micros > 0 ? micros : 1);
  }

  _Choice _choose(String semantic, Set<String> conditions, Duration now) {
    _ordinal = (_ordinal + 1) & 0x7fffffff;
    if (pack.version != packSchemaV3) {
      return _Choice(pack.resolveAnimationId(semantic));
    }
    final visited = <String>{};
    _Choice? visit(String current) {
      if (!visited.add(current)) return null;
      final variants = pack.activities[current];
      if (variants != null) {
        final eligible = <int>[];
        var totalWeight = 0;
        for (var i = 0; i < variants.length; i++) {
          final variant = variants[i];
          final selected = _recent['$current:$i'];
          if (variant.when.every(conditions.contains) &&
              (selected == null ||
                  now.inMicroseconds - selected >= variant.cooldownMs * 1000)) {
            eligible.add(i);
            totalWeight += variant.weight;
          }
        }
        if (totalWeight > 0) {
          var pick =
              _stableHash(
                '${pack.packId}\u0000${pack.packVersion}\u0000$current\u0000$_ordinal',
              ) %
              totalWeight;
          for (final index in eligible) {
            final variant = variants[index];
            if (pick < variant.weight) {
              final key = '$current:$index';
              _recent[key] = now.inMicroseconds;
              return _Choice(variant.animation, variant, key);
            }
            pick -= variant.weight;
          }
        }
      }
      final explicit = pack.fallbacks[current];
      if (explicit != null) {
        final chosen = visit(explicit);
        if (chosen != null) return chosen;
      }
      if (variants == null &&
          const {
            'idle',
            'listening',
            'thinking',
            'speaking',
          }.contains(current) &&
          pack.animations.containsKey(current)) {
        return _Choice(current);
      }
      final standard = characterStandardFallbacks[current];
      return standard == null ? null : visit(standard);
    }

    final choice = visit(semantic);
    if (choice != null) return choice;
    // A validated pack always has its declared fallback and four core states.
    // The final branch also keeps hand-constructed legacy fixtures harmless.
    if (pack.animations.containsKey(pack.fallbackAnimation)) {
      return _Choice(pack.fallbackAnimation);
    }
    if (pack.animations.containsKey('idle')) return const _Choice('idle');
    return _Choice(pack.animations.keys.firstOrNull ?? '');
  }

  // Products remain below 2^53 for equal results on Dart VM and JavaScript.
  int _stableHash(String value) {
    var hash = 5381;
    for (final code in value.codeUnits) {
      hash = ((hash * 33) ^ code) & 0x7fffffff;
    }
    return hash;
  }
}
