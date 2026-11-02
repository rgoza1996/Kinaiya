/// Immutable build provenance compiled into distributed clients (#262/#263/#264).
///
/// The release workflow passes `KINAIYA_CHANNEL`, `KINAIYA_VERSION`,
/// `KINAIYA_BUILD_NUMBER` and `KINAIYA_SOURCE_SHA` as `--dart-define` values.
/// Android derives its application ID from the same `KINAIYA_CHANNEL` define,
/// so the Dart-visible channel and the installed identity cannot disagree.
/// Anything missing or malformed is a development build: it never claims a
/// release channel and never offers release updates.
library;

enum DistributionChannel { development, nightly, stable }

/// Which distributed Flutter entry point this binary was compiled from.
enum DistributedApp {
  /// `lib/main.dart`: the owner client (Converse, Agent, settings).
  kinaiya('kinaiya'),

  /// `lib/companion_main.dart`: the scoped, pairing-only companion.
  companion('kinaiya-companion');

  const DistributedApp(this.assetPrefix);

  /// Release asset name prefix shared with `.github/workflows/distribution.yml`.
  final String assetPrefix;
}

final _sha = RegExp(r'^[0-9a-f]{40}$');
final _stableVersion = RegExp(
  r'^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$',
);

class BuildIdentity {
  const BuildIdentity._({
    required this.channel,
    this.version = '',
    this.buildNumber = 0,
    this.sourceSha = '',
  });

  const BuildIdentity.development()
    : this._(channel: DistributionChannel.development);

  /// Validates explicit values; invalid combinations become a development build.
  factory BuildIdentity.parse({
    required String channel,
    required String version,
    required String buildNumber,
    required String sourceSha,
  }) {
    final selected = switch (channel) {
      'nightly' => DistributionChannel.nightly,
      'stable' => DistributionChannel.stable,
      _ => null,
    };
    final build = int.tryParse(buildNumber);
    if (selected == null ||
        build == null ||
        build < 1 ||
        build > 2100000000 ||
        buildNumber != '$build' ||
        !_sha.hasMatch(sourceSha) ||
        version.isEmpty ||
        version.length > 128) {
      return const BuildIdentity.development();
    }
    final base = version.split('-').first;
    if (!_stableVersion.hasMatch(base) ||
        (selected == DistributionChannel.stable && version != base) ||
        (selected == DistributionChannel.nightly &&
            version != '$base-nightly.$build+${sourceSha.substring(0, 12)}')) {
      return const BuildIdentity.development();
    }
    return BuildIdentity._(
      channel: selected,
      version: version,
      buildNumber: build,
      sourceSha: sourceSha,
    );
  }

  factory BuildIdentity.fromEnvironment() => BuildIdentity.parse(
    channel: const String.fromEnvironment('KINAIYA_CHANNEL'),
    version: const String.fromEnvironment('KINAIYA_VERSION'),
    buildNumber: const String.fromEnvironment('KINAIYA_BUILD_NUMBER'),
    sourceSha: const String.fromEnvironment('KINAIYA_SOURCE_SHA'),
  );

  final DistributionChannel channel;
  final String version;
  final int buildNumber;
  final String sourceSha;

  bool get isRelease => channel != DistributionChannel.development;

  /// The immutable GitHub release tag this binary was published under.
  String? get releaseTag => switch (channel) {
    DistributionChannel.stable => 'v$version',
    DistributionChannel.nightly =>
      'nightly-$buildNumber-${sourceSha.substring(0, 12)}',
    DistributionChannel.development => null,
  };

  String get label => switch (channel) {
    DistributionChannel.development => 'Development build',
    DistributionChannel.stable =>
      'Kinaiya $version (stable, build $buildNumber)',
    DistributionChannel.nightly =>
      'Kinaiya $version (nightly build $buildNumber, experimental)',
  };
}
