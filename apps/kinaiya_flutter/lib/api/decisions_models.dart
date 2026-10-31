/// Literal historical Decisions evidence. These values confer no authority.
library;

import 'dart:convert';

Never _invalid() => throw const FormatException('Invalid Decisions response');
Map<String, Object?> _object(Object? v) {
  if (v is! Map || v.keys.any((k) => k is! String)) _invalid();
  return v.cast<String, Object?>();
}

int _integer(Object? v, {int min = 0}) {
  if (v is! int || v < min || v > 9007199254740991) _invalid();
  return v;
}

String _text(Object? v, {int max = 4096, bool empty = false}) {
  if (v is! String || (!empty && v.isEmpty) || v.runes.length > max) _invalid();
  for (var i = 0; i < v.length; i++) {
    final unit = v.codeUnitAt(i);
    if (unit >= 0xd800 && unit <= 0xdbff) {
      if (++i >= v.length ||
          v.codeUnitAt(i) < 0xdc00 ||
          v.codeUnitAt(i) > 0xdfff) {
        _invalid();
      }
    } else if (unit >= 0xdc00 && unit <= 0xdfff) {
      _invalid();
    }
  }
  return v;
}

String? _optional(Object? v, {int max = 4096}) =>
    v == null ? null : _text(v, max: max);
void _version(Map<String, Object?> v) {
  if (v['contract_version'] != 1) _invalid();
}

List<Object?> _list(Object? v, int max) {
  if (v is! List || v.length > max) _invalid();
  return v;
}

String decisionsLiteral(Object? value) =>
    const JsonEncoder.withIndent('  ').convert(value);

class DecisionRow {
  DecisionRow.fromJson(Map<String, Object?> j)
    : eventId = _text(j['source_event_id'], max: 128),
      sequence = _integer(j['source_sequence'], min: 1),
      sessionId = _text(j['session_id'], max: 128),
      threadId = _text(j['thread_id'], max: 128),
      epoch = _integer(j['epoch'], min: 1),
      kind = _text(j['source_kind'], max: 128),
      origin = _text(j['source_origin'], max: 128),
      revision = j['source_revision'] == null
          ? null
          : _integer(j['source_revision'], min: 1),
      createdAt = _text(j['created_at'], max: 64),
      preview = _text(j['preview'], max: 1000, empty: true),
      disposition = _text(j['disposition'], max: 64),
      outcome = _optional(j['outcome'], max: 128),
      decisionId = _optional(j['decision_id'], max: 128),
      provider = _optional(j['provider'], max: 1024),
      model = _optional(j['model'], max: 1024),
      confidence = j['confidence'] == null
          ? null
          : (j['confidence'] is num
                ? (j['confidence'] as num).toDouble()
                : double.nan),
      callCount = _integer(j['call_count']),
      captureState = _text(j['capture_state'], max: 64) {
    if (DateTime.tryParse(createdAt) == null ||
        !{
          'classified',
          'refused',
          'error',
          'pending',
          'reused',
          'no_inference',
          'unavailable',
        }.contains(disposition) ||
        !{
          'retained',
          'expired',
          'unavailable',
          'not_applicable',
        }.contains(captureState) ||
        (confidence != null &&
            (!confidence!.isFinite || confidence! < 0 || confidence! > 1))) {
      _invalid();
    }
  }
  final String eventId,
      sessionId,
      threadId,
      kind,
      origin,
      createdAt,
      preview,
      disposition,
      captureState;
  final String? outcome, decisionId, provider, model;
  final int sequence, epoch, callCount;
  final int? revision;
  final double? confidence;
  bool get isAgent => kind.startsWith('agent.');
  String get confidenceLabel => confidence == null
      ? 'Unavailable'
      : '${(confidence! * 100).toStringAsFixed(1)}%';
}

