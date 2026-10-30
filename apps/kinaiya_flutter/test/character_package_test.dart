import 'dart:convert';

import 'package:crypto/crypto.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/character/package.dart';
import 'package:kinaiya_flutter/character/pack.dart';

import 'mascot_sprites_test.dart' as fixtures;

Map<String, dynamic> packageFixture({String name = 'Local character'}) {
  final sheet = fixtures.makePng(8, 8);
  final source = fixtures.makePng(2, 2);
  return {
    'package_schema': 1,
    'manifest': jsonDecode(
      fixtures.buildV2(sheet, topOverrides: {'name': name}),
    ),
    'files': [
      {'path': fixtures.kSheetAsset, 'base64': base64Encode(sheet)},
      {'path': fixtures.kFallbackAsset, 'base64': base64Encode(source)},
      {
        'path': 'assets/character/prompt-test.md',
        'base64': base64Encode(utf8.encode('Local test artwork prompt.')),
      },
    ],
  };
}

Map<String, dynamic> semanticPackageFixture() {
  final value = packageFixture();
  manifestOf(value).addAll({
    'pack_schema': packSchemaV3,
    'activities': {
      'idle': [
        {'animation': 'idle', 'weight': 1, 'cooldown_ms': 0},
      ],
      'working.write_code': [
        {
          'animation': 'thinking',
          'weight': 100,
          'cooldown_ms': 60000,
          'when': ['working', 'quiet'],
        },
        {'animation': 'idle', 'weight': 1, 'cooldown_ms': 0, 'when': []},
      ],
    },
    'fallbacks': {
      'working.inspect_repo': 'working.write_code',
      'blocked': 'working.read_email',
    },
  });
  return jsonDecode(jsonEncode(value)) as Map<String, dynamic>;
}

AssetBundle _fixtureBundle(Map<String, dynamic> value) => fixtures.FakeBundle(
  {packAssetPath: jsonEncode(value['manifest'])},
  {
    for (final entry in value['files'] as List)
      (entry as Map)['path'] as String: base64Decode(entry['base64'] as String),
  },
);

AssetBundle defaultFixture() =>
    _fixtureBundle(packageFixture(name: 'Bundled character'));

Map<String, dynamic> manifestOf(Map<String, dynamic> package) =>
    package['manifest'] as Map<String, dynamic>;
List<dynamic> filesOf(Map<String, dynamic> package) => package['files'] as List;

Map<String, dynamic> _semanticVariant(Map<String, dynamic> package) =>
    ((manifestOf(package)['activities'] as Map)['working.write_code'] as List)
            .first
        as Map<String, dynamic>;

void _replacePng(Map<String, dynamic> package, Uint8List png) {
  (filesOf(package).first as Map)['base64'] = base64Encode(png);
  (((manifestOf(package)['sheets'] as List).first) as Map)['sha256'] = sha256
      .convert(png)
      .toString();
}

