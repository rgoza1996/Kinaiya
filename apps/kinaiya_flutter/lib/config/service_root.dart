/// Canonical syntax shared by HTTP clients. This does not grant transport trust:
/// owner setup separately requires HTTPS remotely or loopback HTTP.
library;

String normalizeServiceRoot(String raw) {
  final value = raw.trim();
  const invalid = FormatException('enter a full http(s) service URL');
  try {
    if (RegExp(r'[\s\\\x00-\x1f\x7f]').hasMatch(value)) throw invalid;
    final uri = Uri.parse(value);
    if (!{'http', 'https'}.contains(uri.scheme) ||
        !uri.hasAuthority ||
        uri.host.isEmpty ||
        uri.port < 1 ||
        uri.port > 65535) {
      throw invalid;
    }
    final authorityStart = value.indexOf('://') + 3;
    final pathStart = value.indexOf('/', authorityStart);
    final rawAuthority = value.substring(
      authorityStart,
      pathStart < 0 ? value.length : pathStart,
    );
    if (rawAuthority.contains('@') || uri.hasQuery || uri.hasFragment) {
      throw const FormatException(
        'service URLs cannot contain credentials, queries or fragments',
      );
    }
    // Inspect the original path before Uri can resolve dot segments. Preserve
    // valid proxy prefixes, including escapes, but never resolve an ambiguous
    // path into a different endpoint or strip arbitrary trailing slashes.
    final path = pathStart < 0 ? '' : value.substring(pathStart);
    if (path.contains('//')) {
      throw const FormatException('invalid service path');
    }
    for (final segment in path.split('/')) {
      final decoded = Uri.decodeComponent(segment);
      if (decoded == '.' ||
          decoded == '..' ||
          decoded.contains('/') ||
          decoded.contains('\\') ||
          decoded.contains('%') ||
          decoded.codeUnits.any((c) => c <= 32 || c == 127)) {
        throw const FormatException('invalid service path');
      }
    }
    return value.endsWith('/') ? value.substring(0, value.length - 1) : value;
  } on FormatException catch (error) {
    // Do not reflect the input, which may contain a credential or query secret.
    if (error.message == 'invalid service path' ||
        error.message ==
            'service URLs cannot contain credentials, queries or fragments') {
      throw FormatException(error.message);
    }
    throw invalid;
  }
}

/// Keep invalid form values available for explicit validation/error feedback.
String serviceRootOrOriginal(String raw) {
  try {
    return normalizeServiceRoot(raw);
  } on FormatException {
    return raw;
  }
}
