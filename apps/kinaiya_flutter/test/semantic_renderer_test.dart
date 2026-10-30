import 'dart:async';
import 'dart:convert';

import 'package:crypto/crypto.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/character/pack.dart';
import 'package:kinaiya_flutter/character/playback.dart';
import 'package:kinaiya_flutter/ui/widgets.dart';

import 'mascot_sprites_test.dart'
    show
        makePng,
        buildV1,
        buildV2,
        kSheetAsset,
        kFallbackAsset,
        paintedFrame,
        currentFrame,
        decoded;

const otherAsset = 'assets/character/semantic-other.png';

class RendererBundle extends AssetBundle {
  RendererBundle(this.bytes);
  final Map<String, Uint8List> bytes;
  final Map<String, Completer<ByteData>> gates = {};
  final Map<String, int> loads = {};

  @override
  Future<ByteData> load(String key) async {
    loads.update(key, (value) => value + 1, ifAbsent: () => 1);
    if (gates[key] case final gate?) return gate.future;
    return data(key);
  }

  ByteData data(String key) {
    final value = bytes[key];
    if (value == null) throw const FormatException('Missing synthetic asset');
    return ByteData.sublistView(value);
  }
}

Map<String, Object?> animation(
  List<int> frames, {
  String sheet = 'core',
  bool loop = true,
  int ms = 100,
}) => {'sheet': sheet, 'frames': frames, 'frame_ms': ms, 'loop': loop};

Map<String, Object?> variant(
  String animation, {
  int cooldown = 0,
  List<String> when = const [],
}) => {
  'animation': animation,
  'weight': 1,
  'cooldown_ms': cooldown,
  'when': when,
};

Future<(CharacterPack, RendererBundle)> fixture({bool variants = false}) async {
  final first = makePng(8, 8), second = makePng(8, 8, variant: 1);
  final bundle = RendererBundle({kSheetAsset: first, otherAsset: second});
  final manifest = jsonDecode(buildV2(first)) as Map<String, dynamic>;
  manifest['pack_schema'] = 3;
  manifest['pack_id'] = 'semantic-renderer-test';
  manifest['sheets'] = [
    ...(manifest['sheets'] as List<dynamic>),
    {
      'id': 'other',
      'asset': otherAsset,
      'sha256': sha256.convert(second).toString(),
      'width': 8,
      'height': 8,
      'frame_width': 4,
      'frame_height': 4,
    },
  ];
  manifest['animations'] = {
    'idle': animation(variants ? [0] : [0, 1, 2, 3]),
    'alternate': animation([1]),
    'listening': animation([0]),
    'thinking': animation([1]),
    'speaking': animation([2, 3], ms: 50),
    'warm': animation([1, 0]),
    'inspect': animation([2, 3]),
    'write': animation([1, 0]),
    'critical': animation([0, 1], sheet: 'other', loop: false),
    'blocked': animation([2], sheet: 'other'),
  };
  manifest['activities'] = {
    'idle': variants
        ? [
            variant('idle', cooldown: 60000),
            variant('alternate', cooldown: 60000),
          ]
        : [variant('idle')],
    'listening': [
      variant('listening', when: ['listening']),
    ],
    'speaking': [
      variant('speaking', when: ['speaking']),
    ],
    'warming': [
      variant('warm', when: ['warming']),
    ],
    'working.inspect_repo': [
      variant('inspect', when: ['working']),
    ],
    'working.write_code': [
      variant('write', when: ['working']),
    ],
    'blocked': [
      variant('blocked', when: ['blocked']),
    ],
    'alert.critical': [variant('critical')],
  };
  manifest['fallbacks'] = <String, String>{};
  final pack = await loadCharacterPack(
    bundle: bundle,
    manifestJson: jsonEncode(manifest),
  );
  expect(pack.fallback, isFalse, reason: pack.note);
  expect(pack.isSpritesheet, isTrue);
  bundle.loads.clear();
  return (pack, bundle);
}

