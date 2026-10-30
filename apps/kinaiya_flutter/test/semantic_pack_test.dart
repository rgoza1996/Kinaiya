import 'dart:convert';

import 'package:crypto/crypto.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/character/pack.dart';

import 'mascot_sprites_test.dart' as fixtures;

Map<String, dynamic> _variant({
  String animation = 'idle',
  int weight = 1,
  int cooldownMs = 0,
}) => {'animation': animation, 'weight': weight, 'cooldown_ms': cooldownMs};

Map<String, dynamic> _manifest() =>
    jsonDecode(
          fixtures.buildV2(
            fixtures.makePng(8, 8),
            topOverrides: {
              'pack_schema': packSchemaV3,
              'activities': {
                'idle': [_variant()],
                'working.write_code': [
                  {
                    ..._variant(
                      animation: 'thinking',
                      weight: 100,
                      cooldownMs: 60000,
                    ),
                    'when': ['working', 'quiet'],
                  },
                ],
              },
              'fallbacks': {'blocked': 'working.read_email'},
            },
          ),
        )
        as Map<String, dynamic>;

Future<CharacterPack> _load(Map<String, dynamic> manifest) => loadCharacterPack(
  manifestJson: jsonEncode(manifest),
  bundle: fixtures.FakeBundle(const {}, {
    fixtures.kSheetAsset: fixtures.makePng(8, 8),
    fixtures.kFallbackAsset: fixtures.makePng(2, 2),
  }),
);

