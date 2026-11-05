/// User-initiated client update check against official GitHub Releases.
///
/// This only *reports* availability. Installation is handed to the platform:
/// Android's package installer (which also enforces that an update carries the
/// same signing certificate) or the user's iOS sideloading tool. The app never
/// downloads, replaces or executes its own update. A failed, rate-limited or
/// malformed check is never reported as "up to date".
library;

import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

import 'build_identity.dart';

const releasesApi = 'https://api.github.com/repos/rgoza1996/Kinaiya/releases';
const releasesRoot = 'https://github.com/rgoza1996/Kinaiya/releases';
const _maxResponseBytes = 2 * 1024 * 1024;

final _stableTag = RegExp(
  r'^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$',
);
final _nightlyTag = RegExp(r'^nightly-([1-9][0-9]{0,18})-([0-9a-f]{12})$');

/// The only URLs the app will ask the platform to open for updates.
bool isOfficialReleaseUrl(String url) {
  const prefix = '$releasesRoot/tag/';
  if (!url.startsWith(prefix)) return false;
  final tag = url.substring(prefix.length);
  return _stableTag.hasMatch(tag) || _nightlyTag.hasMatch(tag);
}

enum MobilePlatform {
  android('android.apk'),
  ios('ios-sideload-unsigned.ipa');

  const MobilePlatform(this.assetSuffix);
  final String assetSuffix;
}

/// Asset name contract shared with the distribution workflow.
String mobileAssetName(
  DistributedApp app,
  MobilePlatform platform,
  String tag,
) => '${app.assetPrefix}-$tag-${platform.assetSuffix}';

enum ReleaseCheckState {
  /// Development builds have no channel to compare against.
  unsupported,

  current,
  available,

  /// The newest release has no artifact for this app/platform (for example,
  /// signing was not configured when it was published).
  noPlatformBuild,

  /// The channel has no published release (for example, no approved stable).
  absent,

  /// Network, HTTP or rate-limit failure: status unknown, not up to date.
  unavailable,

  /// Response failed validation: status unknown, not up to date.
  invalid,

  /// The published release is older than this build; never offered as an update.
  olderThanInstalled,
}

class ReleaseCheckResult {
  const ReleaseCheckResult(
    this.state,
    this.message, {
    this.tag,
    this.releaseUrl,
  });
  final ReleaseCheckState state;
  final String message;
  final String? tag;

  /// Set only for [ReleaseCheckState.available]; always an official tag page.
  final String? releaseUrl;
}

class _CheckFailure implements Exception {
  const _CheckFailure(this.state, this.message);
  final ReleaseCheckState state;
  final String message;
}

class ReleaseChecker {
  ReleaseChecker({
    required this.identity,
    required this.app,
    required this.platform,
    http.Client Function()? clientFactory,
  }) : _factory = clientFactory ?? http.Client.new;

  final BuildIdentity identity;
  final DistributedApp app;
  final MobilePlatform platform;
  final http.Client Function() _factory;

  Future<ReleaseCheckResult> check() async {
    if (!identity.isRelease) {
      return const ReleaseCheckResult(
        ReleaseCheckState.unsupported,
        'Development builds do not check for release updates.',
      );
    }
    try {
      final release = identity.channel == DistributionChannel.stable
          ? await _latestStable()
          : await _latestNightly();
      return _compare(release);
    } on _CheckFailure catch (failure) {
      return ReleaseCheckResult(failure.state, failure.message);
    }
  }

  Future<Object?> _get(String url) async {
    final client = _factory();
    try {
      final request = http.Request('GET', Uri.parse(url))
        ..followRedirects = false
        ..headers['Accept'] = 'application/vnd.github+json';
      final response = await client
          .send(request)
          .timeout(const Duration(seconds: 10));
      if (response.statusCode == 404) {
        throw const _CheckFailure(
          ReleaseCheckState.absent,
          'No release is published on this channel yet.',
        );
      }
      if (response.statusCode != 200) {
        // 403/429 are usually GitHub's anonymous rate limit.
        throw _CheckFailure(
          ReleaseCheckState.unavailable,
          response.statusCode == 403 || response.statusCode == 429
              ? 'GitHub limited update checks. Try again later.'
              : 'The release feed is unavailable. Try again later.',
        );
      }
      final bytes = <int>[];
      await for (final chunk in response.stream.timeout(
        const Duration(seconds: 15),
      )) {
        if (chunk.length > _maxResponseBytes - bytes.length) {
          throw const _CheckFailure(
            ReleaseCheckState.invalid,
            'The release feed response exceeded its size limit.',
          );
        }
        bytes.addAll(chunk);
      }
      return jsonDecode(utf8.decode(bytes));
    } on _CheckFailure {
      rethrow;
    } on FormatException {
      throw const _CheckFailure(
        ReleaseCheckState.invalid,
        'The release feed returned an unreadable response.',
      );
    } catch (_) {
      throw const _CheckFailure(
        ReleaseCheckState.unavailable,
        'Could not reach GitHub. Check the network and try again.',
      );
    } finally {
      client.close();
    }
  }