Uint8List _chunk(String type, List<int> data) {
  final out = Uint8List(data.length + 12);
  final view = ByteData.sublistView(out);
  view.setUint32(0, data.length);
  out.setRange(4, 8, type.codeUnits);
  out.setRange(8, 8 + data.length, data);
  var crc = 0xffffffff;
  for (final byte in out.sublist(4, 8 + data.length)) {
    crc ^= byte;
    for (var i = 0; i < 8; i++) {
      crc = (crc & 1) == 1 ? (crc >> 1) ^ 0xedb88320 : crc >> 1;
    }
  }
  view.setUint32(out.length - 4, (crc ^ 0xffffffff) & 0xffffffff);
  return out;
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test(
    'round trip decodes actual PNGs and exports source and prompt',
    () async {
      final selection = await inspectCharacterPackage(
        jsonEncode(packageFixture()),
      );
      expect(selection.pack.name, 'Local character');
      expect(selection.pack.isSpritesheet, isTrue);
      final raw = exportCharacterSelection(selection);
      final again = await inspectCharacterPackage(raw);
      expect(exportCharacterSelection(again), raw);
      expect(filesOf(jsonDecode(raw) as Map<String, dynamic>), hasLength(3));
      expect(
        await selection.bundle.loadString('assets/character/prompt-test.md'),
        'Local test artwork prompt.',
      );
    },
  );

  test(
    'validated bundle and pack cannot be changed through public values',
    () async {
      final selection = await inspectCharacterPackage(
        jsonEncode(packageFixture()),
      );
      expect(() => selection.pack.sheets.clear(), throwsUnsupportedError);
      expect(() => selection.pack.animations.clear(), throwsUnsupportedError);
      expect(
        () => selection.pack.animations['idle']!.frames.clear(),
        throwsUnsupportedError,
      );
      final bytes = await selection.bundle.load(fixtures.kSheetAsset);
      bytes.setUint8(0, 0);
      expect(
        (await selection.bundle.load(fixtures.kSheetAsset)).getUint8(0),
        137,
      );
      await expectLater(
        selection.bundle.load('assets/undeclared.png'),
        throwsA(isA<CharacterPackageException>()),
      );
    },
  );

  test('schema 3 semantics survive a canonical package round trip', () async {
    final value = semanticPackageFixture();
    final selection = await inspectCharacterPackage(jsonEncode(value));
    expect(selection.pack.version, packSchemaV3);
    expect(selection.pack.isSpritesheet, isTrue);
    final variant = selection.pack.activities['working.write_code']!.first;
    expect(variant.animation, 'thinking');
    expect(variant.weight, 100);
    expect(variant.cooldownMs, 60000);
    expect(variant.when, {'working', 'quiet'});
    expect(selection.pack.activities['idle']!.single.when, isEmpty);
    expect(selection.pack.fallbacks['blocked'], 'working.read_email');
    final raw = exportCharacterSelection(selection);
    final exported = jsonDecode(raw) as Map<String, dynamic>;
    expect(manifestOf(exported)['activities'], manifestOf(value)['activities']);
    expect(manifestOf(exported)['fallbacks'], manifestOf(value)['fallbacks']);
    final again = await inspectCharacterPackage(raw);
    expect(exportCharacterSelection(again), raw);
    expect(again.pack.activities['working.write_code']!.first.when, {
      'working',
      'quiet',
    });
  });

  test('imported schema 3 metadata is recursively immutable', () async {
    final selection = await inspectCharacterPackage(
      jsonEncode(semanticPackageFixture()),
    );
    expect(() => selection.pack.activities.clear(), throwsUnsupportedError);
    expect(
      () => selection.pack.activities['working.write_code']!.clear(),
      throwsUnsupportedError,
    );
    expect(
      () => selection.pack.activities['working.write_code']!.first.when.add(
        'blocked',
      ),
      throwsUnsupportedError,
    );
    expect(() => selection.pack.fallbacks.clear(), throwsUnsupportedError);
  });

  test(
    'schema 3 bundled defaults validate and export semantic metadata',
    () async {
      final selection = await loadDefaultCharacterSelection(
        _fixtureBundle(semanticPackageFixture()),
      );
      expect(selection.packageJson, isNull);
      expect(selection.pack.fallback, isFalse);
      expect(selection.pack.activities, hasLength(2));
      expect(
        (await inspectCharacterPackage(
          exportCharacterSelection(selection),
        )).pack.fallbacks,
        selection.pack.fallbacks,
      );
    },
  );

  test('schema 3 allows empty semantic maps for standard fallbacks', () async {
    final value = semanticPackageFixture();
    manifestOf(value)['activities'] = <String, dynamic>{};
    manifestOf(value)['fallbacks'] = <String, dynamic>{};
    final selection = await inspectCharacterPackage(jsonEncode(value));
    expect(selection.pack.activities, isEmpty);
    expect(selection.pack.fallbacks, isEmpty);
    expect(selection.pack.isSpritesheet, isTrue);
  });

  test(
    'default exports a complete validated package while remaining default',
    () async {
      final selection = await loadDefaultCharacterSelection(defaultFixture());
      expect(selection.packageJson, isNull);
      expect(selection.pack.fallback, isFalse);
      expect(
        (await inspectCharacterPackage(
          exportCharacterSelection(selection),
        )).pack.name,
        'Bundled character',
      );
    },
  );

  test(
    'actual bundled default decodes and exports all provenance assets',
    () async {
      final selection = await loadDefaultCharacterSelection(rootBundle);
      expect(selection.pack.fallback, isFalse);
      expect(selection.pack.packId, 'kinaiya-default');
      final package =
          jsonDecode(exportCharacterSelection(selection))
              as Map<String, dynamic>;
      expect(
        filesOf(
          package,
        ).map((file) => (file as Map<String, dynamic>)['path']).toSet(),
        {
          'assets/character/kinaiya-core-v1.png',
          'assets/character/kinaiya-activity-v1.png',
          'assets/character/kinaiya-seated-idle-v1.png',
          'assets/character/kinaiya-seated-idle-v1.prompts.md',
          'assets/mascot/kinaiya-mascot.png',
        },
      );
      final imported = await inspectCharacterPackage(jsonEncode(package));
      expect(imported.pack.version, packSchemaV3);
      expect(
        imported.pack.activities['working.generic']!.single.animation,
        'generic_work',
      );
    },
  );

  test(
    'canonical export ignores map order and rejects duplicate JSON keys',
    () async {
      final value = packageFixture();
      final original = await inspectCharacterPackage(jsonEncode(value));
      final reversed = {
        for (final key in value.keys.toList().reversed) key: value[key],
      };
      expect(
        exportCharacterSelection(
          await inspectCharacterPackage(jsonEncode(reversed)),
        ),
        exportCharacterSelection(original),
      );
      final duplicate = jsonEncode(value).replaceFirst(
        '"package_schema":1',
        '"package_schema":2,"package_schema":1',
      );
      await expectLater(
        inspectCharacterPackage(duplicate),
        throwsA(isA<CharacterPackageException>()),
      );
      final escapedDuplicate = jsonEncode(value).replaceFirst(
        '"name":"Local character"',
        r'"name":"old","na\u006de":"Local character"',
      );
      await expectLater(
        inspectCharacterPackage(escapedDuplicate),
        throwsA(isA<CharacterPackageException>()),
      );
      await expectLater(
        inspectCharacterPackage('${'[' * 40}0${']' * 40}'),
        throwsA(isA<CharacterPackageException>()),
      );
    },
  );

  final invalidCases = <String, void Function(Map<String, dynamic>)>{
    'unknown envelope key': (p) => p['token'] = 'never accepted',
    'unknown manifest personality': (p) =>
        manifestOf(p)['personality'] = 'ignored?',
    'unknown provenance key': (p) =>
        (manifestOf(p)['provenance'] as Map)['permissions'] = [],
    'unknown theme key': (p) => (manifestOf(p)['theme'] as Map)['voice'] = 'x',
    'unknown sheet key': (p) =>
        ((manifestOf(p)['sheets'] as List).first as Map)['url'] = 'x',
    'unknown animation key': (p) =>
        ((manifestOf(p)['animations'] as Map)['idle'] as Map)['code'] = 'x',
    'voice import': (p) => manifestOf(p)['voice_profile'] = 'new voice',
    'v1 import': (p) => manifestOf(p)['pack_schema'] = 1,
    'v2 activity mappings': (p) => manifestOf(p)['activities'] = {},
    'v2 semantic fallbacks': (p) => manifestOf(p)['fallbacks'] = {},
    'noninteger schema': (p) => p['package_schema'] = 1.0,
    'duplicate paths': (p) => filesOf(p).add(filesOf(p).first),
    'path traversal': (p) =>
        (filesOf(p).first as Map)['path'] = 'assets/../outside.png',
    'network path': (p) =>
        (filesOf(p).first as Map)['path'] = 'https://example.com/a.png',
    'unreferenced file': (p) =>
        filesOf(p).add({'path': 'assets/extra.txt', 'base64': 'AA=='}),
    'missing prompt': (p) => filesOf(p).removeLast(),
    'unknown file key': (p) => (filesOf(p).first as Map)['mode'] = 'execute',
    'bad base64': (p) => (filesOf(p).first as Map)['base64'] = 'not base64',
    'noncanonical base64': (p) => (filesOf(p).last as Map)['base64'] = 'Zh==',
    'source checksum mismatch': (p) =>
        (manifestOf(p)['provenance'] as Map)['source_sha256'] = '0' * 64,
    'invalid prompt UTF8': (p) => (filesOf(p).last as Map)['base64'] = '/w==',
    'oversized prompt': (p) => (filesOf(p).last as Map)['base64'] =
        base64Encode(List.filled((64 << 10) + 1, 65)),
    'too many files': (p) => p['files'] = List.filled(13, filesOf(p).first),
    'oversized manifest': (p) => manifestOf(p)['name'] = 'x' * (64 << 10),
    'out of bounds frame': (p) =>
        ((manifestOf(p)['animations'] as Map)['idle'] as Map)['frames'] = [100],
    'bad timing': (p) =>
        ((manifestOf(p)['animations'] as Map)['idle'] as Map)['frame_ms'] = 49,
    'header mismatch': (p) =>
        ((manifestOf(p)['sheets'] as List).first as Map)['width'] = 9,
  };
  for (final entry in invalidCases.entries) {
    test('rejects ${entry.key} with a fixed safe error', () async {
      final value = packageFixture();
      entry.value(value);
      await expectLater(
        inspectCharacterPackage(jsonEncode(value)),
        throwsA(
          isA<CharacterPackageException>().having(
            (e) => e.toString(),
            'safe message',
            'Character package is invalid or exceeds local limits.',
          ),
        ),
      );
    });
  }

  final invalidSemantics = <String, void Function(Map<String, dynamic>)>{
    'noninteger pack schema': (p) => manifestOf(p)['pack_schema'] = 3.0,
    'missing activities': (p) => manifestOf(p).remove('activities'),
    'missing fallbacks': (p) => manifestOf(p).remove('fallbacks'),
    'nonmap activities': (p) => manifestOf(p)['activities'] = [],
    'unknown semantic': (p) =>
        (manifestOf(p)['activities'] as Map)['private.thought'] = [
          {'animation': 'thinking', 'weight': 1, 'cooldown_ms': 0},
        ],
    'empty variant list': (p) =>
        (manifestOf(p)['activities'] as Map)['idle'] = [],
    'nonlist variants': (p) =>
        (manifestOf(p)['activities'] as Map)['idle'] = {},
    'nonobject variant': (p) =>
        (manifestOf(p)['activities'] as Map)['idle'] = ['idle'],
    'unknown variant key': (p) =>
        _semanticVariant(p)['execute'] = 'private tool input',
    'missing variant animation': (p) => _semanticVariant(p).remove('animation'),
    'missing variant weight': (p) => _semanticVariant(p).remove('weight'),
    'missing variant cooldown': (p) =>
        _semanticVariant(p).remove('cooldown_ms'),
    'unknown animation reference': (p) =>
        _semanticVariant(p)['animation'] = 'missing',
    'noninteger weight': (p) => _semanticVariant(p)['weight'] = 1.0,
    'zero weight': (p) => _semanticVariant(p)['weight'] = 0,
    'excess weight': (p) => _semanticVariant(p)['weight'] = 101,
    'noninteger cooldown': (p) => _semanticVariant(p)['cooldown_ms'] = '1',
    'negative cooldown': (p) => _semanticVariant(p)['cooldown_ms'] = -1,
    'excess cooldown': (p) => _semanticVariant(p)['cooldown_ms'] = 60001,
    'nonlist conditions': (p) => _semanticVariant(p)['when'] = 'quiet',
    'null conditions': (p) => _semanticVariant(p)['when'] = null,
    'nonstring condition': (p) => _semanticVariant(p)['when'] = [true],
    'unknown condition': (p) => _semanticVariant(p)['when'] = ['authenticated'],
    'duplicate condition': (p) =>
        _semanticVariant(p)['when'] = ['quiet', 'quiet'],
    'excess variants per activity': (p) =>
        (manifestOf(p)['activities'] as Map)['idle'] = List.filled(9, {
          'animation': 'idle',
          'weight': 1,
          'cooldown_ms': 0,
        }),
    'excess total variants': (p) => manifestOf(p)['activities'] = {
      for (final semantic in characterSemanticIds.take(9))
        semantic: List.filled(8, {
          'animation': 'idle',
          'weight': 1,
          'cooldown_ms': 0,
        }),
    },
    'nonmap fallbacks': (p) => manifestOf(p)['fallbacks'] = [],
    'unknown fallback source': (p) =>
        (manifestOf(p)['fallbacks'] as Map)['private.thought'] = 'idle',
    'unknown fallback target': (p) =>
        (manifestOf(p)['fallbacks'] as Map)['blocked'] = 'private.thought',
    'nonstring fallback target': (p) =>
        (manifestOf(p)['fallbacks'] as Map)['blocked'] = {'semantic': 'idle'},
    'self fallback': (p) =>
        (manifestOf(p)['fallbacks'] as Map)['blocked'] = 'blocked',
    'cycle through standard fallback': (p) =>
        manifestOf(p)['fallbacks'] = {'thinking': 'working.generic'},
    'fallback cycle': (p) => manifestOf(p)['fallbacks'] = {
      'blocked': 'warming',
      'warming': 'alert.important',
      'alert.important': 'blocked',
    },
  };
  for (final entry in invalidSemantics.entries) {
    test('schema 3 rejects ${entry.key} with a fixed safe error', () async {
      final value = semanticPackageFixture();
      entry.value(value);
      await expectLater(
        inspectCharacterPackage(jsonEncode(value)),
        throwsA(
          isA<CharacterPackageException>().having(
            (e) => e.toString(),
            'safe message',
            'Character package is invalid or exceeds local limits.',
          ),
        ),
      );
    });
  }

  test('schema 3 accepts the total variant and condition boundaries', () async {
    final value = semanticPackageFixture();
    manifestOf(value)['activities'] = {
      for (final semantic in characterSemanticIds.take(8))
        semantic: List.filled(8, {
          'animation': 'idle',
          'weight': 1,
          'cooldown_ms': 0,
          'when': characterConditionIds.toList(),
        }),
    };
    final selection = await inspectCharacterPackage(jsonEncode(value));
    expect(selection.pack.activities, hasLength(8));
    expect(
      selection.pack.activities.values.expand((variants) => variants),
      hasLength(64),
    );
    expect(
      selection.pack.activities.values.first.first.when,
      characterConditionIds,
    );
  });

  for (final name in [
    'unknown envelope key',
    'unknown provenance key',
    'unknown sheet key',
    'unknown animation key',
    'voice import',
    'path traversal',
    'network path',
    'unreferenced file',
    'source checksum mismatch',
    'oversized prompt',
    'out of bounds frame',
    'bad timing',
    'header mismatch',
  ]) {
    test('schema 3 retains package rejection for $name', () async {
      final value = semanticPackageFixture();
      invalidCases[name]!(value);
      await expectLater(
        inspectCharacterPackage(jsonEncode(value)),
        throwsA(isA<CharacterPackageException>()),
      );
    });
  }

  test('schema 3 rejects duplicate nested variant keys', () async {
    final raw = jsonEncode(semanticPackageFixture()).replaceFirst(
      '"animation":"thinking"',
      '"animation":"idle","animation":"thinking"',
    );
    await expectLater(
      inspectCharacterPackage(raw),
      throwsA(isA<CharacterPackageException>()),
    );
  });

  test(
    'rejects raw byte budget before parsing including UTF8 expansion',
    () async {
      await expectLater(
        inspectCharacterPackage('x' * (maxCharacterPackageBytes + 1)),
        throwsA(isA<CharacterPackageException>()),
      );
      await expectLater(
        inspectCharacterPackage('é' * ((maxCharacterPackageBytes ~/ 2) + 1)),
        throwsA(isA<CharacterPackageException>()),
      );
    },
  );

  test(
    'rejects malformed PNG structure, CRC, APNG and real decoder failure',
    () async {
      final original = fixtures.makePng(8, 8);
      final invalidCrc = Uint8List.fromList(original)..[35] ^= 1;
      final apng = Uint8List.fromList([
        ...original.sublist(0, 33),
        ..._chunk('acTL', [0, 0, 0, 2, 0, 0, 0, 0]),
        ...original.sublist(33),
      ]);
      // Structurally valid and CRC-correct PNG with invalid compressed pixels.
      final invalidPixels = Uint8List.fromList([
        ...original.sublist(0, 33),
        ..._chunk('IDAT', [1, 2, 3, 4]),
        ..._chunk('IEND', []),
      ]);
      for (final png in [
        invalidCrc,
        apng,
        invalidPixels,
        Uint8List.fromList([...original, 0]),
      ]) {
        final value = packageFixture();
        _replacePng(value, png);
        await expectLater(
          inspectCharacterPackage(jsonEncode(value)),
          throwsA(isA<CharacterPackageException>()),
        );
      }
    },
  );

  test('rejects PNG texture and pixel budgets before a huge decode', () async {
    final value = packageFixture();
    final original = fixtures.makePng(8, 8);
    final ihdr = original.sublist(16, 29);
    ByteData.sublistView(ihdr).setUint32(0, 4097);
    final png = Uint8List.fromList([
      ...original.sublist(0, 8),
      ..._chunk('IHDR', ihdr),
      ...original.sublist(33),
    ]);
    _replacePng(value, png);
    await expectLater(
      inspectCharacterPackage(jsonEncode(value)),
      throwsA(isA<CharacterPackageException>()),
    );
    final overPixels = packageFixture();
    ByteData.sublistView(ihdr).setUint32(0, 4096);
    ByteData.sublistView(ihdr).setUint32(4, 4096);
    final largeHeader = Uint8List.fromList([
      ...original.sublist(0, 8),
      ..._chunk('IHDR', ihdr),
      ...original.sublist(33),
    ]);
    _replacePng(overPixels, largeHeader);
    final sheet = (manifestOf(overPixels)['sheets'] as List).first as Map;
    sheet['width'] = sheet['height'] = 4096;
    (filesOf(overPixels)[1] as Map)['base64'] = base64Encode(largeHeader);
    (manifestOf(overPixels)['provenance'] as Map)['source_sha256'] = sha256
        .convert(largeHeader)
        .toString();
    await expectLater(
      inspectCharacterPackage(jsonEncode(overPixels)),
      throwsA(isA<CharacterPackageException>()),
    );
  });

  test('aggregate decoded file bytes are bounded before PNG parsing', () async {
    final value = packageFixture();
    (filesOf(value).first as Map)['base64'] = base64Encode(Uint8List(8 << 20));
    await expectLater(
      inspectCharacterPackage(jsonEncode(value)),
      throwsA(isA<CharacterPackageException>()),
    );
  });
}
