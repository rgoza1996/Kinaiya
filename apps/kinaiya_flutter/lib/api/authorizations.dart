/// Exact native one-use permit identities, separate from broad service grants.
library;

import 'dart:convert';

const authorizationStates = {
  'requested',
  'approved_unspent',
  'used',
  'expired',
  'revoked',
  'reissued',
  'retired',
};
const _max = 9007199254740990;
Never _bad() => throw const FormatException('Invalid authorization metadata');
Map<String, Object?> _map(Object? value, Set<String> keys) {
  if (value is! Map<String, Object?> ||
      value.length != keys.length ||
      !value.keys.every(keys.contains)) {
    _bad();
  }
  return value;
}

String _token(Object? value, String pattern) {
  if (value is! String ||
      RegExp(pattern).matchAsPrefix(value)?.end != value.length) {
    _bad();
  }
  return value;
}

String _id(Object? value, [int max = 128]) {
  if (value is! String ||
      value.isEmpty ||
      value.runes.length > max ||
      value.codeUnits.contains(0)) {
    _bad();
  }
  return value;
}

int _int(Object? value, [int min = 0]) {
  if (value is! int || value < min || value > _max) _bad();
  return value;
}

bool _bool(Object? value) {
  if (value is! bool) _bad();
  return value;
}

class AuthorizationIdentity {
  AuthorizationIdentity.fromJson(Object? value) {
    final raw = _map(value, {
      'approval_id',
      'task_id',
      'session_id',
      'thread_id',
      'epoch',
      'objective_revision',
      'tool',
      'action_digest',
      'arguments_sha256',
      'source_sha256',
    });
    body = Map.unmodifiable({
      'approval_id': _token(raw['approval_id'], r'apr_[a-f0-9]{16}'),
      'task_id': _id(raw['task_id']),
      'session_id': _id(raw['session_id']),
      'thread_id': _id(raw['thread_id']),
      'epoch': _int(raw['epoch'], 1),
      'objective_revision': _int(raw['objective_revision'], 1),
      'tool': _id(raw['tool'], 256),
      'action_digest': _token(raw['action_digest'], r'sha256:[a-f0-9]{64}'),
      'arguments_sha256': _token(raw['arguments_sha256'], r'[a-f0-9]{64}'),
      'source_sha256': _token(raw['source_sha256'], r'[a-f0-9]{64}'),
    });
  }
  late final Map<String, Object?> body;
  String get id => body['approval_id'] as String;
  String get taskId => body['task_id'] as String;
  String get tool => body['tool'] as String;
  bool matches(AuthorizationIdentity other) =>
      jsonEncode(body) == jsonEncode(other.body);
}

class AuthorizationRow {
  AuthorizationRow.fromJson(Object? value) {
    final raw = _map(value, {
      'identity',
      'state',
      'revision',
      'expires_at',
      'origin',
    });
    identity = AuthorizationIdentity.fromJson(raw['identity']);
    if (!authorizationStates.contains(raw['state']) ||
        raw['origin'] != 'owner_review') {
      _bad();
    }
    state = raw['state'] as String;
    revision = _int(raw['revision']);
    final expires = raw['expires_at'];
    if (expires is! num ||
        !expires.isFinite ||
        expires <= 0 ||
        expires > 1e12) {
      _bad();
    }
    expiresAt = expires.toDouble();
  }
  late final AuthorizationIdentity identity;
  late final String state;
  late final int revision;
  late final double expiresAt;
}

