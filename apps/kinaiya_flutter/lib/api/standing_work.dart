/// Codex-authored strict standing owner metadata; no scheduling or readiness.
library;

import 'dart:convert';
import 'package:crypto/crypto.dart';

const _maxInteger = 9007199254740991;
const _uuid = r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}';
const _invalid = FormatException('Invalid standing work proof');

void _unicode(String value) {
  for (var i = 0; i < value.length; i++) {
    final unit = value.codeUnitAt(i);
    if (unit >= 0xd800 && unit <= 0xdbff) {
      if (++i >= value.length ||
          value.codeUnitAt(i) < 0xdc00 ||
          value.codeUnitAt(i) > 0xdfff) {
        throw _invalid;
      }
    } else if (unit >= 0xdc00 && unit <= 0xdfff) {
      throw _invalid;
    }
  }
}

void _tree(Object? value, [int depth = 1, List<int>? count]) {
  count ??= [0];
  if (depth > 12 || ++count[0] > 2048) throw _invalid;
  if (value is Map) {
    for (final entry in value.entries) {
      if (entry.key is! String) throw _invalid;
      _tree(entry.key, depth + 1, count);
      _tree(entry.value, depth + 1, count);
    }
  } else if (value is List) {
    for (final item in value) {
      _tree(item, depth + 1, count);
    }
  } else if (value is String) {
    _unicode(value);
  } else if (value is int) {
    if (value.abs() > _maxInteger) throw _invalid;
  } else if (value != null && value is! bool) {
    throw _invalid;
  }
}

Map<String, Object?> _closed(Object? value, Set<String> fields) {
  _tree(value);
  if (value is! Map || value.length != fields.length) throw _invalid;
  final result = <String, Object?>{};
  for (final entry in value.entries) {
    final key = entry.key;
    if (key is! String || !fields.contains(key)) throw _invalid;
    result[key] = entry.value;
  }
  return result;
}

int _compare(String left, String right) {
  final a = left.runes.iterator, b = right.runes.iterator;
  while (true) {
    final moreA = a.moveNext(), moreB = b.moveNext();
    if (!moreA || !moreB) {
      return moreA == moreB
          ? 0
          : moreA
          ? 1
          : -1;
    }
    if (a.current != b.current) return a.current.compareTo(b.current);
  }
}

Object? _freeze(Object? value) {
  if (value is Map) {
    final keys = value.keys.cast<String>().toList()..sort(_compare);
    return Map<String, Object?>.unmodifiable({
      for (final key in keys) key: _freeze(value[key]),
    });
  }
  if (value is List) return List<Object?>.unmodifiable(value.map(_freeze));
  return value;
}

List<int> _bytes(Object? value) => utf8.encode(jsonEncode(_freeze(value)));

Map<String, Object?> _bounded(Map<String, Object?> value, int maximum) {
  _tree(value);
  if (_bytes(value).length > maximum) throw _invalid;
  return _freeze(value) as Map<String, Object?>;
}

String _match(Object? value, String pattern) {
  if (value is! String ||
      RegExp(pattern).matchAsPrefix(value)?.end != value.length) {
    throw _invalid;
  }
  return value;
}

String _text(Object? value, int maximum, {bool safeId = false}) {
  if (value is! String ||
      value.runes.isEmpty ||
      value.runes.length > maximum ||
      (safeId && value.contains('\u0000'))) {
    throw _invalid;
  }
  return value;
}

String _backend(Object? value) => _match(value, r'[a-f0-9]{32}');
String _instance(Object? value) => _match(value, _uuid);
String _digest(Object? value) => _match(value, r'[a-f0-9]{64}');

int _integer(Object? value, int minimum, [int maximum = _maxInteger]) {
  if (value is! int || value < minimum || value > maximum) throw _invalid;
  return value;
}

bool _flag(Object? value) {
  if (value is! bool) throw _invalid;
  return value;
}

String _literal(Object? value, Set<String> choices) {
  if (value is! String || !choices.contains(value)) throw _invalid;
  return value;
}

void _version(Map<String, Object?> raw) {
  if (raw['schema_version'] != '1.0') throw _invalid;
}

bool _leap(int year) => year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);

String _date(Object? value) {
  final text = _match(value, r'[0-9]{4}-[0-9]{2}-[0-9]{2}');
  final year = int.parse(text.substring(0, 4));
  final month = int.parse(text.substring(5, 7));
  final day = int.parse(text.substring(8, 10));
  final days = [
    31,
    _leap(year) ? 29 : 28,
    31,
    30,
    31,
    30,
    31,
    31,
    30,
    31,
    30,
    31,
  ];
  if (year < 1 || month < 1 || month > 12 || day < 1 || day > days[month - 1]) {
    throw _invalid;
  }
  return text;
}

