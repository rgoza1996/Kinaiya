import 'dart:async';
import 'dart:convert';

import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/character/pack.dart';
import 'package:kinaiya_flutter/presentation/controller.dart';
import 'package:kinaiya_flutter/presentation/platform.dart';

import 'character_package_test.dart' as fixtures;

class _Storage implements PresentationStorage {
  String? value;
  Completer<String?>? pendingRead;
  Completer<void>? pendingWrite;
  final writes = <String>[];
  bool failWrite = false;
  bool failRead = false;
  @override
  bool get persistent => true;
  @override
  Future<String?> read() async {
    if (failRead) throw StateError('private backend details');
    return pendingRead == null ? value : pendingRead!.future;
  }

  @override
  Future<void> write(String value) async {
    writes.add(value);
    final pending = pendingWrite;
    pendingWrite = null;
    if (pending != null) await pending.future;
    if (failWrite) throw StateError('private backend details');
    this.value = value;
  }
}

class _DelayedBundle extends AssetBundle {
  _DelayedBundle(this.delegate);
  final AssetBundle delegate;
  final ready = Completer<void>();
  @override
  Future<ByteData> load(String key) async {
    await ready.future;
    return delegate.load(key);
  }
}

String _snapshot({
  String? package,
  PresentationPreferences preferences = const PresentationPreferences(),
}) => jsonEncode({
  'state_schema': 1,
  'preferences': preferences.toJson(),
  'package': package,
});

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test(
    'inspection is inert and activation persists only presentation data',
    () async {
      final storage = _Storage();
      final controller = PresentationController(
        storage: storage,
        defaultBundle: fixtures.defaultFixture(),
      );
      addTearDown(controller.dispose);
      await controller.initialize();
      final original = controller.selected;
      final inspected = await controller.inspectPackage(
        jsonEncode(fixtures.packageFixture()),
      );
      expect(controller.selected, same(original));
      expect(storage.writes, isEmpty);
      await controller.useCharacter(inspected);
      await controller.setPreferences(
        const PresentationPreferences(
          reducedMotion: true,
          primaryOverride: '#123456',
        ),
      );
      final saved = jsonDecode(storage.value!) as Map;
      expect(saved.keys.toSet(), {'state_schema', 'preferences', 'package'});
      expect(saved['package'], inspected.packageJson);
      expect(controller.selected, same(inspected));
      await controller.useDefault();
      expect(controller.selected!.packageJson, isNull);
      expect((jsonDecode(storage.value!) as Map)['package'], isNull);
      expect(controller.preferences.reducedMotion, isTrue);
      expect(await controller.exportPackage(), contains('source_asset'));
    },
  );

  test('restores only a fully revalidated snapshot', () async {
    final raw = jsonEncode(fixtures.packageFixture());
    final storage = _Storage()
      ..value = _snapshot(
        package: raw,
        preferences: const PresentationPreferences(
          showCharacter: false,
          backgroundOverride: '#112233',
        ),
      );
    final controller = PresentationController(
      storage: storage,
      defaultBundle: fixtures.defaultFixture(),
    );
    addTearDown(controller.dispose);
    await controller.initialize();
    expect(controller.selected!.pack.name, 'Local character');
    expect(controller.preferences.showCharacter, isFalse);
    expect(controller.preferences.backgroundOverride, '#112233');
    expect(controller.notice, isNull);
    expect(storage.writes, isEmpty);
  });

  test('invalid saved package discards the entire snapshot safely', () async {
    final storage = _Storage()
      ..value = _snapshot(
        package: '{"secret":"never echo"}',
        preferences: const PresentationPreferences(showCharacter: false),
      );
    final controller = PresentationController(
      storage: storage,
      defaultBundle: fixtures.defaultFixture(),
    );
    addTearDown(controller.dispose);
    await controller.initialize();
    expect(controller.selected!.pack.name, 'Bundled character');
    expect(controller.preferences.showCharacter, isTrue);
    expect(controller.notice, contains('could not be restored'));
    expect(controller.notice, isNot(contains('secret')));
    expect(controller.loading, isFalse);
  });

  test(
    'a delayed restore cannot overwrite a newer character or preferences',
    () async {
      final storage = _Storage()..pendingRead = Completer<String?>();
      final controller = PresentationController(
        storage: storage,
        defaultBundle: fixtures.defaultFixture(),
      );
      addTearDown(controller.dispose);
      final initializing = controller.initialize();
      final selection = await controller.inspectPackage(
        jsonEncode(fixtures.packageFixture(name: 'Newest')),
      );
      await controller.useCharacter(selection);
      await controller.setPreferences(
        const PresentationPreferences(reducedMotion: true),
      );
      storage.pendingRead!.complete(
        _snapshot(package: jsonEncode(fixtures.packageFixture(name: 'Old'))),
      );
      await initializing;
      expect(controller.selected, same(selection));
      expect(controller.preferences.reducedMotion, isTrue);
      expect(controller.loading, isFalse);
    },
  );

  test(
    'delayed default for preferences cannot replace a newly activated pack',
    () async {
      final bundle = _DelayedBundle(fixtures.defaultFixture());
      final controller = PresentationController(
        storage: _Storage(),
        defaultBundle: bundle,
      );
      addTearDown(controller.dispose);
      final preferences = controller.setPreferences(
        const PresentationPreferences(reducedMotion: true),
      );
      final selection = await controller.inspectPackage(
        jsonEncode(fixtures.packageFixture(name: 'Newest')),
      );
      await controller.useCharacter(selection);
      bundle.ready.complete();
      await preferences;
      expect(controller.selected, same(selection));
      expect(controller.preferences.reducedMotion, isTrue);
      expect(controller.loading, isFalse);
    },
  );

  test('writes serialize so a slow old write cannot win', () async {
    final storage = _Storage();
    final controller = PresentationController(
      storage: storage,
      defaultBundle: fixtures.defaultFixture(),
    );
    addTearDown(controller.dispose);
    await controller.initialize();
    final gate = Completer<void>();
    storage.pendingWrite = gate;
    final first = controller.setPreferences(
      const PresentationPreferences(showCaptions: false),
    );
    await Future<void>.delayed(Duration.zero);
    expect(storage.writes, hasLength(1));
    final last = controller.setPreferences(
      const PresentationPreferences(reducedMotion: true),
    );
    await Future<void>.delayed(Duration.zero);
    expect(storage.writes, hasLength(1));
    gate.complete();
    await Future.wait([first, last]);
    expect(storage.writes, hasLength(2));
    final saved = (jsonDecode(storage.value!) as Map)['preferences'] as Map;
    expect(saved['reduced_motion'], isTrue);
    expect(saved['show_captions'], isTrue);
  });

  test(
    'storage failures keep session usable and show generic notice',
    () async {
      final storage = _Storage()..failRead = true;
      final controller = PresentationController(
        storage: storage,
        defaultBundle: fixtures.defaultFixture(),
      );
      addTearDown(controller.dispose);
      await controller.initialize();
      expect(controller.selected!.pack.fallback, isFalse);
      storage.failWrite = true;
      await controller.setPreferences(
        const PresentationPreferences(showCharacter: false),
      );
      expect(controller.preferences.showCharacter, isFalse);
      expect(controller.notice, contains('saving locally is unconfirmed'));
      expect(controller.notice, isNot(contains('private')));
      storage.failWrite = false;
      await controller.setPreferences(
        const PresentationPreferences(showCharacter: true),
      );
      expect(controller.notice, isNull);
    },
  );

  test(
    'disposed controller ignores pending restore and emits no notifications',
    () async {
      final storage = _Storage()..pendingRead = Completer<String?>();
      final controller = PresentationController(
        storage: storage,
        defaultBundle: fixtures.defaultFixture(),
      );
      var notifications = 0;
      controller.addListener(() => notifications++);
      final initializing = controller.initialize();
      controller.dispose();
      storage.pendingRead!.complete(null);
      await initializing;
      expect(notifications, 0);
    },
  );

  test('native session storage and import capabilities are honest', () async {
    final controller = PresentationController(
      defaultBundle: fixtures.defaultFixture(),
    );
    addTearDown(controller.dispose);
    await controller.initialize();
    expect(controller.persistent, isFalse);
    expect(presentationImportSupported, isFalse);
    expect(controller.notice, contains('this session'));
    expect(await pickCharacterPackage(), isNull);
    await expectLater(downloadCharacterPackage('{}'), throwsUnsupportedError);
  });

  test('invalid override cannot change or persist presentation', () async {
    final storage = _Storage();
    final controller = PresentationController(
      storage: storage,
      defaultBundle: fixtures.defaultFixture(),
    );
    addTearDown(controller.dispose);
    await controller.initialize();
    await expectLater(
      controller.setPreferences(
        const PresentationPreferences(primaryOverride: 'red'),
      ),
      throwsFormatException,
    );
    expect(controller.preferences.primaryOverride, isNull);
    expect(storage.writes, isEmpty);
    expect(
      const PresentationPreferences(
        primaryOverride: '#123456',
      ).copyWith(clearOverrides: true).primaryOverride,
      isNull,
    );
    expect(packSchemaV2, 2);
  });

  test('actions before initialize also take precedence over restore', () async {
    final storage = _Storage()..value = _snapshot();
    final controller = PresentationController(
      storage: storage,
      defaultBundle: fixtures.defaultFixture(),
    );
    addTearDown(controller.dispose);
    await controller.setPreferences(
      const PresentationPreferences(showCharacter: false),
    );
    storage.value = _snapshot();
    await controller.initialize();
    expect(controller.preferences.showCharacter, isFalse);
    expect(controller.loading, isFalse);
  });

  test('restores old six-key snapshot without rewriting on read', () async {
    final oldPreferences = {
      'reduced_motion': true,
      'show_character': false,
      'show_captions': true,
      'use_pack_palette': false,
      'primary_override': '#123456',
      'background_override': '#112233',
    };
    final storage = _Storage()
      ..value = jsonEncode({
        'state_schema': 1,
        'preferences': oldPreferences,
        'package': jsonEncode(fixtures.packageFixture()),
      });
    final controller = PresentationController(
      storage: storage,
      defaultBundle: fixtures.defaultFixture(),
    );
    addTearDown(controller.dispose);
    await controller.initialize();
    expect(controller.preferences.reducedMotion, isTrue);
    expect(controller.preferences.showCharacter, isFalse);
    expect(controller.preferences.showConverseView, isTrue);
    expect(controller.preferences.primaryOverride, '#123456');
    expect(controller.preferences.backgroundOverride, '#112233');
    expect(controller.preferences.usePackPalette, isFalse);
    expect(controller.notice, isNull);
    expect(storage.writes, isEmpty);
  });

  test(
    'persists converse false and restores it with pack and colors',
    () async {
      final storage = _Storage();
      final controller = PresentationController(
        storage: storage,
        defaultBundle: fixtures.defaultFixture(),
      );
      addTearDown(controller.dispose);
      await controller.initialize();
      final imported = await controller.inspectPackage(
        jsonEncode(fixtures.packageFixture()),
      );
      await controller.useCharacter(imported);
      await controller.setPreferences(
        const PresentationPreferences(
          showConverseView: false,
          showCharacter: false,
          primaryOverride: '#123456',
          backgroundOverride: '#112233',
        ),
      );
      final saved = jsonDecode(storage.value!) as Map;
      final savedPreferences = saved['preferences'] as Map;
      expect(savedPreferences['show_converse_view'], isFalse);
      expect(saved['state_schema'], 1);
      expect(saved['package'], imported.packageJson);

      final reader = PresentationController(
        storage: storage,
        defaultBundle: fixtures.defaultFixture(),
      );
      addTearDown(reader.dispose);
      await reader.initialize();
      expect(reader.selected!.pack.name, 'Local character');
      expect(reader.preferences.showConverseView, isFalse);
      expect(reader.preferences.showCharacter, isFalse);
      expect(reader.preferences.primaryOverride, '#123456');
      expect(reader.preferences.backgroundOverride, '#112233');
      expect(reader.notice, isNull);
    },
  );
}
