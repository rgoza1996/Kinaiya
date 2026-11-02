import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import 'build_identity.dart';
import 'release_check.dart';
import 'release_launcher.dart';

/// Native mobile platform of this process, or null (web/desktop/tests).
MobilePlatform? currentMobilePlatform() {
  if (kIsWeb) return null;
  return switch (defaultTargetPlatform) {
    TargetPlatform.android => MobilePlatform.android,
    TargetPlatform.iOS => MobilePlatform.ios,
    _ => null,
  };
}

/// Build provenance plus an explicit, user-initiated release check.
///
/// Nothing contacts GitHub until the user presses the button, and an update is
/// only ever *opened* as the official release page.
class BuildAndUpdatesPanel extends StatefulWidget {
  const BuildAndUpdatesPanel({
    super.key,
    required this.app,
    this.identity,
    this.platform,
    this.checkerFactory,
    this.launcher = const ReleasePageLauncher(),
  });

  final DistributedApp app;
  final BuildIdentity? identity;

  /// Overrides [currentMobilePlatform]; mainly for tests.
  final MobilePlatform? platform;
  final ReleaseChecker Function(BuildIdentity, DistributedApp, MobilePlatform)?
  checkerFactory;
  final ReleasePageLauncher launcher;

  @override
  State<BuildAndUpdatesPanel> createState() => _BuildAndUpdatesPanelState();
}

class _BuildAndUpdatesPanelState extends State<BuildAndUpdatesPanel> {
  late final BuildIdentity _identity =
      widget.identity ?? BuildIdentity.fromEnvironment();
  late final MobilePlatform? _platform =
      widget.platform ?? currentMobilePlatform();
  ReleaseCheckResult? _result;
  bool _checking = false;
  String? _openProblem;

  Future<void> _check(MobilePlatform platform) async {
    setState(() {
      _checking = true;
      _openProblem = null;
    });
    final checker =
        (widget.checkerFactory ??
        (identity, app, platform) => ReleaseChecker(
          identity: identity,
          app: app,
          platform: platform,
        ))(_identity, widget.app, platform);
    final result = await checker.check();
    if (!mounted) return;
    setState(() {
      _result = result;
      _checking = false;
    });
  }

  Future<void> _open(String url) async {
    final opened = await widget.launcher.open(url);
    if (!mounted || opened) return;
    setState(
      () => _openProblem = 'Could not open the release page. Visit $url',
    );
  }

  String get _installHint => switch (_platform) {
    MobilePlatform.android =>
      'Download the APK from the release page and confirm the installation in Android. '
          'Android only accepts an update signed with the same certificate; the app never replaces itself.',
    MobilePlatform.ios =>
      'Download the IPA from the release page and install it with your sideloading tool, '
          'which re-signs it with your Apple ID. The app never replaces itself.',
    null => '',
  };

  @override
  Widget build(BuildContext context) {
    final platform = _platform;
    final result = _result;
    return Column(
      key: const ValueKey('build-and-updates'),
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(_identity.label, key: const ValueKey('build-label')),
        if (_identity.isRelease) ...[
          SelectableText(
            'Source ${_identity.sourceSha}',
            key: const ValueKey('build-source'),
          ),
          Text('Channel: ${_identity.channel.name}'),
        ],
        if (platform != null && _identity.isRelease) ...[
          const SizedBox(height: 12),
          OutlinedButton(
            key: const ValueKey('check-updates'),
            onPressed: _checking ? null : () => unawaited(_check(platform)),
            child: Text(_checking ? 'Checking…' : 'Check for updates'),
          ),
          if (result != null) ...[
            const SizedBox(height: 8),
            Semantics(
              liveRegion: true,
              child: Text(result.message, key: const ValueKey('update-result')),
            ),
            if (result.state == ReleaseCheckState.available &&
                result.releaseUrl != null) ...[
              const SizedBox(height: 8),
              FilledButton(
                key: const ValueKey('open-release'),
                onPressed: () => unawaited(_open(result.releaseUrl!)),
                child: const Text('Open release page'),
              ),
              Text(_installHint),
            ],
          ],
          if (_openProblem != null) SelectableText(_openProblem!),
        ],
      ],
    );
  }
}
