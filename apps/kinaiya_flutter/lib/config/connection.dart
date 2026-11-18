/// Connection profiles and URL validation.
///
/// Owner and session bearers stay in memory. A paired loopback browser can
/// remember a separate revocable device proof for session renewal.
library;

import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

import '../api/models.dart';
import 'credential_picker.dart';
import 'service_root.dart';

const String defaultSessionUrl = 'http://127.0.0.1:8760';
const String defaultAgentUrl = 'http://127.0.0.1:8761';
const String defaultConverseUrl = 'http://127.0.0.1:8762';
const String localSignInPath = '/__kinaiya/local-signin';
const String localRefreshPath = '/__kinaiya/refresh-session';
const String localRecoveryChallengePath = '/__kinaiya/recovery/challenge';
const String localRecoverAccessPath = '/__kinaiya/recover-access';
const String localRotateOwnerPath = '/__kinaiya/rotate-owner';
const String hostCredentialStorageStatusPath = '/__kinaiya/storage-status';
const String localSignInHeader = 'X-Kinaiya-Local-Handoff';

const List<String> permittedWebOrigins = <String>[
  'http://localhost:8780',
  'http://127.0.0.1:8780',
];

/// Structured actionable failure from the local host broker.
class LocalSignInException implements Exception {
  const LocalSignInException(this.message, {this.code, this.statusCode});

  final String message;
  final String? code;
  final int? statusCode;

  bool get proofRejected => const {
    'LocalSignInDenied',
    'LocalSignInExpired',
    'LocalSignInStale',
  }.contains(code);

  @override
  String toString() => message;
}

/// Callback for exchanging a same-origin loopback handoff for an in-memory
/// owner session. The service credential stays in memory.
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
    this.ownerEpoch,
    this.newOwnerToken,
  });

  factory LocalSignInResult.fromJson(Map<String, Object?> json) {
    final rawToken =
        json['owner_token'] ??
        json['ownerToken'] ??
        json['session_token'] ??
        json['sessionToken'];
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
    final rawEpoch = json['owner_epoch'] ?? json['ownerEpoch'];
    final rawNewOwner = json['new_owner_token'] ?? json['newOwnerToken'];
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
      ownerEpoch: rawEpoch is int ? rawEpoch : null,
      newOwnerToken: rawNewOwner is String && rawNewOwner.trim().isNotEmpty
          ? rawNewOwner.trim()
          : null,
    );
  }

  final String ownerToken;
  final String sessionUrl;
  final String agentUrl;
  final String converseUrl;
  final String? decisionsUrl;
  final bool converseEnabled;
  final int? ownerEpoch;
  final String? newOwnerToken;

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

bool isLocalBrokerPage(Uri page) =>
    (page.scheme == 'http' || page.scheme == 'https') &&
    page.userInfo.isEmpty &&
    ConnectionProfile._isLoopback(page.host);

Uri _brokerEndpoint(String path, Uri? endpoint) {
  final page = kIsWeb ? Uri.base : Uri.parse(permittedWebOrigins.last);
  final target = endpoint ?? page.resolve(path);
  if (!isLocalBrokerPage(target) ||
      (kIsWeb && (!isLocalBrokerPage(page) || target.origin != page.origin))) {
    throw const LocalSignInException(
      'Open Kinaiya on its local host to use local sign-in.',
    );
  }
  return target;
}

