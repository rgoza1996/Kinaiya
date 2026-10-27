import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/character/pack.dart';
import 'package:kinaiya_flutter/ui/widgets.dart';

// --- Synthetic bounded PNG assets (no new dependencies) ---

int _crc32(List<int> bytes) {
  var crc = 0xffffffff;
  for (final b in bytes) {
    crc ^= b;
    for (var i = 0; i < 8; i++) {
      if ((crc & 1) != 0) {
        crc = (crc >> 1) ^ 0xedb88320;
      } else {
        crc >>= 1;
      }
    }
  }
  return crc ^ 0xffffffff;
}

void _u32(List<int> out, int v) {
  out.addAll(<int>[
    (v >> 24) & 0xff,
    (v >> 16) & 0xff,
    (v >> 8) & 0xff,
    v & 0xff,
  ]);
}

void _chunk(List<int> out, String type, List<int> data) {
  _u32(out, data.length);
  final t = type.codeUnits;
  out.addAll(t);
  out.addAll(data);
  _u32(out, _crc32(<int>[...t, ...data]));
}

/// Minimal valid RGBA PNG with per-frame distinct blocks so each frame id
/// decodes to different bytes. Still tiny and bounded for tests.
Uint8List makePng(int width, int height, {int variant = 0}) {
  final raw = <int>[];
  for (var y = 0; y < height; y++) {
    raw.add(0); // filter byte
    for (var x = 0; x < width; x++) {
      // Distinct color per 4x4 block to make frames observable.
      final bx = x ~/ 4;
      final by = y ~/ 4;
      final sel = (bx + by * 2 + variant) % 4;
      final cols = <List<int>>[
        <int>[230, 40, 40, 255],
        <int>[40, 180, 60, 255],
        <int>[50, 90, 230, 255],
        <int>[240, 230, 240, 255],
      ];
      raw.addAll(cols[sel]);
    }
  }
  final compressed = ZLibCodec().encode(raw);
  final out = <int>[];
  out.addAll(<int>[137, 80, 78, 71, 13, 10, 26, 10]);
  final ihdr = <int>[];
  _u32(ihdr, width);
  _u32(ihdr, height);
  ihdr.addAll(<int>[8, 6, 0, 0, 0]);
  _chunk(out, 'IHDR', ihdr);
  _chunk(out, 'IDAT', compressed);
  _chunk(out, 'IEND', <int>[]);
  return Uint8List.fromList(out);
}

class FakeBundle extends AssetBundle {
  FakeBundle(this.strings, this.bytes, {this.delays = const {}});
  final Map<String, String> strings;
  final Map<String, Uint8List> bytes;
  final Map<String, Duration> delays;

  @override
  Future<ByteData> load(String key) async {
    final d = delays[key];
    if (d != null) await Future<void>.delayed(d);
    final b = bytes[key];
    if (b == null) throw FlutterError('missing $key');
    return ByteData.view(b.buffer, b.offsetInBytes, b.lengthInBytes);
  }

  @override
  Future<String> loadString(String key, {bool cache = true}) async {
    final d = delays[key];
    if (d != null) await Future<void>.delayed(d);
    final s = strings[key];
    if (s == null) throw FlutterError('missing $key');
    return s;
  }
}

class ViewBundle extends FakeBundle {
  ViewBundle(super.strings, super.bytes);
  @override
  Future<ByteData> load(String key) async {
    final data = await super.load(key);
    final padded = Uint8List(data.lengthInBytes + 12);
    padded.setRange(
      5,
      5 + data.lengthInBytes,
      data.buffer.asUint8List(data.offsetInBytes, data.lengthInBytes),
    );
    return ByteData.view(padded.buffer, 5, data.lengthInBytes);
  }
}

Finder paintedFrame() => find.byWidgetPredicate(
  (w) => w is CustomPaint && w.painter is MascotFramePainter,
);
MascotFramePainter currentFrame(WidgetTester tester) =>
    tester.widget<CustomPaint>(paintedFrame()).painter! as MascotFramePainter;
