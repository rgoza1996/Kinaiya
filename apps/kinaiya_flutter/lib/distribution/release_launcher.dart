/// Hands an official release page to the operating system's browser.
///
/// Native code re-validates the URL; neither side opens anything else. The
/// browser download and the platform installer remain under the user's control.
library;

import 'package:flutter/services.dart';

import 'release_check.dart';

class ReleasePageLauncher {
  const ReleasePageLauncher({
    MethodChannel channel = const MethodChannel('kinaiya/distribution'),
  }) : _channel = channel;

  final MethodChannel _channel;

  Future<bool> open(String url) async {
    if (!isOfficialReleaseUrl(url)) return false;
    try {
      return await _channel.invokeMethod<bool>('openReleasePage', url) ?? false;
    } on PlatformException {
      return false;
    } on MissingPluginException {
      return false;
    }
  }
}