Future<Map<String, Object?>> _postLocalBroker(
  String path, {
  Uri? endpoint,
  http.Client? httpClient,
  Map<String, Object?>? body,
}) async {
  final target = _brokerEndpoint(path, endpoint);
  final client = httpClient ?? http.Client();
  try {
    final request = http.Request('POST', target)
      ..followRedirects = false
      ..headers.addAll({localSignInHeader: '1', 'Accept': 'application/json'});
    if (body != null) {
      request.headers['Content-Type'] = 'application/json';
      request.body = jsonEncode(body);
    }
    final response = await client
        .send(request)
        .then(http.Response.fromStream)
        .timeout(const Duration(seconds: 5));
    if (response.statusCode != 200) {
      Map? error;
      try {
        final parsed = jsonDecode(response.body);
        if (parsed is Map) error = parsed;
      } on FormatException {
        // A non-Kinaiya host can return a non-JSON error.
      }
      throw LocalSignInException(
        error?['detail']?.toString() ??
            'Local host request failed (${response.statusCode}).',
        code: error?['code']?.toString(),
        statusCode: response.statusCode,
      );
    }
    final json = jsonDecode(response.body);
    if (json is! Map<String, Object?>) {
      throw const FormatException('Invalid local host response.');
    }
    return json;
  } finally {
    if (httpClient == null) client.close();
  }
}

final _deviceProofFormat = RegExp(r'^kny_refr_[A-Za-z0-9_-]{32,128}$');

Future<LocalSignInResult> _pairedSession(Map<String, Object?> json) async {
  final session = LocalSignInResult.fromJson(json);
  session
      .toConnectionProfile(); // Validate the returned endpoints before pairing.
  final proof = json['refresh_token'];
  if (proof is String && _deviceProofFormat.hasMatch(proof)) {
    await rememberDeviceProof(proof);
  }
  return session;
}

/// Reuse the paired device. A rejected proof can be replaced by a launch handoff.
Future<LocalSignInResult> exchangeLocalSignIn({
  http.Client? httpClient,
  Uri? endpoint,
  Future<String?> Function() readProof = readDeviceProof,
}) async {
  final target = _brokerEndpoint(localSignInPath, endpoint);
  final proof = await readProof();
  if (proof != null && _deviceProofFormat.hasMatch(proof)) {
    try {
      final json = await _postLocalBroker(
        localRefreshPath,
        endpoint: target.resolve(localRefreshPath),
        httpClient: httpClient,
        body: {'refresh_token': proof},
      );
      return LocalSignInResult.fromJson(json);
    } on LocalSignInException catch (error) {
      if (!error.proofRejected) rethrow;
    }
  }
  return _pairedSession(
    await _postLocalBroker(
      localSignInPath,
      endpoint: target,
      httpClient: httpClient,
    ),
  );
}

/// Requests a single-use recovery file on the local host.
Future<Map<String, Object?>> requestRecoveryChallenge({
  http.Client? httpClient,
  Uri? endpoint,
}) => _postLocalBroker(
  localRecoveryChallengePath,
  endpoint: endpoint,
  httpClient: httpClient,
);

Future<LocalSignInResult> recoverLocalAccess({
  required String authorizationProof,
  http.Client? httpClient,
  Uri? endpoint,
}) async => _pairedSession(
  await _postLocalBroker(
    localRecoverAccessPath,
    endpoint: endpoint,
    httpClient: httpClient,
    body: {'authorization_proof': authorizationProof},
  ),
);

Future<LocalSignInResult> rotateOwnerCredential({
  required String authorizationProof,
  bool returnExportedToken = true,
  http.Client? httpClient,
  Uri? endpoint,
}) async => _pairedSession(
  await _postLocalBroker(
    localRotateOwnerPath,
    endpoint: endpoint,
    httpClient: httpClient,
    body: {
      'authorization_proof': authorizationProof,
      'confirm': true,
      'return_exported_token': returnExportedToken,
    },
  ),
);

/// Checks private credential storage health on the local host.
Future<Map<String, Object?>> checkStorageStatus({
  http.Client? httpClient,
  Uri? endpoint,
}) async {
  final target = _brokerEndpoint(hostCredentialStorageStatusPath, endpoint);
  final client = httpClient ?? http.Client();
  final ownsClient = httpClient == null;
  try {
    final response = await client
        .get(
          target,
          headers: const <String, String>{'Accept': 'application/json'},
        )
        .timeout(const Duration(seconds: 5));
    final decoded = jsonDecode(response.body);
    if (decoded is! Map) {
      throw const FormatException(
        'Storage status response must be a JSON map.',
      );
    }
    return decoded.cast<String, Object?>();
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
