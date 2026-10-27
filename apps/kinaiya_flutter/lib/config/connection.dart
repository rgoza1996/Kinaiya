/// Connection profiles and URL validation.
///
/// Credentials live in memory only: never persisted, never logged, never
/// placed in URLs. Insecure remote HTTP is rejected; loopback HTTP is allowed.
library;

const String defaultSessionUrl = 'http://127.0.0.1:8760';
const String defaultAgentUrl = 'http://127.0.0.1:8761';
const String defaultConverseUrl = 'http://127.0.0.1:8762';

const List<String> permittedWebOrigins = <String>[
  'http://localhost:8780',
  'http://127.0.0.1:8780',
];

/// Named backend profile shown in the setup dialog.
enum BackendProfile {
  /// Clean local backend; the operator must configure providers before use.
  clean(
    'Local inference only',
    'Requires provider setup before Converse or Agent work.',
  ),

  /// Meta Responses development profile for public/synthetic input only.
  metaContributor(
    'Allow Meta Contributor (development)',
    'Synthetic and public input only. Cloud disclosure applies.',
  ),

  /// Cloud development profile for public/synthetic input only.
  cloudDevelopment(
    'Allow cloud inference (development)',
    'Synthetic and public input only. Cloud disclosure applies.',
  );

  const BackendProfile(this.label, this.note);
  final String label;
  final String note;
}

/// Validated connection settings. The token is held in memory only.
class ConnectionProfile {
  ConnectionProfile({
    required this.sessionUrl,
    required this.agentUrl,
    required this.converseUrl,
    required this.ownerToken,
    this.backendProfile = BackendProfile.clean,
  });

  final String sessionUrl;
  final String agentUrl;
  final String converseUrl;
  final String ownerToken;
  final BackendProfile backendProfile;

  static ConnectionProfile defaults() => ConnectionProfile(
    sessionUrl: defaultSessionUrl,
    agentUrl: defaultAgentUrl,
    converseUrl: defaultConverseUrl,
    ownerToken: '',
  );

  /// Human-readable problem, or null when usable.
  String? validate() {
    for (final entry in <MapEntry<String, String>>[
      MapEntry('Session URL', sessionUrl),
      MapEntry('Agent URL', agentUrl),
      MapEntry('Converse URL', converseUrl),
    ]) {
      final problem = validateServiceUrl(entry.value);
      if (problem != null) return '${entry.key}: $problem';
    }
    if (ownerToken.isEmpty) return 'Owner Bearer [REDACTED] is required.';
    return null;
  }

  /// Rejects insecure remote HTTP; loopback HTTP and HTTPS are allowed.
  static String? validateServiceUrl(String raw) {
    final uri = Uri.tryParse(raw.trim());
    if (uri == null || !uri.hasScheme || !uri.hasAuthority) {
      return 'enter a full http(s) URL';
    }
    if (uri.scheme != 'http' && uri.scheme != 'https') {
      return 'only http(s) URLs are supported';
    }
    if (uri.scheme == 'http' && !_isLoopback(uri.host)) {
      return 'insecure remote HTTP is rejected; use https or loopback';
    }
    return null;
  }

  static bool _isLoopback(String host) {
    final h = host.toLowerCase();
    return h == 'localhost' || h == '127.0.0.1' || h == '::1' || h == '[::1]';
  }
}