class DecisionPage {
  DecisionPage.fromJson(Map<String, Object?> j)
    : through = _integer(j['through_sequence']),
      next = j['next_before_sequence'] == null
          ? null
          : _integer(j['next_before_sequence'], min: 1),
      exhausted = j['exhausted'] == true,
      timezone = _text(j['timezone'], max: 128),
      rangeStart = _optional(j['range_start_utc'], max: 64),
      rangeEnd = _optional(j['range_end_utc'], max: 64),
      rows = List.unmodifiable(
        _list(j['rows'], 20).map((v) => DecisionRow.fromJson(_object(v))),
      ) {
    _version(j);
    if (j['exhausted'] is! bool ||
        (!exhausted && next == null) ||
        (exhausted && next != null)) {
      _invalid();
    }
    var last = through + 1;
    for (final row in rows) {
      if (row.sequence >= last) _invalid();
      last = row.sequence;
    }
    if (next != null && next! > last) _invalid();
  }
  final int through;
  final int? next;
  final bool exhausted;
  final String timezone;
  final String? rangeStart, rangeEnd;
  final List<DecisionRow> rows;
}

class DecisionCall {
  DecisionCall.fromJson(Map<String, Object?> j)
    : sequence = _integer(j['call_sequence'], min: 1),
      id = _text(j['call_id'], max: 128),
      sourceId = _text(j['source_event_id'], max: 128),
      status = _text(j['status'], max: 64),
      captureState = _text(j['capture_state'], max: 64),
      data = Map.unmodifiable(_object(jsonDecode(jsonEncode(j)))) {
    if (!{
          'started',
          'completed',
          'refused',
          'error',
          'cancelled',
          'reused',
        }.contains(status) ||
        !{
          'retained',
          'expired',
          'unavailable',
          'not_applicable',
        }.contains(captureState)) {
      _invalid();
    }
    _text(j['attempt_id'], max: 128);
    _integer(j['attempt'], min: 1);
  }
  final int sequence;
  final String id, sourceId, status, captureState;
  final Map<String, Object?> data;
}

class DecisionDetail {
  DecisionDetail.fromJson(Map<String, Object?> j)
    : summary = DecisionRow.fromJson(_object(j['summary'])),
      source = Map.unmodifiable(_object(j['source'])),
      receipts = List.unmodifiable(_list(j['receipts'], 100).map(_object)),
      receiptsTruncated = j['receipts_truncated'] == true,
      through = _integer(j['through_call_sequence']),
      next = j['next_before_call_sequence'] == null
          ? null
          : _integer(j['next_before_call_sequence'], min: 1),
      exhausted = j['exhausted'] == true,
      calls = List.unmodifiable(
        _list(j['calls'], 5).map((v) => DecisionCall.fromJson(_object(v))),
      ) {
    _version(j);
    if (j['exhausted'] is! bool ||
        j['receipts_truncated'] is! bool ||
        (!exhausted && next == null) ||
        (exhausted && next != null) ||
        source['event_id'] != summary.eventId ||
        source['session_id'] != summary.sessionId ||
        source['thread_id'] != summary.threadId ||
        source['sequence'] != summary.sequence ||
        source['epoch'] != summary.epoch) {
      _invalid();
    }
    final payload = _object(source['payload']);
    final rawRevision = payload['revision'] ?? 1;
    final revision =
        rawRevision is int &&
            rawRevision >= 1 &&
            rawRevision <= 9007199254740991
        ? rawRevision
        : null;
    if (source['kind'] != summary.kind ||
        source['origin'] != summary.origin ||
        source['created_at'] != summary.createdAt ||
        revision != summary.revision ||
        (summary.isAgent &&
            (summary.disposition != 'no_inference' ||
                summary.callCount != 0 ||
                calls.isNotEmpty ||
                !exhausted))) {
      _invalid();
    }
    var last = through + 1;
    for (final call in calls) {
      if (call.sourceId != summary.eventId || call.sequence >= last) _invalid();
      last = call.sequence;
    }
    if (next != null && next! > last) _invalid();
  }
  final DecisionRow summary;
  final Map<String, Object?> source;
  final List<Map<String, Object?>> receipts;
  final bool receiptsTruncated, exhausted;
  final int through;
  final int? next;
  final List<DecisionCall> calls;
}