  Map<String, Object?> _validRelease(Object? entry, {required bool nightly}) {
    if (entry is! Map<String, Object?>) {
      throw const _CheckFailure(
        ReleaseCheckState.invalid,
        'Malformed release entry.',
      );
    }
    final tag = entry['tag_name'];
    if (entry['draft'] != false ||
        entry['prerelease'] != nightly ||
        tag is! String ||
        !(nightly ? _nightlyTag : _stableTag).hasMatch(tag) ||
        entry['html_url'] != '$releasesRoot/tag/$tag' ||
        entry['assets'] is! List) {
      throw const _CheckFailure(
        ReleaseCheckState.invalid,
        'The release feed failed identity checks.',
      );
    }
    return entry;
  }

  Future<Map<String, Object?>> _latestStable() async =>
      _validRelease(await _get('$releasesApi/latest'), nightly: false);

  /// Newest nightly on the first page (GitHub lists newest first). The bound
  /// is deliberate: the app never paginates an unbounded feed.
  Future<Map<String, Object?>> _latestNightly() async {
    final entries = await _get('$releasesApi?per_page=100&page=1');
    if (entries is! List || entries.length > 100) {
      throw const _CheckFailure(
        ReleaseCheckState.invalid,
        'Malformed release list.',
      );
    }
    Map<String, Object?>? newest;
    var newestBuild = 0;
    final seen = <int>{};
    for (final entry in entries) {
      if (entry is! Map<String, Object?>) {
        throw const _CheckFailure(
          ReleaseCheckState.invalid,
          'Malformed release entry.',
        );
      }
      final tag = entry['tag_name'];
      if (tag is! String || !tag.startsWith('nightly-')) continue;
      if (entry['draft'] != false || entry['prerelease'] != true) continue;
      final release = _validRelease(entry, nightly: true);
      final build = int.parse(_nightlyTag.firstMatch(tag)!.group(1)!);
      if (!seen.add(build)) {
        throw const _CheckFailure(
          ReleaseCheckState.invalid,
          'Ambiguous nightly build identity in the release feed.',
        );
      }
      if (build > newestBuild) {
        newestBuild = build;
        newest = release;
      }
    }
    if (newest == null) {
      throw const _CheckFailure(
        ReleaseCheckState.absent,
        'No nightly release is published yet.',
      );
    }
    return newest;
  }

  ReleaseCheckResult _compare(Map<String, Object?> release) {
    final tag = release['tag_name']! as String;
    final int order;
    if (identity.channel == DistributionChannel.stable) {
      final published = _stableTag.firstMatch(tag)!;
      final installed = _stableTag.firstMatch('v${identity.version}')!;
      order = _compareVersions(published, installed);
    } else {
      final build = int.parse(_nightlyTag.firstMatch(tag)!.group(1)!);
      order = build.compareTo(identity.buildNumber);
    }
    if (order == 0) {
      if (tag != identity.releaseTag) {
        return ReleaseCheckResult(
          ReleaseCheckState.invalid,
          'The published release does not match this build identity.',
          tag: tag,
        );
      }
      return ReleaseCheckResult(
        ReleaseCheckState.current,
        'This is the newest ${identity.channel.name} release.',
        tag: tag,
      );
    }
    if (order < 0) {
      return ReleaseCheckResult(
        ReleaseCheckState.olderThanInstalled,
        'The published release is older than this build; nothing to install.',
        tag: tag,
      );
    }
    final expected = mobileAssetName(app, platform, tag);
    final assets = release['assets']! as List;
    final present = assets.any(
      (asset) =>
          asset is Map<String, Object?> &&
          asset['name'] == expected &&
          asset['state'] == 'uploaded' &&
          asset['browser_download_url'] ==
              '$releasesRoot/download/$tag/$expected',
    );
    if (!present) {
      return ReleaseCheckResult(
        ReleaseCheckState.noPlatformBuild,
        'A newer release ($tag) exists, but it has no build for this app on this platform.',
        tag: tag,
      );
    }
    return ReleaseCheckResult(
      ReleaseCheckState.available,
      'Update available: $tag.',
      tag: tag,
      releaseUrl: '$releasesRoot/tag/$tag',
    );
  }
}

int _compareVersions(RegExpMatch a, RegExpMatch b) {
  for (var group = 1; group <= 3; group++) {
    final order = int.parse(a.group(group)!)
        .compareTo(int.parse(b.group(group)!));
    if (order != 0) return order;
  }
  return 0;
}
