import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/config/transport_problem.dart';
import 'package:kinaiya_flutter/distribution/build_identity.dart';
import 'package:kinaiya_flutter/distribution/release_check.dart';
import 'package:kinaiya_flutter/distribution/release_launcher.dart';
import 'package:kinaiya_flutter/distribution/release_panel.dart';

const _sha = '0123456789abcdef0123456789abcdef01234567';
const _sha2 = 'fedcba9876543210fedcba9876543210fedcba98';

BuildIdentity nightly(int build, [String sha = _sha]) => BuildIdentity.parse(
  channel: 'nightly',
  version: '0.1.0-nightly.$build+${sha.substring(0, 12)}',
  buildNumber: '$build',
  sourceSha: sha,
);

BuildIdentity stable(String version) => BuildIdentity.parse(
  channel: 'stable',
  version: version,
  buildNumber: '77',
  sourceSha: _sha,
);

Map<String, Object?> release(
  String tag, {
  bool prerelease = true,
  List<String> assets = const [],
  String? htmlUrl,
}) => {
  'tag_name': tag,
  'draft': false,
  'prerelease': prerelease,
  'html_url': htmlUrl ?? '$releasesRoot/tag/$tag',
  'assets': [
    for (final name in assets)
      {
        'name': name,
        'state': 'uploaded',
        'browser_download_url': '$releasesRoot/download/$tag/$name',
      },
  ],
};

ReleaseChecker checker(
  BuildIdentity identity,
  http.Response Function(http.Request) respond, {
  DistributedApp app = DistributedApp.kinaiya,
  MobilePlatform platform = MobilePlatform.android,
  List<Uri>? requested,
}) => ReleaseChecker(
  identity: identity,
  app: app,
  platform: platform,
  clientFactory: () => MockClient((request) async {
    requested?.add(request.url);
    expect(request.headers.containsKey('Authorization'), isFalse);
    return respond(request);
  }),
);

http.Response json(Object? body, [int status = 200]) =>
    http.Response(jsonEncode(body), status);