Finder frameAt(int id) => find.byWidgetPredicate(
  (w) =>
      w is CustomPaint &&
      w.painter is MascotFramePainter &&
      (w.painter! as MascotFramePainter).src ==
          Rect.fromLTWH((id % 2) * 4, (id ~/ 2) * 4, 4, 4),
);
Finder nonzeroFrame() => find.byWidgetPredicate(
  (w) =>
      w is CustomPaint &&
      w.painter is MascotFramePainter &&
      (w.painter! as MascotFramePainter).src != const Rect.fromLTWH(0, 0, 4, 4),
);
Future<void> decoded(WidgetTester tester) async {
  for (var i = 0; i < 40; i++) {
    await tester.runAsync(
      () => Future<void>.delayed(const Duration(milliseconds: 10)),
    );
    await tester.pump(const Duration(milliseconds: 1));
    if (paintedFrame().evaluate().isNotEmpty) return;
  }
  fail(
    'No real decoded sprite painted; fallback labels are not drawing evidence.',
  );
}

const String kSheetAsset = 'assets/character/kinaiya-core-v1.png';
const String kFallbackAsset = 'assets/mascot/kinaiya-mascot.png';

String _sha(Uint8List b) => sha256.convert(b).toString();

/// Builds the fixed v2 contract manifest with small explicit geometry
/// (8x8 sheet, 4x4 frames => 4 frame ids). Caller may override fields.
String buildV2(
  Uint8List sheetPng, {
  Map<String, dynamic>? animOverrides,
  Map<String, dynamic>? topOverrides,
  List<Map<String, dynamic>>? sheetsOverride,
}) {
  final sheets =
      sheetsOverride ??
      <Map<String, dynamic>>[
        <String, dynamic>{
          'id': 'core',
          'asset': kSheetAsset,
          'sha256': _sha(sheetPng),
          'width': 8,
          'height': 8,
          'frame_width': 4,
          'frame_height': 4,
        },
      ];
  final anims =
      animOverrides ??
      <String, dynamic>{
        'idle': {
          'sheet': 'core',
          'frames': [0, 1, 2, 3],
          'frame_ms': 100,
          'loop': true,
        },
        'listening': {
          'sheet': 'core',
          'frames': [0],
          'frame_ms': 100,
          'loop': true,
        },
        'thinking': {
          'sheet': 'core',
          'frames': [1],
          'frame_ms': 100,
          'loop': true,
        },
        'speaking': {
          'sheet': 'core',
          'frames': [2, 3],
          'frame_ms': 50,
          'loop': true,
        },
      };
  final base = <String, dynamic>{
    'pack_schema': 2,
    'pack_id': 'kinaiya-core',
    'version': '1.0.0-test',
    'name': 'Kinaiya',
    'author': 'Kinaiya test',
    'license': 'All rights reserved (test)',
    'provenance': {
      'source_asset': kFallbackAsset,
      'source_sha256': _sha(makePng(2, 2)),
      'generator': 'test-generator',
      'prompt_file': 'assets/character/prompt-test.md',
    },
    'theme': {'primary': '#141091', 'background': '#FFFFFF'},
    'voice_profile': null,
    'fallback_animation': 'idle',
    'anchor': [0.5, 0.9],
    'scale_limits': [0.5, 2.0],
    'sheets': sheets,
    'animations': anims,
  };
  if (topOverrides != null) base.addAll(topOverrides);
  return jsonEncode(base);
}