Map<String, dynamic> _workVariant(Map<String, dynamic> manifest) =>
    ((manifest['activities'] as Map)['working.write_code'] as List).single
        as Map<String, dynamic>;

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('bundled seated idle keeps all non-idle atlas identities', () async {
    final pack = await loadCharacterPack();
    expect(pack.fallback, isFalse, reason: pack.note);
    expect(pack.sheets.keys.toSet(), {'core', 'activity', 'seated_idle'});
    expect(pack.sheets['seated_idle']!.capacity, 4);
    expect(pack.sheets['seated_idle']!.width, 1254);
    expect(pack.sheets['seated_idle']!.height, 1254);
    expect(pack.sheets['seated_idle']!.frameWidth, 627);
    expect(pack.sheets['seated_idle']!.frameHeight, 576);
    expect(
      pack.sheets['seated_idle']!.sha256,
      '1322c7fa9ba76ad0cbc6ac8409564d5247d0366bf53fe368f9a39338ac938259',
    );
    expect(pack.sheets.values.fold(0, (n, s) => n + s.capacity), 36);
    for (final alias in ['idle', 'idle_blink']) {
      final animation = pack.animations[alias]!;
      expect(animation.sheet, 'seated_idle');
      expect(
        animation.frames,
        alias == 'idle' ? [0, 0, 0, 0, 0, 2, 0, 0] : [0, 2, 0],
      );
      expect(animation.frames.toSet(), {0, 2});
      expect(animation.frameMs, alias == 'idle' ? 280 : 220);
      expect(animation.loop, isTrue);
    }
    final unchanged = {
      'listening': ('core', 4, 250),
      'thinking': ('core', 8, 300),
      'speaking': ('core', 12, 140),
      'generic_work': ('activity', 0, 300),
      'waiting': ('activity', 4, 320),
      'important': ('activity', 8, 220),
      'critical': ('activity', 12, 180),
    };
    for (final entry in unchanged.entries) {
      final animation = pack.animations[entry.key]!;
      final (sheet, first, ms) = entry.value;
      expect(animation.sheet, sheet);
      expect(animation.frames, [
        first,
        first + 1,
        first + 2,
        first + 3,
        first + 2,
        first + 1,
      ]);
      expect(animation.frameMs, ms);
      expect(animation.loop, isTrue);
    }
    expect(pack.animations, hasLength(9));
    expect(pack.fallbackAnimation, 'idle');
    expect(pack.anchorX, .5);
    expect(pack.anchorY, .9);
    const originals = {
      'core':
          '4eabec3223bc1a921645ce086a1f9fbf8b0098e66c1017e73af826197ff39412',
      'activity':
          'c89deb785e27cb8a741fbcf697b991e46f9b03ab2256342a2c340d555761d4c2',
    };
    for (final entry in originals.entries) {
      final sheet = pack.sheets[entry.key]!;
      final bytes = await rootBundle.load(sheet.asset);
      expect(sheet.sha256, entry.value);
      expect(
        sha256
            .convert(
              bytes.buffer.asUint8List(
                bytes.offsetInBytes,
                bytes.lengthInBytes,
              ),
            )
            .toString(),
        entry.value,
      );
    }
  });

  test(
    'schema 3 retains all required spritesheet metadata and semantics',
    () async {
      final pack = await _load(_manifest());
      expect(pack.fallback, isFalse, reason: pack.note);
      expect(pack.version, packSchemaV3);
      expect(pack.isSpritesheet, isTrue);
      expect(pack.packId, 'kinaiya-core');
      expect(pack.packVersion, '1.0.0-test');
      expect(pack.author, 'Kinaiya test');
      expect(pack.license, 'All rights reserved (test)');
      expect(pack.provenance!.generator, 'test-generator');
      expect(pack.themePrimary, '#141091');
      expect(pack.themeBackground, '#FFFFFF');
      expect(pack.anchorX, 0.5);
      expect(pack.anchorY, 0.9);
      expect(pack.scaleMin, 0.5);
      expect(pack.scaleMax, 2);
      expect(
        pack.animations.keys,
        containsAll(['idle', 'listening', 'thinking', 'speaking']),
      );
      expect(pack.sheets['core']!.capacity, 4);
      expect(pack.fallbackAnimation, 'idle');
      final variant = pack.activities['working.write_code']!.single;
      expect(variant.animation, 'thinking');
      expect(variant.weight, 100);
      expect(variant.cooldownMs, 60000);
      expect(variant.when, {'working', 'quiet'});
      expect(pack.fallbacks, {'blocked': 'working.read_email'});
    },
  );

  test(
    'schema 1 and 2 fixtures remain accepted without semantic fields',
    () async {
      final pose = fixtures.makePng(2, 2);
      final legacy = await _load(
        jsonDecode(fixtures.buildV1(pose)) as Map<String, dynamic>,
      );
      expect(legacy.fallback, isFalse, reason: legacy.note);
      expect(legacy.version, packSchemaVersion);
      expect(legacy.poses, hasLength(1));
      expect(legacy.activities, isEmpty);
      expect(legacy.fallbacks, isEmpty);
      final v2 = await _load(
        jsonDecode(fixtures.buildV2(fixtures.makePng(8, 8)))
            as Map<String, dynamic>,
      );
      expect(v2.fallback, isFalse, reason: v2.note);
      expect(v2.version, packSchemaV2);
      expect(v2.isSpritesheet, isTrue);
      expect(v2.activities, isEmpty);
      expect(v2.fallbacks, isEmpty);
    },
  );

  for (final schema in [packSchemaVersion, packSchemaV2]) {
    for (final key in ['activities', 'fallbacks']) {
      test('schema $schema rejects $key even when empty or null', () async {
        for (final value in [<String, dynamic>{}, null]) {
          final manifest = schema == packSchemaVersion
              ? jsonDecode(fixtures.buildV1(fixtures.makePng(2, 2)))
                    as Map<String, dynamic>
              : jsonDecode(fixtures.buildV2(fixtures.makePng(8, 8)))
                    as Map<String, dynamic>;
          manifest[key] = value;
          final pack = await _load(manifest);
          expect(pack.fallback, isTrue);
          expect(pack.note, isNotEmpty);
        }
      });
    }
  }

  for (final field in [
    'pack_id',
    'version',
    'name',
    'author',
    'license',
    'provenance',
    'theme',
    'voice_profile',
    'fallback_animation',
    'anchor',
    'scale_limits',
    'sheets',
    'animations',
    'activities',
    'fallbacks',
  ]) {
    test('schema 3 requires $field', () async {
      final manifest = _manifest()..remove(field);
      final pack = await _load(manifest);
      expect(pack.fallback, isTrue);
      expect(pack.note, isNotEmpty);
    });
  }

  final invalid = <String, void Function(Map<String, dynamic>)>{
    'unknown manifest key': (m) => m['code'] = 'untrusted',
    'unknown provenance key': (m) =>
        (m['provenance'] as Map)['permissions'] = [],
    'unknown theme key': (m) => (m['theme'] as Map)['voice'] = 'untrusted',
    'unknown sheet key': (m) =>
        ((m['sheets'] as List).first as Map)['url'] = 'untrusted',
    'unknown animation key': (m) =>
        ((m['animations'] as Map)['idle'] as Map)['script'] = 'untrusted',
    'unknown variant key': (m) => _workVariant(m)['tool'] = 'untrusted',
    'unknown activity': (m) =>
        (m['activities'] as Map)['private.reasoning'] = [_variant()],
    'nonmap activities': (m) => m['activities'] = [],
    'nonlist variants': (m) => (m['activities'] as Map)['idle'] = {},
    'empty variants': (m) => (m['activities'] as Map)['idle'] = [],
    'nonobject variant': (m) => (m['activities'] as Map)['idle'] = ['idle'],
    'missing animation': (m) => _workVariant(m).remove('animation'),
    'unknown animation': (m) => _workVariant(m)['animation'] = 'missing',
    'nonstring animation': (m) => _workVariant(m)['animation'] = 1,
    'missing weight': (m) => _workVariant(m).remove('weight'),
    'noninteger weight': (m) => _workVariant(m)['weight'] = 1.0,
    'zero weight': (m) => _workVariant(m)['weight'] = 0,
    'excess weight': (m) => _workVariant(m)['weight'] = 101,
    'missing cooldown': (m) => _workVariant(m).remove('cooldown_ms'),
    'noninteger cooldown': (m) => _workVariant(m)['cooldown_ms'] = 1.0,
    'negative cooldown': (m) => _workVariant(m)['cooldown_ms'] = -1,
    'excess cooldown': (m) => _workVariant(m)['cooldown_ms'] = 60001,
    'null conditions': (m) => _workVariant(m)['when'] = null,
    'nonlist conditions': (m) => _workVariant(m)['when'] = 'quiet',
    'nonstring condition': (m) => _workVariant(m)['when'] = [true],
    'unknown condition': (m) => _workVariant(m)['when'] = ['authorized'],
    'duplicate conditions': (m) =>
        _workVariant(m)['when'] = ['working', 'working'],
    'excess conditions': (m) =>
        _workVariant(m)['when'] = [...characterConditionIds, 'quiet'],
    'nonmap fallbacks': (m) => m['fallbacks'] = [],
    'unknown fallback source': (m) => m['fallbacks'] = {'unknown': 'idle'},
    'unknown fallback target': (m) => m['fallbacks'] = {'blocked': 'unknown'},
    'nonstring fallback target': (m) => m['fallbacks'] = {
      'blocked': {'activity': 'idle'},
    },
    'self fallback': (m) => m['fallbacks'] = {'blocked': 'blocked'},
    'two-node fallback cycle': (m) =>
        m['fallbacks'] = {'blocked': 'warming', 'warming': 'blocked'},
    'cycle through standard generic work edge': (m) =>
        m['fallbacks'] = {'thinking': 'working.generic'},
    'cycle through standard multi-hop work edges': (m) =>
        m['fallbacks'] = {'thinking': 'working.write_code'},
    'cycle through standard blocked edge': (m) =>
        m['fallbacks'] = {'idle': 'blocked'},
    'long fallback cycle': (m) => m['fallbacks'] = {
      'blocked': 'warming',
      'warming': 'alert.important',
      'alert.important': 'working.generic',
      'working.generic': 'working.reason',
      'working.reason': 'blocked',
    },
    '17 activity mappings': (m) => m['activities'] = {
      for (final semantic in characterSemanticIds) semantic: [_variant()],
      'unknown.a': [_variant()],
      'unknown.b': [_variant()],
      'unknown.c': [_variant()],
    },
    '17 fallback entries': (m) => m['fallbacks'] = {
      for (final semantic in characterSemanticIds) semantic: 'idle',
      'unknown.a': 'idle',
      'unknown.b': 'idle',
      'unknown.c': 'idle',
    },
    '9 variants in one activity': (m) =>
        m['activities'] = {'idle': List.generate(9, (_) => _variant())},
    '65 total variants': (m) => m['activities'] = {
      for (final semantic in characterSemanticIds.take(8))
        semantic: List.generate(8, (_) => _variant()),
      'working.reason': [_variant()],
    },
  };
  for (final entry in invalid.entries) {
    test('direct schema 3 loader rejects ${entry.key}', () async {
      final manifest = _manifest();
      entry.value(manifest);
      final pack = await _load(manifest);
      expect(pack.fallback, isTrue);
      expect(pack.isSpritesheet, isFalse);
      expect(pack.note, isNotEmpty);
      expect(pack.activities, isEmpty);
      expect(pack.fallbacks, isEmpty);
    });
  }

  test('all known activity IDs are accepted within the mapping cap', () async {
    // The closed vocabulary contains 14 IDs, fewer than the 16-entry budget.
    final manifest = _manifest();
    manifest['activities'] = {
      for (final semantic in characterSemanticIds) semantic: [_variant()],
    };
    final pack = await _load(manifest);
    expect(pack.fallback, isFalse, reason: pack.note);
    expect(pack.activities.keys.toSet(), characterSemanticIds);
  });

  test('8 variants per mapping and 64 total variants are accepted', () async {
    final manifest = _manifest();
    manifest['activities'] = {
      for (final semantic in characterSemanticIds.take(8))
        semantic: List.generate(8, (_) => _variant()),
    };
    final pack = await _load(manifest);
    expect(pack.fallback, isFalse, reason: pack.note);
    expect(
      pack.activities.values.every((variants) => variants.length == 8),
      isTrue,
    );
    expect(
      pack.activities.values.expand((variants) => variants),
      hasLength(64),
    );
  });

  test(
    'empty maps, absent when and every allowed condition are accepted',
    () async {
      final manifest = _manifest();
      manifest['activities'] = <String, dynamic>{};
      manifest['fallbacks'] = <String, dynamic>{};
      final standard = await _load(manifest);
      expect(standard.fallback, isFalse, reason: standard.note);
      manifest['activities'] = {
        'idle': [
          _variant(),
          {..._variant(weight: 100, cooldownMs: 60000), 'when': []},
          {..._variant(), 'when': characterConditionIds.toList()},
        ],
      };
      final pack = await _load(manifest);
      expect(pack.fallback, isFalse, reason: pack.note);
      expect(pack.activities['idle']![0].when, isEmpty);
      expect(pack.activities['idle']![1].when, isEmpty);
      expect(pack.activities['idle']![2].when, characterConditionIds);
    },
  );

  test(
    'fallback may target a known semantic with no explicit mapping',
    () async {
      final pack = await _load(_manifest());
      expect(pack.fallback, isFalse, reason: pack.note);
      expect(pack.activities, isNot(contains('working.read_email')));
      expect(pack.fallbacks['blocked'], 'working.read_email');
    },
  );

  test('loaded semantic metadata is recursively immutable', () async {
    final pack = await _load(_manifest());
    expect(() => pack.activities.clear(), throwsUnsupportedError);
    expect(() => pack.activities['idle']!.clear(), throwsUnsupportedError);
    expect(
      () => pack.activities['working.write_code']!.single.when.clear(),
      throwsUnsupportedError,
    );
    expect(() => pack.fallbacks.clear(), throwsUnsupportedError);
  });

  test('model constructors copy caller semantic maps lists and sets', () {
    final conditions = <String>{'working', 'quiet'};
    final variant = PackVariant(animation: 'thinking', when: conditions);
    final variants = <PackVariant>[variant];
    final activities = <String, List<PackVariant>>{
      'working.write_code': variants,
    };
    final fallbacks = <String, String>{'blocked': 'idle'};
    final pack = CharacterPack(
      version: packSchemaV3,
      name: 'Synthetic constructor boundary',
      fallbackAsset: fixtures.kFallbackAsset,
      poses: const [],
      fallback: false,
      activities: activities,
      fallbacks: fallbacks,
    );
    conditions.clear();
    variants.clear();
    activities.clear();
    fallbacks.clear();
    expect(variant.weight, 1);
    expect(variant.cooldownMs, 0);
    expect(variant.when, {'working', 'quiet'});
    expect(pack.activities['working.write_code']!.single.animation, 'thinking');
    expect(pack.activities['working.write_code']!.single.when, {
      'working',
      'quiet',
    });
    expect(pack.fallbacks, {'blocked': 'idle'});
    expect(() => variant.when.add('warming'), throwsUnsupportedError);
    expect(() => pack.activities.clear(), throwsUnsupportedError);
    expect(
      () => pack.activities['working.write_code']!.clear(),
      throwsUnsupportedError,
    );
    expect(() => pack.fallbacks.clear(), throwsUnsupportedError);
  });
}