void main() {
  group('BuildIdentity', () {
    test('missing or malformed defines are a development build', () {
      expect(BuildIdentity.fromEnvironment().isRelease, isFalse);
      for (final identity in [
        BuildIdentity.parse(
          channel: 'beta',
          version: '0.1.0',
          buildNumber: '1',
          sourceSha: _sha,
        ),
        BuildIdentity.parse(
          channel: 'stable',
          version: '0.1.0',
          buildNumber: '0',
          sourceSha: _sha,
        ),
        BuildIdentity.parse(
          channel: 'stable',
          version: '0.1.0',
          buildNumber: '01',
          sourceSha: _sha,
        ),
        BuildIdentity.parse(
          channel: 'stable',
          version: '0.1.0',
          buildNumber: '1',
          sourceSha: 'abc',
        ),
        BuildIdentity.parse(
          channel: 'stable',
          version: '0.1.0-alpha',
          buildNumber: '1',
          sourceSha: _sha,
        ),
        // Nightly version must embed its own build number and source prefix.
        BuildIdentity.parse(
          channel: 'nightly',
          version: '0.1.0-nightly.9+${_sha.substring(0, 12)}',
          buildNumber: '8',
          sourceSha: _sha,
        ),
      ]) {
        expect(identity.channel, DistributionChannel.development);
        expect(identity.label, 'Development build');
        expect(identity.releaseTag, isNull);
      }
    });

    test('valid identities derive the immutable release tag', () {
      expect(nightly(12).releaseTag, 'nightly-12-0123456789ab');
      expect(stable('1.2.3').releaseTag, 'v1.2.3');
      expect(nightly(12).label, contains('nightly build 12'));
    });

    test('asset names match the release workflow contract', () {
      expect(
        mobileAssetName(
          DistributedApp.kinaiya,
          MobilePlatform.android,
          'nightly-3-0123456789ab',
        ),
        'kinaiya-nightly-3-0123456789ab-android.apk',
      );
      expect(
        mobileAssetName(DistributedApp.companion, MobilePlatform.ios, 'v1.0.0'),
        'kinaiya-companion-v1.0.0-ios-sideload-unsigned.ipa',
      );
    });
  });

  group('isOfficialReleaseUrl', () {
    test('accepts only official immutable tag pages', () {
      expect(isOfficialReleaseUrl('$releasesRoot/tag/v1.2.3'), isTrue);
      expect(
        isOfficialReleaseUrl('$releasesRoot/tag/nightly-5-a006c612f9a8'),
        isTrue,
      );
      for (final url in [
        '$releasesRoot/latest',
        '$releasesRoot/tag/v1.2.3-alpha',
        '$releasesRoot/tag/nightly-5-a006c612f9a8/../../evil',
        'https://github.com/someone/Kinaiya/releases/tag/v1.2.3',
        'http://github.com/rgoza1996/Kinaiya/releases/tag/v1.2.3',
        '$releasesRoot/download/v1.2.3/kinaiya-v1.2.3-android.apk',
      ]) {
        expect(isOfficialReleaseUrl(url), isFalse, reason: url);
      }
    });
  });

  group('ReleaseChecker', () {
    test('development builds never contact the feed', () async {
      final requested = <Uri>[];
      final result = await checker(
        const BuildIdentity.development(),
        (_) => fail('no request expected'),
        requested: requested,
      ).check();
      expect(result.state, ReleaseCheckState.unsupported);
      expect(requested, isEmpty);
    });

    test('newer nightly with this platform asset is available', () async {
      const tag = 'nightly-13-fedcba987654';
      final requested = <Uri>[];
      final result = await checker(nightly(12), (_) {
        return json([
          release('v0.0.9', prerelease: false),
          release(tag, assets: ['kinaiya-$tag-android.apk']),
          release(
            'nightly-12-0123456789ab',
            assets: ['kinaiya-nightly-12-0123456789ab-android.apk'],
          ),
        ]);
      }, requested: requested).check();
      expect(result.state, ReleaseCheckState.available);
      expect(result.releaseUrl, '$releasesRoot/tag/$tag');
      expect(requested.single.toString(), '$releasesApi?per_page=100&page=1');
    });

    test(
      'same nightly build is current only when the identity matches',
      () async {
        final same = await checker(
          nightly(12),
          (_) => json([release('nightly-12-0123456789ab')]),
        ).check();
        expect(same.state, ReleaseCheckState.current);
        final conflicting = await checker(
          nightly(12),
          (_) => json([release('nightly-12-${_sha2.substring(0, 12)}')]),
        ).check();
        expect(conflicting.state, ReleaseCheckState.invalid);
      },
    );

    test(
      'newer release without this app/platform artifact is not offered',
      () async {
        const tag = 'nightly-14-fedcba987654';
        final result = await checker(
          nightly(12),
          (_) => json([
            release(tag, assets: ['kinaiya-$tag-android.apk']),
          ]),
          app: DistributedApp.companion,
        ).check();
        expect(result.state, ReleaseCheckState.noPlatformBuild);
        expect(result.releaseUrl, isNull);
      },
    );

    test('asset with a foreign download URL is not trusted', () async {
      const tag = 'nightly-14-fedcba987654';
      final entry = release(tag);
      entry['assets'] = [
        {
          'name': 'kinaiya-$tag-android.apk',
          'state': 'uploaded',
          'browser_download_url':
              'https://example.com/kinaiya-$tag-android.apk',
        },
      ];
      final result = await checker(nightly(12), (_) => json([entry])).check();
      expect(result.state, ReleaseCheckState.noPlatformBuild);
    });

    test('older published nightly is never offered as a downgrade', () async {
      final result = await checker(
        nightly(12),
        (_) => json([
          release(
            'nightly-11-0123456789ab',
            assets: ['kinaiya-nightly-11-0123456789ab-android.apk'],
          ),
        ]),
      ).check();
      expect(result.state, ReleaseCheckState.olderThanInstalled);
      expect(result.releaseUrl, isNull);
    });

    test('duplicate nightly build identities are rejected', () async {
      final result = await checker(
        nightly(12),
        (_) => json([
          release('nightly-13-0123456789ab'),
          release('nightly-13-fedcba987654'),
        ]),
      ).check();
      expect(result.state, ReleaseCheckState.invalid);
    });

    test('mismatched html_url fails identity checks', () async {
      final result = await checker(
        nightly(12),
        (_) => json([
          release('nightly-13-0123456789ab', htmlUrl: 'https://evil.example/x'),
        ]),
      ).check();
      expect(result.state, ReleaseCheckState.invalid);
    });

    test('stable compares semantic versions from /releases/latest', () async {
      final requested = <Uri>[];
      final newer = await checker(
        stable('1.2.3'),
        (_) => json(
          release(
            'v1.10.0',
            prerelease: false,
            assets: ['kinaiya-v1.10.0-ios-sideload-unsigned.ipa'],
          ),
        ),
        platform: MobilePlatform.ios,
        requested: requested,
      ).check();
      expect(newer.state, ReleaseCheckState.available);
      expect(requested.single.toString(), '$releasesApi/latest');
      final current = await checker(
        stable('1.2.3'),
        (_) => json(release('v1.2.3', prerelease: false)),
      ).check();
      expect(current.state, ReleaseCheckState.current);
    });

    test('stable rejects prerelease entries from /releases/latest', () async {
      final result = await checker(
        stable('1.2.3'),
        (_) => json(release('v1.3.0', prerelease: true)),
      ).check();
      expect(result.state, ReleaseCheckState.invalid);
    });

    test(
      'absent, rate-limited, offline and malformed feeds are never up to date',
      () async {
        final cases = <ReleaseCheckState, http.Response Function(http.Request)>{
          ReleaseCheckState.absent: (_) => json({'message': 'Not Found'}, 404),
          ReleaseCheckState.unavailable: (_) =>
              json({'message': 'rate limited'}, 403),
          ReleaseCheckState.invalid: (_) => http.Response('{not json', 200),
        };
        for (final entry in cases.entries) {
          final result = await checker(stable('1.2.3'), entry.value).check();
          expect(result.state, entry.key);
          expect(result.releaseUrl, isNull);
        }
        final offline = await ReleaseChecker(
          identity: stable('1.2.3'),
          app: DistributedApp.kinaiya,
          platform: MobilePlatform.android,
          clientFactory: () =>
              MockClient((_) async => throw http.ClientException('offline')),
        ).check();
        expect(offline.state, ReleaseCheckState.unavailable);
        final noNightly = await checker(
          nightly(3),
          (_) => json([release('v1.0.0', prerelease: false)]),
        ).check();
        expect(noNightly.state, ReleaseCheckState.absent);
      },
    );

    test('oversized feed responses are rejected', () async {
      final result = await checker(
        nightly(3),
        (_) => http.Response(
          '[${List.filled(300000, '"xxxxxxx"').join(',')}]',
          200,
        ),
      ).check();
      expect(result.state, ReleaseCheckState.invalid);
    });
  });

  group('ReleasePageLauncher', () {
    const channel = MethodChannel('kinaiya/distribution');
    tearDown(
      () => TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(channel, null),
    );

    test('only official URLs reach the platform channel', () async {
      final calls = <MethodCall>[];
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(channel, (call) async {
            calls.add(call);
            return true;
          });
      const launcher = ReleasePageLauncher();
      expect(await launcher.open('https://evil.example/apk'), isFalse);
      expect(calls, isEmpty);
      expect(await launcher.open('$releasesRoot/tag/v1.0.0'), isTrue);
      expect(calls.single.method, 'openReleasePage');
      expect(calls.single.arguments, '$releasesRoot/tag/v1.0.0');
    });

    test(
      'missing native handler reports failure instead of throwing',
      () async {
        expect(
          await const ReleasePageLauncher().open('$releasesRoot/tag/v1.0.0'),
          isFalse,
        );
      },
    );
  });

  group('BuildAndUpdatesPanel', () {
    testWidgets('development build shows no update control', (tester) async {
      await tester.pumpWidget(
        const MaterialApp(
          home: Scaffold(
            body: BuildAndUpdatesPanel(
              app: DistributedApp.kinaiya,
              identity: BuildIdentity.development(),
              platform: MobilePlatform.android,
            ),
          ),
        ),
      );
      expect(find.text('Development build'), findsOneWidget);
      expect(find.byKey(const ValueKey('check-updates')), findsNothing);
    });

    testWidgets(
      'release build checks only when asked and offers the release page',
      (tester) async {
        var checks = 0;
        const tag = 'nightly-13-fedcba987654';
        await tester.pumpWidget(
          MaterialApp(
            home: Scaffold(
              body: BuildAndUpdatesPanel(
                app: DistributedApp.kinaiya,
                identity: nightly(12),
                platform: MobilePlatform.android,
                checkerFactory: (identity, app, platform) {
                  checks++;
                  return checker(
                    identity,
                    (_) => json([
                      release(tag, assets: ['kinaiya-$tag-android.apk']),
                    ]),
                  );
                },
              ),
            ),
          ),
        );
        expect(find.textContaining('nightly build 12'), findsOneWidget);
        expect(checks, 0);
        await tester.tap(find.byKey(const ValueKey('check-updates')));
        // The HTTP mock completes on real async; let it settle outside fake time.
        await tester.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 200)),
        );
        await tester.pumpAndSettle();
        expect(checks, 1);
        expect(find.text('Update available: $tag.'), findsOneWidget);
        expect(find.byKey(const ValueKey('open-release')), findsOneWidget);
        expect(find.textContaining('same certificate'), findsOneWidget);
      },
    );

    testWidgets('desktop shows provenance without an update control', (
      tester,
    ) async {
      debugDefaultTargetPlatformOverride = TargetPlatform.linux;
      try {
        await tester.pumpWidget(
          MaterialApp(
            home: Scaffold(
              body: BuildAndUpdatesPanel(
                app: DistributedApp.kinaiya,
                identity: nightly(12),
              ),
            ),
          ),
        );
        expect(find.textContaining('nightly build 12'), findsOneWidget);
        expect(find.byKey(const ValueKey('check-updates')), findsNothing);
      } finally {
        debugDefaultTargetPlatformOverride = null;
      }
    });
  });

  group('transport problems', () {
    test('certificate and socket failures are distinguished', () {
      expect(
        classifyTransportError(
          const HandshakeException('CERTIFICATE_VERIFY_FAILED'),
        ),
        TransportProblem.certificate,
      );
      expect(
        classifyTransportError(const SocketException('Connection refused')),
        TransportProblem.offline,
      );
      expect(
        classifyTransportError(StateError('other')),
        TransportProblem.unknown,
      );
      expect(
        transportProblemMessage(TransportProblem.certificate),
        contains('never bypasses certificate checks'),
      );
    });
  });
}