String buildV1(Uint8List posePng) {
  return jsonEncode(<String, dynamic>{
    'pack_schema': 1,
    'name': 'Kinaiya',
    'poses': [
      {'name': 'idle', 'asset': kFallbackAsset, 'sha256': _sha(posePng)},
    ],
  });
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  group('pack loader', () {
    test('v1 synthetic pose pack stays compatible', () async {
      final png = makePng(2, 2);
      final manifest = buildV1(png);
      final bundle = FakeBundle(const {}, {kFallbackAsset: png});
      final pack = await loadCharacterPack(
        bundle: bundle,
        manifestJson: manifest,
      );
      expect(pack.fallback, isFalse);
      expect(pack.version, packSchemaVersion);
      expect(pack.poses, hasLength(1));
      expect(pack.fallbackAsset, contains('kinaiya-mascot.png'));
      expect(
        CharacterPack.motionPhase('speaking'),
        CharacterPack.motionPhase('speaking'),
      );
    });

    test('v1 maxPoses 0 still forces fallback', () async {
      final png = makePng(2, 2);
      final manifest = buildV1(png);
      final bundle = FakeBundle(const {}, {kFallbackAsset: png});
      final pack = await loadCharacterPack(
        bounds: const PackBounds(maxPoses: 0),
        bundle: bundle,
        manifestJson: manifest,
      );
      expect(pack.fallback, isTrue);
      expect(pack.note, isNotEmpty);
    });

    test('v2 valid pack loads with explicit geometry', () async {
      final sheet = makePng(8, 8);
      final manifest = buildV2(sheet);
      final bundle = FakeBundle(const {}, {kSheetAsset: sheet});
      final pack = await loadCharacterPack(
        bundle: bundle,
        manifestJson: manifest,
      );
      expect(pack.fallback, isFalse, reason: pack.note);
      expect(pack.isSpritesheet, isTrue);
      expect(pack.packId, 'kinaiya-core');
      expect(pack.sheets['core']!.width, 8);
      expect(pack.sheets['core']!.columns, 2);
      expect(pack.sheets['core']!.capacity, 4);
      // Rectangle bounds come from the manifest, not an assumed 2048.
      expect(pack.sheets['core']!.frameRect(1).left, 4);
      expect(pack.themePrimary, '#141091');
      expect(pack.anchorX, 0.5);
      expect(pack.scaleMax, 2.0);
    });

    test('v2 state mapping and unknown fallback', () async {
      final sheet = makePng(8, 8);
      final bundle = FakeBundle(const {}, {kSheetAsset: sheet});
      final pack = await loadCharacterPack(
        bundle: bundle,
        manifestJson: buildV2(sheet),
      );
      expect(pack.resolveAnimationId('idle'), 'idle');
      expect(pack.resolveAnimationId('listening'), 'listening');
      expect(pack.resolveAnimationId('thinking'), 'thinking');
      expect(pack.resolveAnimationId('speaking'), 'speaking');
      expect(pack.resolveAnimationId('dancing'), 'idle');
      expect(pack.resolveAnimationId('working.inspect_repo'), 'idle');
      // Presentation-only mapping, never fabricated task state.
      expect(pack.resolveAnimationId('working.reason'), 'thinking');
    });

    test('v2 frame progression is deterministic', () async {
      final sheet = makePng(8, 8);
      final bundle = FakeBundle(const {}, {kSheetAsset: sheet});
      final pack = await loadCharacterPack(
        bundle: bundle,
        manifestJson: buildV2(sheet),
      );
      final idle = pack.animations['idle']!;
      expect(CharacterPack.frameIndexFor(idle, Duration.zero), 0);
      expect(
        CharacterPack.frameIndexFor(idle, const Duration(milliseconds: 99)),
        0,
      );
      expect(
        CharacterPack.frameIndexFor(idle, const Duration(milliseconds: 100)),
        1,
      );
      expect(
        CharacterPack.frameIndexFor(idle, const Duration(milliseconds: 350)),
        3,
      );
      expect(
        CharacterPack.frameIndexFor(idle, const Duration(milliseconds: 400)),
        0,
      );
    });
  });

  group('pack failures fall back with a visible note', () {
    test('malformed json', () async {
      final pack = await loadCharacterPack(
        bundle: FakeBundle(const {}, const {}),
        manifestJson: '{not json',
      );
      expect(pack.fallback, isTrue);
      expect(pack.note, isNotEmpty);
      expect(pack.fallbackAsset, contains('kinaiya-mascot.png'));
    });

    test('string-coerced schema is rejected', () async {
      final sheet = makePng(8, 8);
      final raw = jsonDecode(buildV2(sheet)) as Map<String, dynamic>;
      raw['pack_schema'] = '2';
      final pack = await loadCharacterPack(
        bundle: FakeBundle(const {}, {kSheetAsset: sheet}),
        manifestJson: jsonEncode(raw),
      );
      expect(pack.fallback, isTrue);
      expect(pack.note, isNotEmpty);
    });

    test('string-coerced frame_ms is rejected', () async {
      final sheet = makePng(8, 8);
      final anims = <String, dynamic>{
        'idle': {
          'sheet': 'core',
          'frames': [0],
          'frame_ms': '100',
          'loop': true,
        },
        'listening': {
          'sheet': 'core',
          'frames': [0],
          'frame_ms': 100,
          'loop': true,
        },
        'thinking': {
          'sheet': 'core',
          'frames': [0],
          'frame_ms': 100,
          'loop': true,
        },
        'speaking': {
          'sheet': 'core',
          'frames': [0],
          'frame_ms': 100,
          'loop': true,
        },
      };
      final pack = await loadCharacterPack(
        bundle: FakeBundle(const {}, {kSheetAsset: sheet}),
        manifestJson: buildV2(sheet, animOverrides: anims),
      );
      expect(pack.fallback, isTrue);
      expect(pack.note, isNotEmpty);
    });

    test('missing sheet asset', () async {
      final sheet = makePng(8, 8);
      final pack = await loadCharacterPack(
        bundle: FakeBundle(const {}, const {}),
        manifestJson: buildV2(sheet),
      );
      expect(pack.fallback, isTrue);
      expect(pack.note, contains('missing'));
    });

    test('checksum mismatch', () async {
      final sheet = makePng(8, 8);
      final other = makePng(8, 8);
      // Flip one byte so the sha differs but the PNG stays valid.
      other[40] = (other[40] + 1) & 0xff;
      final pack = await loadCharacterPack(
        bundle: FakeBundle(const {}, {kSheetAsset: other}),
        manifestJson: buildV2(sheet),
      );
      expect(pack.fallback, isTrue);
      expect(pack.note, contains('checksum'));
    });

    test('dimension mismatch', () async {
      final sheet = makePng(8, 8);
      final small = makePng(4, 4);
      final pack = await loadCharacterPack(
        bundle: FakeBundle(const {}, {kSheetAsset: small}),
        manifestJson: buildV2(sheet),
      );
      // Manifest claims 8x8 but bytes decode to 4x4 header.
      // sha must match the delivered bytes to reach the dimension check,
      // so rebuild the manifest around the small bytes with wrong geometry.
      final badManifest = buildV2(
        small,
        sheetsOverride: <Map<String, dynamic>>[
          <String, dynamic>{
            'id': 'core',
            'asset': kSheetAsset,
            'sha256': _sha(small),
            'width': 8,
            'height': 8,
            'frame_width': 4,
            'frame_height': 4,
          },
        ],
      );
      final pack2 = await loadCharacterPack(
        bundle: FakeBundle(const {}, {kSheetAsset: small}),
        manifestJson: badManifest,
      );
      expect(pack.fallback, isTrue);
      expect(pack2.fallback, isTrue);
      expect(pack2.note, contains('dimensions'));
    });

    test('frame out of bounds', () async {
      final sheet = makePng(8, 8);
      final anims = <String, dynamic>{
        'idle': {
          'sheet': 'core',
          'frames': [0, 99],
          'frame_ms': 100,
          'loop': true,
        },
        'listening': {
          'sheet': 'core',
          'frames': [0],
          'frame_ms': 100,
          'loop': true,
        },
        'thinking': {
          'sheet': 'core',
          'frames': [0],
          'frame_ms': 100,
          'loop': true,
        },
        'speaking': {
          'sheet': 'core',
          'frames': [0],
          'frame_ms': 100,
          'loop': true,
        },
      };
      final pack = await loadCharacterPack(
        bundle: FakeBundle(const {}, {kSheetAsset: sheet}),
        manifestJson: buildV2(sheet, animOverrides: anims),
      );
      expect(pack.fallback, isTrue);
      expect(pack.note, contains('bounds'));
    });

    test('unsafe asset path is rejected', () async {
      final sheet = makePng(8, 8);
      final badSheets = <Map<String, dynamic>>[
        <String, dynamic>{
          'id': 'core',
          'asset': '../outside/evil.png',
          'sha256': _sha(sheet),
          'width': 8,
          'height': 8,
          'frame_width': 4,
          'frame_height': 4,
        },
      ];
      final pack = await loadCharacterPack(
        bundle: FakeBundle(const {}, {kSheetAsset: sheet}),
        manifestJson: buildV2(sheet, sheetsOverride: badSheets),
      );
      expect(pack.fallback, isTrue);
      expect(pack.note, isNotEmpty);
    });

    test('frame_ms outside 50..5000 is rejected', () async {
      final sheet = makePng(8, 8);
      final anims = <String, dynamic>{
        'idle': {
          'sheet': 'core',
          'frames': [0],
          'frame_ms': 10,
          'loop': true,
        },
        'listening': {
          'sheet': 'core',
          'frames': [0],
          'frame_ms': 100,
          'loop': true,
        },
        'thinking': {
          'sheet': 'core',
          'frames': [0],
          'frame_ms': 100,
          'loop': true,
        },
        'speaking': {
          'sheet': 'core',
          'frames': [0],
          'frame_ms': 100,
          'loop': true,
        },
      };
      final pack = await loadCharacterPack(
        bundle: FakeBundle(const {}, {kSheetAsset: sheet}),
        manifestJson: buildV2(sheet, animOverrides: anims),
      );
      expect(pack.fallback, isTrue);
      expect(pack.note, contains('timing'));
    });

    test('oversize json is rejected', () async {
      final big = 'x' * (65 * 1024);
      final pack = await loadCharacterPack(
        bundle: FakeBundle(const {}, const {}),
        manifestJson: big,
      );
      expect(pack.fallback, isTrue);
      expect(pack.note, isNotEmpty);
    });

    test('v2 maxPoses 0 still forces fallback', () async {
      final sheet = makePng(8, 8);
      final pack = await loadCharacterPack(
        bounds: const PackBounds(maxPoses: 0),
        bundle: FakeBundle(const {}, {kSheetAsset: sheet}),
        manifestJson: buildV2(sheet),
      );
      expect(pack.fallback, isTrue);
      expect(pack.note, isNotEmpty);
    });
  });

  test('real default pack has all four animated states', () async {
    final pack = await loadCharacterPack();
    expect(pack.fallback, isFalse, reason: pack.note);
    expect(pack.isSpritesheet, isTrue);
    for (final state in ['idle', 'listening', 'thinking', 'speaking']) {
      expect(
        pack.animationFor(state)!.frames.toSet().length,
        greaterThanOrEqualTo(4),
      );
    }
  });
  test('trailing atlas padding keeps last frame in actual bounds', () async {
    final png = makePng(9, 9);
    final pack = await loadCharacterPack(
      bundle: FakeBundle({}, {kSheetAsset: png}),
      manifestJson: buildV2(
        png,
        sheetsOverride: [
          {
            'id': 'core',
            'asset': kSheetAsset,
            'sha256': _sha(png),
            'width': 9,
            'height': 9,
            'frame_width': 4,
            'frame_height': 4,
          },
        ],
      ),
    );
    expect(pack.fallback, isFalse);
    expect(pack.sheets['core']!.frameRect(3), const Rect.fromLTWH(4, 4, 4, 4));
    expect(pack.sheets['core']!.containsFrame(4), isFalse);
  });

  group('mascot widget draws real frames', () {
    Future<CharacterPack> validPack(Uint8List sheet, FakeBundle bundle) =>
        loadCharacterPack(bundle: bundle, manifestJson: buildV2(sheet));

    testWidgets('shows bitmap frames and advances after pumps', (tester) async {
      final sheet = makePng(8, 8);
      final bundle = FakeBundle(const {}, {kSheetAsset: sheet});
      final pack = await validPack(sheet, bundle);
      expect(pack.isSpritesheet, isTrue);

      await tester.pumpWidget(
        MaterialApp(
          home: MascotWidget(pack: pack, mood: 'idle', bundle: bundle),
        ),
      );
      // Non-blocking: shipped art shows immediately while the sheet decodes.
      await tester.pump();
      await decoded(tester);
      expect(find.byType(CustomPaint), findsWidgets);
      expect(frameAt(0), findsOneWidget);
      // Observable drawn frame advances with pumped time (frame_ms 100).
      await tester.pump(const Duration(milliseconds: 120));
      expect(frameAt(1), findsOneWidget);
      await tester.pump(const Duration(milliseconds: 200));
      expect(frameAt(3), findsOneWidget);
      await tester.pumpWidget(const SizedBox.shrink());
      expect(tester.takeException(), isNull);
    });

    testWidgets('state selects animation; unknown falls back to idle', (
      tester,
    ) async {
      final sheet = makePng(8, 8);
      final bundle = FakeBundle(const {}, {kSheetAsset: sheet});
      final pack = await validPack(sheet, bundle);

      await tester.pumpWidget(
        MaterialApp(
          home: MascotWidget(pack: pack, mood: 'speaking', bundle: bundle),
        ),
      );
      await tester.pump();
      await decoded(tester);
      expect(frameAt(2), findsOneWidget);

      await tester.pumpWidget(
        MaterialApp(
          home: MascotWidget(pack: pack, mood: 'dancing', bundle: bundle),
        ),
      );
      await tester.pump();
      await decoded(tester);
      expect(frameAt(0), findsOneWidget);

      await tester.pumpWidget(
        MaterialApp(
          home: MascotWidget(
            pack: pack,
            mood: 'working.reason',
            bundle: bundle,
          ),
        ),
      );
      await tester.pump();
      await decoded(tester);
      expect(frameAt(1), findsOneWidget);
      await tester.pumpWidget(const SizedBox.shrink());
      expect(tester.takeException(), isNull);
    });

    testWidgets('reduced motion shows a fixed frame', (tester) async {
      final sheet = makePng(8, 8);
      final bundle = FakeBundle(const {}, {kSheetAsset: sheet});
      final pack = await validPack(sheet, bundle);
      await tester.pumpWidget(
        MaterialApp(
          home: MediaQuery(
            data: const MediaQueryData(disableAnimations: true),
            child: MascotWidget(pack: pack, mood: 'idle', bundle: bundle),
          ),
        ),
      );
      await tester.pump();
      await decoded(tester);
      expect(frameAt(0), findsOneWidget);
      await tester.pump(const Duration(seconds: 2));
      expect(frameAt(0), findsOneWidget);
      expect(nonzeroFrame(), findsNothing);
    });

    testWidgets('ticker mode off freezes the frame', (tester) async {
      final sheet = makePng(8, 8);
      final bundle = FakeBundle(const {}, {kSheetAsset: sheet});
      final pack = await validPack(sheet, bundle);
      await tester.pumpWidget(
        MaterialApp(
          home: TickerMode(
            enabled: false,
            child: MascotWidget(pack: pack, mood: 'idle', bundle: bundle),
          ),
        ),
      );
      await tester.pump();
      await decoded(tester);
      final first = paintedFrame();
      expect(first, findsOneWidget);
      await tester.pump(const Duration(seconds: 2));
      // Still frame 0: a muted ticker never advances the bitmap.
      expect(frameAt(0), findsOneWidget);
    });

    testWidgets('pack replacement wins; stale decode is dropped', (
      tester,
    ) async {
      final sheetA = makePng(8, 8);
      final sheetB = makePng(8, 8, variant: 1);
      const assetB = 'assets/character/kinaiya-alt-v1.png';
      String manifestB() {
        final base = jsonDecode(buildV2(sheetA)) as Map<String, dynamic>;
        final sheets = [
          {
            'id': 'core',
            'asset': assetB,
            'sha256': _sha(sheetB),
            'width': 8,
            'height': 8,
            'frame_width': 4,
            'frame_height': 4,
          },
        ];
        base['sheets'] = sheets;
        base['pack_id'] = 'kinaiya-alt';
        return jsonEncode(base);
      }

      final slowBundle = FakeBundle(
        const {},
        {kSheetAsset: sheetA, assetB: sheetB},
        delays: {kSheetAsset: const Duration(milliseconds: 300)},
      );
      final fastBundle = FakeBundle(const {}, {
        kSheetAsset: sheetA,
        assetB: sheetB,
      });

      final packA = await loadCharacterPack(
        bundle: fastBundle,
        manifestJson: buildV2(sheetA),
      );
      final packB = await loadCharacterPack(
        bundle: fastBundle,
        manifestJson: manifestB(),
      );
      expect(packA.isSpritesheet, isTrue);
      expect(packB.isSpritesheet, isTrue);

      // Mount A with a slow sheet, then replace with B before A finishes.
      await tester.pumpWidget(
        MaterialApp(
          home: MascotWidget(pack: packA, mood: 'idle', bundle: slowBundle),
        ),
      );
      await tester.pumpWidget(
        MaterialApp(
          home: MascotWidget(pack: packB, mood: 'speaking', bundle: fastBundle),
        ),
      );
      await tester.pump();
      await decoded(tester);
      final winner = currentFrame(tester).image;
      await tester.pump(const Duration(milliseconds: 500));
      expect(identical(currentFrame(tester).image, winner), isTrue);
      // Owned by B: speaking animation, not stale idle from A.
      expect(currentFrame(tester).src.top, 4);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox.shrink());
      expect(tester.takeException(), isNull);
    });

    testWidgets('same-pack bundle replacement loads a fresh image', (
      tester,
    ) async {
      final png = makePng(8, 8);
      final a = FakeBundle({}, {kSheetAsset: png}),
          b = FakeBundle({}, {kSheetAsset: png});
      final pack = await validPack(png, a);
      await tester.pumpWidget(
        MaterialApp(
          home: MascotWidget(pack: pack, mood: 'idle', bundle: a),
        ),
      );
      await decoded(tester);
      final old = currentFrame(tester).image;
      await tester.pumpWidget(
        MaterialApp(
          home: MascotWidget(pack: pack, mood: 'idle', bundle: b),
        ),
      );
      await decoded(tester);
      expect(identical(old, currentFrame(tester).image), isFalse);
      expect(tester.takeException(), isNull);
    });

    testWidgets(
      'ByteData view offsets work through validation and actual decoding',
      (tester) async {
        final png = makePng(8, 8);
        final bundle = ViewBundle({}, {kSheetAsset: png});
        final pack = await validPack(png, bundle);
        expect(pack.fallback, isFalse);
        await tester.pumpWidget(
          MaterialApp(
            home: MascotWidget(pack: pack, mood: 'idle', bundle: bundle),
          ),
        );
        await decoded(tester);
        expect(frameAt(0), findsOneWidget);
      },
    );

    testWidgets('reduced motion can resume after an elapsed state change', (
      tester,
    ) async {
      final png = makePng(8, 8), bundle = FakeBundle({}, {});
      bundle.bytes[kSheetAsset] = png;
      final pack = await validPack(png, bundle);
      Widget scene(bool reduced) => MaterialApp(
        home: MediaQuery(
          data: MediaQueryData(disableAnimations: reduced),
          child: MascotWidget(pack: pack, mood: 'idle', bundle: bundle),
        ),
      );
      await tester.pumpWidget(scene(false));
      await decoded(tester);
      await tester.pump(const Duration(milliseconds: 350));
      await tester.pumpWidget(scene(true));
      expect(frameAt(0), findsOneWidget);
      await tester.pump(const Duration(seconds: 2));
      expect(frameAt(0), findsOneWidget);
      await tester.pumpWidget(scene(false));
      await tester.pump(const Duration(milliseconds: 110));
      expect(frameAt(1), findsOneWidget);
      expect(
        CharacterPack.frameIndexFor(
          pack.animations['idle']!,
          const Duration(seconds: -1),
        ),
        0,
      );
    });

    testWidgets('pending load after disposal cannot paint or throw', (
      tester,
    ) async {
      final png = makePng(8, 8), fast = FakeBundle({}, {});
      fast.bytes[kSheetAsset] = png;
      final pack = await validPack(png, fast);
      final slow = FakeBundle(
        {},
        {kSheetAsset: png},
        delays: {kSheetAsset: const Duration(milliseconds: 300)},
      );
      await tester.pumpWidget(
        MaterialApp(
          home: MascotWidget(pack: pack, mood: 'idle', bundle: slow),
        ),
      );
      await tester.pumpWidget(const SizedBox.shrink());
      await tester.pump(const Duration(seconds: 1));
      expect(paintedFrame(), findsNothing);
      expect(tester.takeException(), isNull);
    });

    testWidgets('corrupt PNG data gives a visible static fallback', (
      tester,
    ) async {
      final png = makePng(8, 8);
      png[50] ^= 255;
      final bundle = FakeBundle({}, {kSheetAsset: png});
      final pack = await validPack(png, bundle);
      expect(pack.fallback, isFalse);
      await tester.pumpWidget(
        MaterialApp(
          home: MascotWidget(pack: pack, mood: 'idle', bundle: bundle),
        ),
      );
      for (var i = 0; i < 15; i++) {
        await tester.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 10)),
        );
        await tester.pump();
      }
      expect(paintedFrame(), findsNothing);
      expect(
        find.text('Animation unavailable; showing original mascot.'),
        findsOneWidget,
      );
      expect(tester.takeException(), isNull);
    });

    testWidgets('invalid pack stays on shipped art without crashing', (
      tester,
    ) async {
      final pack = CharacterPack(
        version: packSchemaV2,
        name: 'Kinaiya',
        fallbackAsset: kFallbackAsset,
        poses: const [],
        fallback: true,
        note: 'bad pack; single frame',
      );
      await tester.pumpWidget(
        MaterialApp(
          home: MascotWidget(pack: pack, mood: 'idle'),
        ),
      );
      await tester.pump();
      expect(find.bySemanticsLabel('Kinaiya mascot, idle'), findsOneWidget);
      expect(find.byType(Image), findsOneWidget);
    });
  });
}
