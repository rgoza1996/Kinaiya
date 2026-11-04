/// Connection profiles and URL validation.
///
/// Credentials live in memory only: never persisted, never logged, never
/// placed in URLs. Insecure remote HTTP is rejected; loopback HTTP is allowed.
library;

import 'dart:convert';

import 'package:http/http.dart' as http;

import '../api/models.dart';
import 'service_root.dart';

const String defaultSessionUrl = 'http://127.0.0.1:8760';
const String defaultAgentUrl = 'http://127.0.0.1:8761';
const String defaultConverseUrl = 'http://127.0.0.1:8762';
const String localSignInPath = '/__kinaiya/local-signin';
const String localSignInHeader = 'X-Kinaiya-Local-Handoff';

const List<String> permittedWebOrigins = <String>[
  'http://localhost:8780',
  'http://127.0.0.1:8780',
];

/// Callback for exchanging a same-origin loopback handoff for an in-memory
/// owner session. Never persists tokens to browser storage or disk.
typedef LocalSignInExchange = Future<LocalSignInResult> Function();

/// Verified local-host sign-in payload returned by `POST /__kinaiya/local-signin`.
/// Held in RAM only; never broadens or overrides user inference disclosure.
class LocalSignInResult {
  LocalSignInResult({
    required this.ownerToken,
    this.sessionUrl = defaultSessionUrl,
    this.agentUrl = defaultAgentUrl,
    this.converseUrl = defaultConverseUrl,
    this.decisionsUrl,
    this.converseEnabled = false,
  });

  factory LocalSignInResult.fromJson(Map<String, Object?> json) {
    final rawToken = json['owner_token'] ?? json['ownerToken'];
    if (rawToken is! String || rawToken.trim().isEmpty) {
      throw const FormatException(
        'Local sign-in response is missing owner_token.',
      );
    }
    final rawSession =
        (json['session_url'] ?? json['sessionUrl'] ?? defaultSessionUrl);
    final rawAgent = (json['agent_url'] ?? json['agentUrl'] ?? defaultAgentUrl);
    final rawConverse =
        (json['converse_url'] ?? json['converseUrl'] ?? defaultConverseUrl);
    final rawDecisions = json['decisions_url'] ?? json['decisionsUrl'];
    if (rawSession is! String ||
        rawAgent is! String ||
        rawConverse is! String) {
      throw const FormatException(
        'Local sign-in response contains invalid service URLs.',
      );
    }
    return LocalSignInResult(
      ownerToken: rawToken.trim(),
      sessionUrl: rawSession,
      agentUrl: rawAgent,
      converseUrl: rawConverse,
      decisionsUrl: rawDecisions is String && rawDecisions.trim().isNotEmpty
          ? rawDecisions.trim()
          : null,
      converseEnabled:
          (json['converse_enabled'] ?? json['converseEnabled']) == true,
    );
  }

  final String ownerToken;
  final String sessionUrl;
  final String agentUrl;
  final String converseUrl;
  final String? decisionsUrl;
  final bool converseEnabled;

  /// Builds a loopback-validated [ConnectionProfile] while preserving the
  /// caller's [backendProfile] disclosure setting.
  ConnectionProfile toConnectionProfile({
    BackendProfile backendProfile = BackendProfile.clean,
  }) {
    final profile = ConnectionProfile(
      sessionUrl: sessionUrl,
      agentUrl: agentUrl,
      converseUrl: converseUrl,
      decisionsUrl: decisionsUrl,
      ownerToken: ownerToken,
      backendProfile: backendProfile,
    );
    final problem = profile.validateLoopbackOnly();
    if (problem != null) {
      throw FormatException(problem);
    }
    return profile;
  }
}

