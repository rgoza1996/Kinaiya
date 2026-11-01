/// Redacted native speech admissions and positive cleanup proofs.
library;

import 'dart:convert';

Never _bad() => throw const FormatException('Invalid speech cleanup metadata');

Map<String, Object?> _object(Object? value, Set<String> keys) {
  if (value is! Map<String, Object?> ||
      value.length != keys.length ||
      !keys.containsAll(value.keys)) {
    _bad();
  }
  return value;
}

String _token(Object? value, int length) {
  if (value is! String ||
      value.length != length ||
      RegExp(r'[a-f0-9]+').matchAsPrefix(value)?.end != value.length) {
    _bad();
  }
  return value;
}

bool _flag(Object? value) {
  if (value is! bool) _bad();
  return value;
}

String _reason(Object? value) {
  if (value is! String ||
      value.isEmpty ||
      utf8.encode(value).length > 256 ||
      value.codeUnits.any((c) => c < 32 || c == 127)) {
    _bad();
  }
  return value;
}

class SttAdmission {
  SttAdmission.fromJson(Object? value) {
    final raw = _object(value, {
      'admission_id',
      'identity_sha256',
      'protocol',
      'worker_instance',
      'operation_id',
      'publication_state',
      'active',
      'reconcilable',
      'reason',
    });
    id = _token(raw['admission_id'], 32);
    identitySha256 = _token(raw['identity_sha256'], 64);
    final declaredProtocol = raw['protocol'];
    if (declaredProtocol != null &&
        declaredProtocol != 'native_stt_settlement_v1') {
      _bad();
    }
    protocol = declaredProtocol as String?;
    workerInstance = raw['worker_instance'] == null
        ? null
        : _token(raw['worker_instance'], 32);
    operationId = raw['operation_id'] == null
        ? null
        : _token(raw['operation_id'], 32);
    if ((protocol == null) != (workerInstance == null && operationId == null) ||
        (protocol != null && (workerInstance == null || operationId == null))) {
      _bad();
    }
    final publication = raw['publication_state'];
    if (!{
      'none',
      'not_admitted',
      'required',
      'legacy_unknown',
    }.contains(publication)) {
      _bad();
    }
    publicationState = publication as String;
    active = _flag(raw['active']);
    reconcilable = _flag(raw['reconcilable']);
    if ((protocol == null &&
            (publicationState != 'legacy_unknown' || reconcilable)) ||
        (protocol != null && publicationState == 'legacy_unknown') ||
        (active && reconcilable)) {
      _bad();
    }
    reason = _reason(raw['reason']);
  }
  late final String id, identitySha256, publicationState, reason;
  late final String? protocol, workerInstance, operationId;
  late final bool active, reconcilable;
  String get identity => '$id:$identitySha256';
  Map<String, Object?> request(String backend) => sttAdmissionRequest({
    'schema_version': '1.0',
    'expected_backend': backend,
    'admission_id': id,
    'identity_sha256': identitySha256,
  });
}

class SttAdmissionInventory {
  SttAdmissionInventory.fromJson(Object? value) {
    final raw = _object(value, {
      'schema_version',
      'role',
      'backend_id',
      'instance_id',
      'admissions',
      'limit',
    });
    if (raw['schema_version'] != '1.0' ||
        raw['role'] != 'stt' ||
        raw['limit'] is! int ||
        raw['limit'] != 64) {
      _bad();
    }
    backendId = _token(raw['backend_id'], 32);
    instanceId = _token(raw['instance_id'], 32);
    final entries = raw['admissions'];
    if (entries is! List || entries.length > 64) _bad();
    admissions = List.unmodifiable(entries.map(SttAdmission.fromJson));
    if (admissions.map((row) => row.id).toSet().length != admissions.length) {
      _bad();
    }
  }
  late final String backendId, instanceId;
  late final List<SttAdmission> admissions;
}

Map<String, Object?> sttAdmissionRequest(Object? value) {
  final raw = _object(value, {
    'schema_version',
    'expected_backend',
    'admission_id',
    'identity_sha256',
  });
  if (raw['schema_version'] != '1.0') _bad();
  return Map.unmodifiable({
    'schema_version': '1.0',
    'expected_backend': _token(raw['expected_backend'], 32),
    'admission_id': _token(raw['admission_id'], 32),
    'identity_sha256': _token(raw['identity_sha256'], 64),
  });
}

class SttAdmissionOutcome {
  SttAdmissionOutcome.fromJson(Object? value) {
    final raw = _object(value, {
      'schema_version',
      'role',
      'status',
      'backend_id',
      'admission_id',
      'identity_sha256',
      'worker_settled',
      'publication_settled',
      'retired',
      'reason',
    });
    if (raw['schema_version'] != '1.0' ||
        raw['role'] != 'stt' ||
        !{'settled', 'unknown'}.contains(raw['status'])) {
      _bad();
    }
    status = raw['status'] as String;
    backendId = _token(raw['backend_id'], 32);
    admissionId = _token(raw['admission_id'], 32);
    identitySha256 = _token(raw['identity_sha256'], 64);
    workerSettled = _flag(raw['worker_settled']);
    publicationSettled = _flag(raw['publication_settled']);
    retired = _flag(raw['retired']);
    if ((status == 'settled') != retired ||
        (retired && (!workerSettled || !publicationSettled))) {
      _bad();
    }
    reason = _reason(raw['reason']);
  }
  late final String status, backendId, admissionId, identitySha256, reason;
  late final bool workerSettled, publicationSettled, retired;
  bool matches(Map<String, Object?> body) =>
      body['expected_backend'] == backendId &&
      body['admission_id'] == admissionId &&
      body['identity_sha256'] == identitySha256;
}