String _time(Object? value) =>
    _match(value, r'(?:[01][0-9]|2[0-3]):[0-5][0-9]');

DateTime _instant(Object? value) {
  final text = _match(
    value,
    r'[0-9]{4}-[0-9]{2}-[0-9]{2}T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]Z',
  );
  _date(text.substring(0, 10));
  return DateTime.utc(
    int.parse(text.substring(0, 4)),
    int.parse(text.substring(5, 7)),
    int.parse(text.substring(8, 10)),
    int.parse(text.substring(11, 13)),
    int.parse(text.substring(14, 16)),
    int.parse(text.substring(17, 19)),
  );
}

String _zone(Object? value) {
  // IANA keys stay opaque. Installation, civil-date span and fold/gap authority
  // belong to native ZoneInfo, never to browser-local timezone conversion.
  if (value is! String ||
      value.isEmpty ||
      value.contains('\u0000') ||
      value.startsWith('/') ||
      value
          .split('/')
          .any((part) => part.isEmpty || part == '.' || part == '..')) {
    throw _invalid;
  }
  return value;
}

Map<String, Object?> _quiet(Object? value) {
  final raw = _closed(value, {
    'timezone',
    'start_local_time',
    'end_local_time',
  });
  _zone(raw['timezone']);
  if (_time(raw['start_local_time']) == _time(raw['end_local_time'])) {
    throw _invalid;
  }
  return raw;
}

Map<String, Object?> _authorization(Object? value) {
  final raw = _closed(value, {
    'label',
    'objective',
    'content_scope',
    'enabled',
    'task_session',
    'workspace_pin',
    'schedule',
    'delivery',
    'overlap_policy',
    'maximum_runs',
    'inference_authorization',
  });
  _text(raw['label'], 200);
  _text(raw['objective'], 16000);
  _literal(raw['content_scope'], {
    'local_only',
    'public_synthetic',
    'private_cloud_authorized',
  });
  _flag(raw['enabled']);
  final task = _closed(raw['task_session'], {
    'session_id',
    'thread_id',
    'epoch',
  });
  _text(task['session_id'], 128, safeId: true);
  _text(task['thread_id'], 128, safeId: true);
  _integer(task['epoch'], 1);
  final pin = _closed(raw['workspace_pin'], {
    'schema_version',
    'workspace_id',
    'root_identity',
    'settings_revision',
    'source',
    'shell_backend',
    'mode',
  });
  _version(pin);
  _digest(pin['workspace_id']);
  _digest(pin['root_identity']);
  _integer(pin['settings_revision'], 0, _maxInteger - 1);
  _literal(pin['source'], {'host', 'owner_saved'});
  if (pin['shell_backend'] != 'bubblewrap' || pin['mode'] != 'direct') {
    throw _invalid;
  }
  final schedule = _closed(raw['schedule'], {
    'kind',
    'timezone',
    'local_time',
    'fold_policy',
    'gap_policy',
    'grace_seconds',
    'missed_run_policy',
    'start_local_date',
    'valid_until_utc',
  });
  _zone(schedule['timezone']);
  _time(schedule['local_time']);
  _integer(schedule['grace_seconds'], 1, 86400);
  _date(schedule['start_local_date']);
  _instant(schedule['valid_until_utc']);
  if (schedule['kind'] != 'daily_local' ||
      schedule['fold_policy'] != 'earlier' ||
      schedule['gap_policy'] != 'skip' ||
      schedule['missed_run_policy'] != 'latest_within_grace') {
    throw _invalid;
  }
  final delivery = _closed(raw['delivery'], {
    'kind',
    'attention',
    'preview',
    'quiet_hours',
  });
  if (delivery['kind'] != 'in_app' || delivery['preview'] != 'status_only') {
    throw _invalid;
  }
  _literal(delivery['attention'], {'routine', 'important'});
  if (delivery['quiet_hours'] != null) _quiet(delivery['quiet_hours']);
  _integer(raw['maximum_runs'], 1, 32);
  if (raw['overlap_policy'] != 'one_unresolved_per_authorization' ||
      raw['inference_authorization'] != 'configured_agent_profile') {
    throw _invalid;
  }
  return _freeze(raw) as Map<String, Object?>;
}

String _authorizationDigest(StandingChange request, int revision) =>
    sha256.convert([
      ...utf8.encode('kinaiya.standing.authorization.v1\u0000'),
      ..._bytes({
        'responsibility_id': request.responsibilityId,
        'revision': revision,
        'request': request.body,
      }),
    ]).toString();