class AuthorizationsPage {
  AuthorizationsPage.fromJson(Object? value) {
    final raw = _map(value, {
      'schema_version',
      'broker_instance',
      'broad_grants',
      'authorizations',
      'next_after',
      'limits',
    });
    if (raw['schema_version'] != '1.0') _bad();
    instance = _token(raw['broker_instance'], r'[a-f0-9]{32}');
    final grants = _map(raw['broad_grants'], {
      'source',
      'write_file',
      'shell',
      'network',
      'yolo',
      'workspace_available',
      'readonly_roots',
      'isolation',
      'isolation_verified',
    });
    if (grants['source'] != 'service_instance' ||
        grants['isolation'] != 'required_per_operation' ||
        grants['isolation_verified'] != false ||
        !{'allow', 'ask', 'deny'}.contains(grants['write_file']) ||
        !{'allow', 'ask', 'deny'}.contains(grants['shell'])) {
      _bad();
    }
    broad = Map.unmodifiable({
      'write_file': grants['write_file'],
      'shell': grants['shell'],
      'network': _bool(grants['network']),
      'yolo': _bool(grants['yolo']),
      'workspace_available': _bool(grants['workspace_available']),
      'readonly_roots': _int(grants['readonly_roots']),
    });
    final limits = _map(raw['limits'], {
      'live',
      'closed',
      'revocations',
      'publications',
    });
    if (limits['live'] != 1024 ||
        limits['closed'] != 256 ||
        limits['revocations'] != 10000 ||
        limits['publications'] != 32) {
      _bad();
    }
    final rows = raw['authorizations'];
    if (rows is! List || rows.length > 50) _bad();
    authorizations = List.unmodifiable(rows.map(AuthorizationRow.fromJson));
    for (var i = 1; i < authorizations.length; i++) {
      if (authorizations[i - 1].identity.id.compareTo(
            authorizations[i].identity.id,
          ) >=
          0) {
        _bad();
      }
    }
    nextAfter = raw['next_after'] == null
        ? null
        : _token(raw['next_after'], r'apr_[a-f0-9]{16}');
    if (nextAfter != null &&
        (authorizations.isEmpty ||
            authorizations.last.identity.id != nextAfter)) {
      _bad();
    }
  }
  late final String instance;
  late final Map<String, Object?> broad;
  late final List<AuthorizationRow> authorizations;
  late final String? nextAfter;
}

Map<String, Object?> authorizationRevoke(Object? value) {
  final raw = _map(value, {
    'schema_version',
    'broker_instance',
    'idempotency_key',
    'identity',
    'expected_revision',
    'expected_state',
  });
  if (raw['schema_version'] != '1.0' ||
      raw['expected_state'] != 'approved_unspent') {
    _bad();
  }
  return Map.unmodifiable({
    'schema_version': '1.0',
    'broker_instance': _token(raw['broker_instance'], r'[a-f0-9]{32}'),
    'idempotency_key': _token(
      raw['idempotency_key'],
      r'[A-Za-z0-9:._-]{1,128}',
    ),
    'identity': AuthorizationIdentity.fromJson(raw['identity']).body,
    'expected_revision': _int(raw['expected_revision']),
    'expected_state': 'approved_unspent',
  });
}

class AuthorizationReceipt {
  AuthorizationReceipt.fromJson(Object? value) {
    final raw = _map(value, {
      'schema_version',
      'status',
      'request',
      'observed_state',
      'revision',
      'journal_status',
    });
    if (raw['schema_version'] != '1.0' ||
        !{'revoked', 'not_revoked'}.contains(raw['status']) ||
        !authorizationStates.contains(raw['observed_state']) ||
        !{
          'not_required',
          'pending',
          'confirmed',
          'unconfirmed',
        }.contains(raw['journal_status'])) {
      _bad();
    }
    request = authorizationRevoke(raw['request']);
    status = raw['status'] as String;
    observedState = raw['observed_state'] as String;
    revision = _int(raw['revision']);
    journalStatus = raw['journal_status'] as String;
    if (status == 'revoked'
        ? observedState != 'revoked' ||
              revision != (request['expected_revision'] as int) + 1 ||
              journalStatus == 'not_required'
        : journalStatus != 'not_required') {
      _bad();
    }
  }
  late final Map<String, Object?> request;
  late final String status, observedState, journalStatus;
  late final int revision;
  bool matches(Map<String, Object?> original) =>
      jsonEncode(request) == jsonEncode(authorizationRevoke(original));
  bool get settled => status == 'not_revoked' || journalStatus == 'confirmed';
}

class AuthorizationLookup {
  AuthorizationLookup.fromJson(Object? value) {
    final raw = _map(value, {
      'schema_version',
      'status',
      'broker_instance',
      'receipt',
    });
    if (raw['schema_version'] != '1.0' ||
        !{'observed', 'unobserved'}.contains(raw['status'])) {
      _bad();
    }
    instance = _token(raw['broker_instance'], r'[a-f0-9]{32}');
    receipt = raw['receipt'] == null
        ? null
        : AuthorizationReceipt.fromJson(raw['receipt']);
    if ((raw['status'] == 'observed') != (receipt != null)) _bad();
  }
  late final String instance;
  late final AuthorizationReceipt? receipt;
}