Widget scene(
  CharacterPack pack,
  AssetBundle bundle, {
  CharacterSignals signals = const CharacterSignals(),
  bool reduced = false,
  bool visible = true,
}) => MaterialApp(
  home: MediaQuery(
    data: MediaQueryData(disableAnimations: reduced),
    child: TickerMode(
      enabled: visible,
      child: Center(
        child: MascotWidget(
          pack: pack,
          mood: 'idle',
          signals: signals,
          bundle: bundle,
        ),
      ),
    ),
  ),
);

Rect rect(int id) => Rect.fromLTWH((id % 2) * 4, (id ~/ 2) * 4, 4, 4);

Transform translation(WidgetTester tester) =>
    tester.widget<Transform>(find.byKey(const ValueKey('mascot-approach')));

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  testWidgets(
    'actual default seated idle stays on frame zero with reduced motion',
    (tester) async {
      final pack = await tester.runAsync(() => loadCharacterPack());
      expect(pack, isNotNull);
      expect(pack!.fallback, isFalse, reason: pack.note);
      final seated = pack.sheets['seated_idle'];
      expect(seated, isNotNull);
      final bytes = <String, Uint8List>{};
      for (final sheet in pack.sheets.values) {
        final data = await tester.runAsync(() => rootBundle.load(sheet.asset));
        bytes[sheet.asset] = data!.buffer.asUint8List(
          data.offsetInBytes,
          data.lengthInBytes,
        );
      }
      final bundle = RendererBundle(bytes);
      await tester.pumpWidget(scene(pack, bundle, reduced: true));
      await decoded(tester);
      final image = currentFrame(tester).image;
      expect(bundle.loads[seated!.asset], 1);
      expect(currentFrame(tester).src, seated.frameRect(0));
      expect(image.width, seated.width);
      expect(image.height, seated.height);
      await tester.pump(const Duration(seconds: 3));
      expect(currentFrame(tester).image, same(image));
      expect(currentFrame(tester).src, seated.frameRect(0));

      await tester.pumpWidget(
        scene(
          pack,
          bundle,
          signals: const CharacterSignals(listening: true),
          reduced: true,
        ),
      );
      await decoded(tester);
      expect(currentFrame(tester).src, pack.sheets['core']!.frameRect(4));
      await tester.pumpWidget(scene(pack, bundle, reduced: true));
      await decoded(tester);
      expect(currentFrame(tester).src, seated.frameRect(0));
      expect(bundle.loads[seated.asset], 1);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'actual default idle animates only the registered seated frames',
    (tester) async {
      final pack = await tester.runAsync(() => loadCharacterPack());
      expect(pack, isNotNull);
      expect(pack!.fallback, isFalse, reason: pack.note);
      final seated = pack.sheets['seated_idle']!;
      await tester.pumpWidget(scene(pack, rootBundle));
      await decoded(tester);
      final observed = <Rect>{currentFrame(tester).src};
      for (var tick = 0; tick < 25; tick++) {
        await tester.pump(const Duration(milliseconds: 110));
        observed.add(currentFrame(tester).src);
      }
      expect(observed, {seated.frameRect(0), seated.frameRect(2)});
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'semantic inputs select and advance real frames without reloading a shared sheet',
    (tester) async {
      final (pack, bundle) = await fixture();
      await tester.pumpWidget(scene(pack, bundle));
      await decoded(tester);
      final image = currentFrame(tester).image;
      expect(currentFrame(tester).src, rect(0));
      await tester.pumpWidget(
        scene(
          pack,
          bundle,
          signals: const CharacterSignals(
            working: true,
            activity: 'working.inspect_repo',
          ),
        ),
      );
      expect(currentFrame(tester).src, rect(2));
      expect(currentFrame(tester).image, same(image));
      await tester.pump(const Duration(milliseconds: 110));
      expect(currentFrame(tester).src, rect(3));
      await tester.pumpWidget(
        scene(
          pack,
          bundle,
          signals: const CharacterSignals(
            speaking: true,
            listening: true,
            working: true,
          ),
        ),
      );
      expect(currentFrame(tester).src, rect(2));
      expect(find.bySemanticsLabel('Kinaiya mascot, speaking'), findsOneWidget);
      expect(bundle.loads[kSheetAsset], 1);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'legacy mood input supplies blocked and warming variant conditions',
    (tester) async {
      final (pack, bundle) = await fixture();
      Widget legacyMood(String mood) => MaterialApp(
        home: Center(
          child: MascotWidget(pack: pack, mood: mood, bundle: bundle),
        ),
      );
      await tester.pumpWidget(legacyMood('blocked'));
      await decoded(tester);
      expect(currentFrame(tester).src, rect(2));
      expect(bundle.loads[otherAsset], 1);
      await tester.pumpWidget(legacyMood('warming'));
      await decoded(tester);
      expect(currentFrame(tester).src, rect(1));
      expect(find.byKey(const ValueKey('mascot-approach')), findsOneWidget);
      expect(find.bySemanticsLabel('Kinaiya mascot, warming'), findsOneWidget);
    },
  );

  testWidgets(
    'eligible variants change drawn frames at cycle boundaries while keeping the decoded image',
    (tester) async {
      final (pack, bundle) = await fixture(variants: true);
      await tester.pumpWidget(scene(pack, bundle));
      await decoded(tester);
      final first = currentFrame(tester);
      expect([rect(0), rect(1)], contains(first.src));
      await tester.pump(const Duration(milliseconds: 110));
      expect(currentFrame(tester).src, isNot(first.src));
      expect(currentFrame(tester).image, same(first.image));
      expect(bundle.loads[kSheetAsset], 1);
    },
  );

  testWidgets(
    'a pending different sheet cannot replace a selected verified shared sheet',
    (tester) async {
      final (pack, bundle) = await fixture();
      await tester.pumpWidget(scene(pack, bundle));
      await decoded(tester);
      final image = currentFrame(tester).image;
      final pending = Completer<ByteData>();
      bundle.gates[otherAsset] = pending;
      await tester.pumpWidget(
        scene(
          pack,
          bundle,
          signals: const CharacterSignals(activity: 'alert.critical'),
        ),
      );
      expect(paintedFrame(), findsNothing);
      await tester.pump(const Duration(milliseconds: 300));
      expect(bundle.loads[otherAsset], 1);
      await tester.pumpWidget(
        scene(pack, bundle, signals: const CharacterSignals(speaking: true)),
      );
      expect(currentFrame(tester).image, same(image));
      expect(currentFrame(tester).src, rect(2));
      await tester.pumpWidget(
        scene(
          pack,
          bundle,
          signals: const CharacterSignals(activity: 'alert.critical'),
        ),
      );
      expect(bundle.loads[otherAsset], 1);
      await tester.pumpWidget(
        scene(pack, bundle, signals: const CharacterSignals(speaking: true)),
      );
      pending.complete(bundle.data(otherAsset));
      await tester.pump();
      await tester.runAsync(
        () => Future<void>.delayed(const Duration(milliseconds: 20)),
      );
      await tester.pump();
      expect(currentFrame(tester).image, same(image));
      expect(currentFrame(tester).src.top, 4);
      expect(bundle.loads[kSheetAsset], 1);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'actual different-sheet selection decodes new art and preserves one-shot completion',
    (tester) async {
      final (pack, bundle) = await fixture();
      await tester.pumpWidget(scene(pack, bundle));
      await decoded(tester);
      final first = currentFrame(tester).image;
      const critical = CharacterSignals(activity: 'alert.critical');
      await tester.pumpWidget(scene(pack, bundle, signals: critical));
      await decoded(tester);
      expect(currentFrame(tester).image, isNot(same(first)));
      expect(currentFrame(tester).src, rect(0));
      await tester.pump(const Duration(milliseconds: 350));
      expect(currentFrame(tester).src, rect(1));
      await tester.pumpWidget(
        scene(pack, bundle, signals: critical, reduced: true),
      );
      expect(currentFrame(tester).src, rect(0));
      await tester.pumpWidget(scene(pack, bundle, signals: critical));
      expect(currentFrame(tester).src, rect(1));
      final completedPaint = tester.widget<CustomPaint>(paintedFrame());
      await tester.pump(const Duration(seconds: 2));
      expect(currentFrame(tester).src, rect(1));
      expect(tester.widget<CustomPaint>(paintedFrame()), same(completedPaint));
      expect(bundle.loads[otherAsset], 1);
    },
  );

  testWidgets(
    'bundle replacement resets selection ownership and drops an old pending sheet',
    (tester) async {
      final (pack, oldBundle) = await fixture();
      final pending = Completer<ByteData>();
      oldBundle.gates[kSheetAsset] = pending;
      await tester.pumpWidget(scene(pack, oldBundle));
      final freshBundle = RendererBundle(oldBundle.bytes);
      await tester.pumpWidget(
        scene(
          pack,
          freshBundle,
          signals: const CharacterSignals(speaking: true),
        ),
      );
      await decoded(tester);
      final winner = currentFrame(tester).image;
      pending.complete(oldBundle.data(kSheetAsset));
      await tester.pump();
      await tester.runAsync(
        () => Future<void>.delayed(const Duration(milliseconds: 20)),
      );
      await tester.pump();
      expect(currentFrame(tester).image, same(winner));
      expect(currentFrame(tester).src.top, 4);
      expect(freshBundle.loads[kSheetAsset], 1);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'warming approaches locally without changing layout and ready speech skips it immediately',
    (tester) async {
      final (pack, bundle) = await fixture();
      const warming = CharacterSignals(warming: true);
      await tester.pumpWidget(scene(pack, bundle, signals: warming));
      await decoded(tester);
      final bounds = tester.getSize(find.byType(MascotWidget));
      final dy = translation(tester).transform.storage[13];
      expect(dy, greaterThan(35));
      await tester.pump(const Duration(milliseconds: 600));
      expect(translation(tester).transform.storage[13], lessThan(dy));
      expect(translation(tester).transform.storage[13], greaterThan(0));
      expect(tester.getSize(find.byType(MascotWidget)), bounds);
      await tester.pumpWidget(
        scene(
          pack,
          bundle,
          signals: const CharacterSignals(warming: true, speaking: true),
        ),
      );
      expect(find.byKey(const ValueKey('mascot-approach')), findsNothing);
      expect(currentFrame(tester).src, rect(2));
      expect(tester.getSize(find.byType(MascotWidget)), bounds);
      expect(find.bySemanticsLabel('Kinaiya mascot, speaking'), findsOneWidget);
    },
  );

  testWidgets(
    'reduced motion fixes the selected semantic frame and disables approach',
    (tester) async {
      final (pack, bundle) = await fixture();
      await tester.pumpWidget(
        scene(
          pack,
          bundle,
          signals: const CharacterSignals(warming: true),
          reduced: true,
        ),
      );
      await decoded(tester);
      expect(currentFrame(tester).src, rect(1));
      expect(find.byKey(const ValueKey('mascot-approach')), findsNothing);
      await tester.pump(const Duration(seconds: 2));
      expect(currentFrame(tester).src, rect(1));
      await tester.pumpWidget(
        scene(
          pack,
          bundle,
          signals: const CharacterSignals(speaking: true),
          reduced: true,
        ),
      );
      expect(currentFrame(tester).src, rect(2));
      await tester.pump(const Duration(seconds: 2));
      expect(currentFrame(tester).src, rect(2));
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'reduced motion follows a changed work semantic without retaining an old costume',
    (tester) async {
      final (pack, bundle) = await fixture();
      await tester.pumpWidget(
        scene(
          pack,
          bundle,
          signals: const CharacterSignals(
            working: true,
            activity: 'working.inspect_repo',
          ),
          reduced: true,
        ),
      );
      await decoded(tester);
      expect(currentFrame(tester).src, rect(2));
      await tester.pump(const Duration(seconds: 1));
      await tester.pumpWidget(
        scene(
          pack,
          bundle,
          signals: const CharacterSignals(
            working: true,
            activity: 'working.write_code',
          ),
          reduced: true,
        ),
      );
      expect(currentFrame(tester).src, rect(1));
      expect(
        find.bySemanticsLabel('Kinaiya mascot, working.write_code'),
        findsOneWidget,
      );
      expect(bundle.loads[kSheetAsset], 1);
    },
  );

  testWidgets(
    'hidden animation freezes variant selection and resumes without cooldown catch-up',
    (tester) async {
      final (pack, bundle) = await fixture(variants: true);
      await tester.pumpWidget(scene(pack, bundle));
      await decoded(tester);
      await tester.pump(const Duration(milliseconds: 40));
      final first = currentFrame(tester);
      await tester.pumpWidget(scene(pack, bundle, visible: false));
      await tester.pump(const Duration(seconds: 65));
      await tester.pumpWidget(
        scene(
          pack,
          bundle,
          signals: const CharacterSignals(
            working: true,
            activity: 'working.inspect_repo',
          ),
          visible: false,
        ),
      );
      expect(currentFrame(tester).src, first.src);
      await tester.pumpWidget(scene(pack, bundle));
      expect(currentFrame(tester).src, first.src);
      await tester.pump(const Duration(milliseconds: 110));
      expect(currentFrame(tester).src, isNot(first.src));
      expect(currentFrame(tester).image, same(first.image));
      expect(bundle.loads[kSheetAsset], 1);
    },
  );

  testWidgets(
    'a failed sheet is bounded and switching to verified art recovers',
    (tester) async {
      final (pack, bundle) = await fixture();
      await tester.pumpWidget(scene(pack, bundle));
      await decoded(tester);
      final image = currentFrame(tester).image;
      bundle.bytes[otherAsset] = Uint8List.fromList([0, 1, 2]);
      await tester.pumpWidget(
        scene(pack, bundle, signals: const CharacterSignals(blocked: true)),
      );
      await tester.pump();
      expect(
        find.text('Animation unavailable; showing original mascot.'),
        findsOneWidget,
      );
      await tester.pump(const Duration(seconds: 2));
      expect(bundle.loads[otherAsset], 1);
      await tester.pumpWidget(
        scene(pack, bundle, signals: const CharacterSignals(speaking: true)),
      );
      expect(currentFrame(tester).image, same(image));
      expect(currentFrame(tester).src, rect(2));
      expect(
        find.text('Animation unavailable; showing original mascot.'),
        findsNothing,
      );
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'legacy decorative motion remains animated and respects hidden and reduced states',
    (tester) async {
      final png = makePng(8, 8);
      final bundle = RendererBundle({kFallbackAsset: png});
      final pack = await loadCharacterPack(
        bundle: bundle,
        manifestJson: buildV1(png),
      );
      final legacy = find.byKey(const ValueKey('mascot-legacy-scale'));
      await tester.pumpWidget(scene(pack, bundle));
      final first = tester.widget<Transform>(legacy).transform;
      await tester.pump(const Duration(milliseconds: 200));
      expect(tester.widget<Transform>(legacy).transform, isNot(first));
      final animated = tester.widget<Transform>(legacy).transform;
      await tester.pumpWidget(scene(pack, bundle, visible: false));
      await tester.pump(const Duration(seconds: 10));
      expect(tester.widget<Transform>(legacy).transform, animated);
      await tester.pumpWidget(scene(pack, bundle));
      expect(tester.widget<Transform>(legacy).transform, animated);
      await tester.pumpWidget(scene(pack, bundle, reduced: true));
      expect(legacy, findsNothing);
      expect(tester.takeException(), isNull);
    },
  );
}