String _recordDigest(
  StandingChangeReceipt receipt,
  String activation,
  String? clock,
) => sha256.convert([
  ...utf8.encode('kinaiya.standing.record.v1\u0000'),
  ..._bytes({
    'receipt': receipt.json,
    'activation': activation,
    'authorized_from_utc': clock,
  }),
]).toString();

final class StandingChange {
  StandingChange.fromJson(Object? value) {
    final raw = _closed(value, {
      'schema_version',
      'idempotency_key',
      'expected_backend',
      'expected_instance',
      'expected_revision',
      'responsibility_id',
      'authorization',
    });
    _version(raw);
    key = _match(raw['idempotency_key'], r'[A-Za-z0-9:._-]{1,128}');
    backendId = _backend(raw['expected_backend']);
    instanceId = _instance(raw['expected_instance']);
    revision = _integer(raw['expected_revision'], 0, _maxInteger - 1);
    responsibilityId = _instance(raw['responsibility_id']);
    authorization = _authorization(raw['authorization']);
    body = _bounded({...raw, 'authorization': authorization}, 114688);
    if (canonicalBytes.length * 2 + 32768 > 262144) throw _invalid;
  }
  String get schemaVersion => '1.0';
  late final String key, backendId, instanceId, responsibilityId;
  late final int revision;
  late final Map<String, Object?> body, authorization;
  Map<String, Object?> get json => body;
  List<int> get canonicalBytes => _bytes(body);
  bool matches(StandingChange other) =>
      jsonEncode(body) == jsonEncode(other.body);
}

final class StandingChangeReceipt {
  StandingChangeReceipt.fromJson(Object? value, {StandingChange? original}) {
    final raw = _closed(value, {
      'schema_version',
      'status',
      'backend_id',
      'service_instance',
      'revision',
      'request',
      'authorization_digest',
    });
    _version(raw);
    if (raw['status'] != 'responsibility_saved') throw _invalid;
    request = StandingChange.fromJson(raw['request']);
    if (original != null &&
        !request.matches(StandingChange.fromJson(original.body))) {
      throw _invalid;
    }
    backendId = _backend(raw['backend_id']);
    serviceInstance = _instance(raw['service_instance']);
    revision = _integer(raw['revision'], 1);
    authorizationDigest = _digest(raw['authorization_digest']);
    if (backendId != request.backendId ||
        serviceInstance != request.instanceId ||
        revision != request.revision + 1 ||
        authorizationDigest != _authorizationDigest(request, revision)) {
      throw _invalid;
    }
    json = _bounded({...raw, 'request': request.body}, 131072);
    if (_bytes({'request': request.body, 'receipt': json}).length > 262144) {
      throw _invalid;
    }
  }
  String get schemaVersion => '1.0';
  String get status => 'responsibility_saved';
  late final String backendId, serviceInstance, authorizationDigest;
  late final int revision;
  late final StandingChange request;
  late final Map<String, Object?> json;
}

final class StandingCurrent {
  StandingCurrent.fromJson(Object? value) {
    final raw = _closed(value, {
      'schema_version',
      'backend_id',
      'instance_id',
      'responsibility_id',
      'revision',
      'control_writable',
    });
    _version(raw);
    backendId = _backend(raw['backend_id']);
    instanceId = _instance(raw['instance_id']);
    responsibilityId = _instance(raw['responsibility_id']);
    revision = _integer(raw['revision'], 0);
    controlWritable = _flag(raw['control_writable']);
    json = _bounded(raw, 4096);
  }
  String get schemaVersion => '1.0';
  late final String backendId, instanceId, responsibilityId;
  late final int revision;
  late final bool controlWritable;
  late final Map<String, Object?> json;
}

final class StandingRecord {
  StandingRecord.fromJson(Object? value) {
    final raw = _closed(value, {
      'schema_version',
      'receipt',
      'activation',
      'authorized_from_utc',
      'record_digest',
    });
    _version(raw);
    receipt = StandingChangeReceipt.fromJson(raw['receipt']);
    activation = _literal(raw['activation'], {'pending', 'armed'});
    final clock = raw['authorized_from_utc'];
    if ((activation == 'pending') != (clock == null)) throw _invalid;
    authorizedFromUtc = clock == null
        ? null
        : clock is String
        ? clock
        : throw _invalid;
    if (activation == 'armed') {
      final from = _instant(authorizedFromUtc);
      final schedule =
          receipt.request.authorization['schedule'] as Map<String, Object?>;
      final duration = _instant(schedule['valid_until_utc']).difference(from);
      if (duration <= Duration.zero || duration > const Duration(days: 30)) {
        throw _invalid;
      }
    }
    recordDigest = _digest(raw['record_digest']);
    if (recordDigest != _recordDigest(receipt, activation, authorizedFromUtc)) {
      throw _invalid;
    }
    json = _bounded({...raw, 'receipt': receipt.json}, 131072);
  }
  String get schemaVersion => '1.0';
  late final StandingChangeReceipt receipt;
  late final String activation, recordDigest;
  late final String? authorizedFromUtc;
  late final Map<String, Object?> json;
}

