import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/presentation/controller.dart';
import 'package:kinaiya_flutter/presentation/storage.dart' as storage;
import 'package:kinaiya_flutter/presentation/storage_native.dart';

import 'character_package_test.dart' as fixtures;

const _channel = MethodChannel('dev.kinaiya/presentation_storage_v1');
const _limit = 13 * 1024 * 1024;

Matcher _code(String code) => isA<PlatformException>()
    .having((e) => e.code, 'code', code)
    .having((e) => e.details, 'details', isNull)
    .having((e) => e.message, 'message', isNot(contains('private')));

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  final messenger =
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;

  tearDown(() => messenger.setMockMethodCallHandler(_channel, null));

  test('unsupported operating systems retain session storage', () async {
    if (Platform.isAndroid || Platform.isIOS) return;
    final local = storage.createPresentationStorage();
    expect(local.persistent, isFalse);
    await local.write('session only');
    expect(await local.read(), 'session only');
  });

  test(
    'fixed read/write channel round-trips complete Unicode snapshots',
    () async {
      String? saved;
      final calls = <MethodCall>[];
      messenger.setMockMethodCallHandler(_channel, (call) async {
        calls.add(call);
        if (call.method == 'read') return saved;
        saved = (call.arguments as Map)['snapshot'] as String;
        return null;
      });
      final local = NativePresentationStorage();
      expect(local.persistent, isTrue);
      expect(await local.read(), isNull);
      const value = '{"name":"Moon 🌙 café 漢字"}';
      await local.write(value);
      expect(await local.read(), value);
      expect(calls.map((c) => c.method), ['read', 'write', 'read']);
      expect(calls.first.arguments, isNull);
      expect(calls[1].arguments, {'snapshot': value});
    },
  );

  test('accepts the exact byte limit and rejects multibyte overflow', () async {
    var writes = 0;
    messenger.setMockMethodCallHandler(_channel, (call) async {
      expect(call.method, 'write');
      writes++;
      expect((call.arguments as Map)['snapshot'], hasLength(_limit));
      return null;
    });
    final local = NativePresentationStorage();
    await local.write('a' * _limit);
    await expectLater(
      local.write('🌙' * ((_limit ~/ 4) + 1)),
      throwsA(_code('presentation_invalid')),
    );
    await expectLater(
      local.write('a' * (_limit + 1)),
      throwsA(_code('presentation_invalid')),
    );
    expect(writes, 1);
  });

  test('unpaired surrogates are rejected before channel encoding', () async {
    var calls = 0;
    messenger.setMockMethodCallHandler(_channel, (_) async {
      calls++;
      return null;
    });
    final local = NativePresentationStorage();
    for (final value in [
      String.fromCharCode(0xd800),
      String.fromCharCode(0xdc00),
      String.fromCharCodes([0xd800, 0x61]),
    ]) {
      await expectLater(
        local.write(value),
        throwsA(_code('presentation_invalid')),
      );
    }
    expect(calls, 0);
  });

  test(
    'malformed channel response types cannot become a saved state',
    () async {
      messenger.setMockMethodCallHandler(
        _channel,
        (call) async => call.method == 'read' ? 42 : 'private unexpected ack',
      );
      final local = NativePresentationStorage();
      await expectLater(local.read(), throwsA(_code('presentation_invalid')));
      await expectLater(
        local.write('{}'),
        throwsA(_code('presentation_invalid')),
      );
    },
  );

  test('oversize native response is refused independently', () async {
    messenger.setMockMethodCallHandler(
      _channel,
      (_) async => '🌙' * ((_limit ~/ 4) + 1),
    );
    await expectLater(
      NativePresentationStorage().read(),
      throwsA(_code('presentation_invalid')),
    );
  });

  test('missing and failing plugins expose only fixed failures', () async {
    final local = NativePresentationStorage();
    await expectLater(local.read(), throwsA(_code('presentation_unavailable')));
    messenger.setMockMethodCallHandler(_channel, (_) async {
      throw PlatformException(
        code: 'private path',
        message: 'private file contents',
        details: {'private': 'data'},
      );
    });
    await expectLater(local.read(), throwsA(_code('presentation_unavailable')));
    messenger.setMockMethodCallHandler(_channel, (_) async {
      throw PlatformException(
        code: 'presentation_invalid',
        message: 'private malformed file',
      );
    });
    await expectLater(local.read(), throwsA(_code('presentation_invalid')));
  });

  test(
    'timeouts retain bounded ownership and order across instances',
    () async {
      final started = Completer<void>();
      final release = Completer<String?>();
      final secondDispatched = Completer<void>();
      final calls = <String>[];
      String? saved;
      messenger.setMockMethodCallHandler(_channel, (call) async {
        calls.add(call.method);
        if (calls.length == 1) {
          started.complete();
          return release.future;
        }
        if (call.method == 'write') {
          saved = (call.arguments as Map)['snapshot'] as String;
          secondDispatched.complete();
          return null;
        }
        return saved;
      });
      final first = NativePresentationStorage(
        responseTimeout: const Duration(milliseconds: 30),
      ).read();
      final firstCheck = expectLater(
        first,
        throwsA(_code('presentation_unavailable')),
      );
      await started.future;
      final second = NativePresentationStorage(
        responseTimeout: const Duration(milliseconds: 30),
      ).write('newest');
      final secondCheck = expectLater(
        second,
        throwsA(_code('presentation_unavailable')),
      );
      try {
        await Future.wait([firstCheck, secondCheck]);
        expect(calls, ['read']);
        await expectLater(
          NativePresentationStorage().read(),
          throwsA(_code('presentation_busy')),
        );
      } finally {
        release.complete('older read');
      }
      await secondDispatched.future;
      expect(await NativePresentationStorage().read(), 'newest');
      expect(calls, ['read', 'write', 'read']);
    },
  );

  test(
    'recreated controller restores validated artwork and preferences',
    () async {
      String? saved;
      messenger.setMockMethodCallHandler(_channel, (call) async {
        if (call.method == 'read') return saved;
        saved = (call.arguments as Map)['snapshot'] as String;
        return null;
      });
      final original = PresentationController(
        storage: NativePresentationStorage(),
        defaultBundle: fixtures.defaultFixture(),
      );
      await original.initialize();
      await original.useCharacter(
        await original.inspectPackage(jsonEncode(fixtures.packageFixture())),
      );
      await original.setPreferences(
        const PresentationPreferences(
          reducedMotion: true,
          primaryOverride: '#141091',
          showCaptions: false,
        ),
      );
      original.dispose();
      expect((jsonDecode(saved!) as Map).keys.toSet(), {
        'state_schema',
        'preferences',
        'package',
      });
      final restored = PresentationController(
        storage: NativePresentationStorage(),
        defaultBundle: fixtures.defaultFixture(),
      );
      addTearDown(restored.dispose);
      await restored.initialize();
      expect(restored.selected!.pack.name, 'Local character');
      expect(restored.preferences.reducedMotion, isTrue);
      expect(restored.preferences.primaryOverride, '#141091');
      expect(restored.preferences.showCaptions, isFalse);
      expect(restored.notice, isNull);
    },
  );

  test(
    'corrupt restore and failed save retain usable session settings',
    () async {
      messenger.setMockMethodCallHandler(_channel, (call) async {
        if (call.method == 'read') return '{"private":"invalid snapshot"}';
        throw PlatformException(
          code: 'presentation_unavailable',
          message: 'private filesystem error',
        );
      });
      final controller = PresentationController(
        storage: NativePresentationStorage(),
        defaultBundle: fixtures.defaultFixture(),
      );
      addTearDown(controller.dispose);
      await controller.initialize();
      expect(controller.selected!.pack.name, 'Bundled character');
      expect(controller.loading, isFalse);
      expect(controller.notice, contains('could not be restored'));
      expect(controller.notice, isNot(contains('private')));
      await controller.setPreferences(
        const PresentationPreferences(showCharacter: false),
      );
      expect(controller.preferences.showCharacter, isFalse);
      expect(controller.notice, contains('saving locally is unconfirmed'));
      expect(controller.notice, isNot(contains('private')));
    },
  );
}