/// Performs the same-origin loopback `POST /__kinaiya/local-signin` exchange.
Future<LocalSignInResult> exchangeLocalSignIn({
  http.Client? httpClient,
  Uri? endpoint,
}) async {
  final target =
      endpoint ?? Uri.parse('${permittedWebOrigins.last}$localSignInPath');
  if (!ConnectionProfile._isLoopback(target.host)) {
    throw const FormatException(
      'Local sign-in exchange is restricted to loopback origins.',
    );
  }
  final client = httpClient ?? http.Client();
  final ownsClient = httpClient == null;
  try {
    final response = await client
        .post(
          target,
          headers: const <String, String>{
            localSignInHeader: '1',
            'Accept': 'application/json',
          },
        )
        .timeout(const Duration(seconds: 5));
    if (response.statusCode != 200) {
      throw FormatException(
        'Local sign-in exchange failed (${response.statusCode}).',
      );
    }
    final decoded = jsonDecode(response.body);
    if (decoded is! Map) {
      throw const FormatException('Local sign-in response must be a JSON map.');
    }
    return LocalSignInResult.fromJson(decoded.cast<String, Object?>());
  } finally {
    if (ownsClient) {
      client.close();
    }
  }
}

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
  ),

  privateCloudAuthorized(
    'Allow private input off-device',
    'Authorize private input to the configured cloud provider. Meta Contributor is excluded.',
  );

  const BackendProfile(this.label, this.note);
  final String label;
  final String note;

  ContentScope get contentScope => switch (this) {
    BackendProfile.clean => ContentScope.localOnly,
    BackendProfile.metaContributor ||
    BackendProfile.cloudDevelopment => ContentScope.publicSynthetic,
    BackendProfile.privateCloudAuthorized =>
      ContentScope.privateCloudAuthorized,
  };
}

/// Validated connection settings. The token is held in memory only.
class ConnectionProfile {
  ConnectionProfile({
    required String sessionUrl,
    required String agentUrl,
    required String converseUrl,
    String? decisionsUrl,
    required this.ownerToken,
    this.backendProfile = BackendProfile.clean,
  }) : sessionUrl = serviceRootOrOriginal(sessionUrl),
       agentUrl = serviceRootOrOriginal(agentUrl),
       converseUrl = serviceRootOrOriginal(converseUrl),
       decisionsUrl = decisionsUrl == null || decisionsUrl.trim().isEmpty
           ? null
           : serviceRootOrOriginal(decisionsUrl.trim());

  final String sessionUrl;
  final String agentUrl;
  final String converseUrl;

  /// Optional independent Decisions service. No endpoint is inferred.
  final String? decisionsUrl;
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
      if (decisionsUrl != null) MapEntry('Decisions URL', decisionsUrl!),
    ]) {
      final problem = validateServiceUrl(entry.value);
      if (problem != null) return '${entry.key}: $problem';
    }
    if (ownerToken.isEmpty) {
      return 'Owner access token is required. Export your owner token with '
          'kinaiya --data-dir <dir> owner-login (writes owner-login.txt) '
          'and enter it or use Load local access token before clicking Connect.';
    }
    return null;
  }

  /// Ensures all configured service endpoints are loopback-only.
  String? validateLoopbackOnly() {
    final problem = validate();
    if (problem != null) return problem;
    for (final entry in <MapEntry<String, String>>[
      MapEntry('Session URL', sessionUrl),
      MapEntry('Agent URL', agentUrl),
      MapEntry('Converse URL', converseUrl),
      if (decisionsUrl != null) MapEntry('Decisions URL', decisionsUrl!),
    ]) {
      final uri = Uri.parse(normalizeServiceRoot(entry.value));
      if (!_isLoopback(uri.host)) {
        return '${entry.key}: local sign-in requires a loopback endpoint';
      }
    }
    return null;
  }

  /// Rejects insecure remote HTTP; loopback HTTP and HTTPS are allowed.
  static String? validateServiceUrl(String raw) {
    final Uri uri;
    try {
      uri = Uri.parse(normalizeServiceRoot(raw));
    } on FormatException catch (error) {
      return error.message;
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