final class StandingResponsibilitySnapshot {
  StandingResponsibilitySnapshot.fromJson(Object? value) {
    final raw = _closed(value, {
      'schema_version',
      'backend_id',
      'instance_id',
      'responsibility_id',
      'revision',
      'control_writable',
      'record',
    });
    current = StandingCurrent.fromJson({
      for (final key in raw.keys.where((key) => key != 'record')) key: raw[key],
    });
    record = raw['record'] == null
        ? null
        : StandingRecord.fromJson(raw['record']);
    if ((current.revision == 0) != (record == null) ||
        (record != null &&
            (current.revision != record!.receipt.revision ||
                current.backendId != record!.receipt.backendId ||
                current.responsibilityId !=
                    record!.receipt.request.responsibilityId))) {
      throw _invalid;
    }
    json = _bounded({...current.json, 'record': record?.json}, 147456);
  }
  String get schemaVersion => '1.0';
  String get backendId => current.backendId;
  String get instanceId => current.instanceId;
  String get responsibilityId => current.responsibilityId;
  int get revision => current.revision;
  bool get controlWritable => current.controlWritable;
  late final StandingCurrent current;
  late final StandingRecord? record;
  late final Map<String, Object?> json;
}

final class StandingChangeLookup {
  StandingChangeLookup.fromJson(Object? value, {StandingChange? original}) {
    final raw = _closed(value, {
      'schema_version',
      'status',
      'request',
      'current',
      'request_instance',
      'receipt',
      'mutation_fenced',
    });
    _version(raw);
    status = _literal(raw['status'], {'recorded', 'unobserved'});
    request = StandingChange.fromJson(raw['request']);
    if (original != null &&
        !request.matches(StandingChange.fromJson(original.body))) {
      throw _invalid;
    }
    current = StandingCurrent.fromJson(raw['current']);
    requestInstance = _literal(raw['request_instance'], {'current', 'retired'});
    mutationFenced = _flag(raw['mutation_fenced']);
    if (current.backendId != request.backendId ||
        current.responsibilityId != request.responsibilityId ||
        (requestInstance == 'current') !=
            (current.instanceId == request.instanceId)) {
      throw _invalid;
    }
    if (status == 'recorded') {
      receipt = StandingChangeReceipt.fromJson(
        raw['receipt'],
        original: request,
      );
      if (current.revision < receipt!.revision || mutationFenced) {
        throw _invalid;
      }
    } else {
      if (raw['receipt'] != null ||
          mutationFenced !=
              (requestInstance == 'retired' ||
                  current.revision > request.revision)) {
        throw _invalid;
      }
      receipt = null;
    }
    json = _bounded({
      ...raw,
      'request': request.body,
      'current': current.json,
      'receipt': receipt?.json,
    }, 262144);
  }
  String get schemaVersion => '1.0';
  late final String status, requestInstance;
  late final StandingChange request;
  late final StandingCurrent current;
  late final StandingChangeReceipt? receipt;
  late final bool mutationFenced;
  late final Map<String, Object?> json;
}

final class StandingResponsibilityPage {
  StandingResponsibilityPage.fromJson(Object? value) {
    final raw = _closed(value, {
      'schema_version',
      'backend_id',
      'instance_id',
      'control_writable',
      'items',
      'next_cursor',
    });
    _version(raw);
    backendId = _backend(raw['backend_id']);
    instanceId = _instance(raw['instance_id']);
    controlWritable = _flag(raw['control_writable']);
    final rows = raw['items'];
    if (rows is! List || rows.length > 20) throw _invalid;
    items = List<StandingRecord>.unmodifiable(
      rows.map(StandingRecord.fromJson),
    );
    String? previous;
    for (final item in items) {
      final id = item.receipt.request.responsibilityId;
      if (item.receipt.backendId != backendId ||
          (previous != null && id.compareTo(previous) <= 0)) {
        throw _invalid;
      }
      previous = id;
    }
    nextCursor = raw['next_cursor'] == null
        ? null
        : _instance(raw['next_cursor']);
    if (nextCursor != null && nextCursor != previous) throw _invalid;
    json = _bounded({
      ...raw,
      'items': items.map((record) => record.json).toList(),
    }, 262144);
  }
  String get schemaVersion => '1.0';
  late final String backendId, instanceId;
  late final bool controlWritable;
  late final List<StandingRecord> items;
  late final String? nextCursor;
  late final Map<String, Object?> json;
}
