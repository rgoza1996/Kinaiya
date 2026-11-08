/// Strict owner-facing companion wire values (ADR0045, ADR0110).
///
/// Separate from `lib/companion/models.dart` so revoked, expired, and stale
/// device directory entries remain visible to the owner without weakening the
/// foreground device parser.
library;

import 'dart:convert';

import 'models.dart' show SessionInfo;

const int ownerCompanionMaxSafeInteger = 9007199254740991;
const int ownerCompanionMaxDevices = 32;
const int ownerCompanionMaxCallsPerDevice = 100;
const int ownerCompanionMaxCallPageRows = 20;
const int ownerCompanionMaxNoteBytes = 4096;

const _bindingKeys = {'schema_version', 'session_id', 'thread_id', 'epoch'};

Never _invalid([String message = 'Invalid companion response']) =>
    throw FormatException(message);

Map<String, Object?> _object(Object? value, Set<String> keys) {
  if (value is! Map || value.length != keys.length) _invalid();
  final result = <String, Object?>{};
  for (final entry in value.entries) {
    if (entry.key is! String || !keys.contains(entry.key)) _invalid();
    result[entry.key as String] = entry.value;
  }
  if (result['schema_version'] != '1.0') _invalid();
  return result;
}

int ownerCompanionInteger(Object? value, {int min = 1}) {
  if (value is! int || value < min || value > ownerCompanionMaxSafeInteger) {
    _invalid('Invalid companion counter');
  }
  return value;
}

/// Checks UTF-16 surrogate pairs before UTF-8 byte length validation.
String _checkedUtf8String(
  Object? value, {
  required int minBytes,
  required int maxBytes,
}) {
  if (value is! String) _invalid('Invalid companion text');
  for (var i = 0; i < value.length; i++) {
    final c = value.codeUnitAt(i);
    if (c >= 0xd800 && c <= 0xdbff) {
      if (++i >= value.length) _invalid('Invalid companion text');
      final low = value.codeUnitAt(i);
      if (low < 0xdc00 || low > 0xdfff) _invalid('Invalid companion text');
    } else if (c >= 0xdc00 && c <= 0xdfff) {
      _invalid('Invalid companion text');
    }
  }
  final encoded = utf8.encode(value);
  if (encoded.length < minBytes ||
      encoded.length > maxBytes ||
      utf8.decode(encoded, allowMalformed: false) != value) {
    _invalid('Invalid companion text');
  }
  return value;
}

bool _isCcControl(int rune, {bool allowNewlineAndTab = false}) {
  if (allowNewlineAndTab && (rune == 0x09 || rune == 0x0A)) {
    return false;
  }
  return (rune >= 0x00 && rune <= 0x1F) || (rune >= 0x7F && rune <= 0x9F);
}

String ownerCompanionIdentifier(Object? value) {
  final s = _checkedUtf8String(value, minBytes: 1, maxBytes: 128);
  if (s.length > 128 || !RegExp(r'^[A-Za-z0-9_.:-]+$').hasMatch(s)) {
    _invalid('Invalid companion identifier');
  }
  return s;
}

String ownerCompanionIdempotencyKey(Object? value) {
  final s = _checkedUtf8String(value, minBytes: 1, maxBytes: 128);
  if (s.length > 128 || !RegExp(r'^[!-~]+$').hasMatch(s)) {
    _invalid('Invalid companion idempotency key');
  }
  return s;
}

String ownerCompanionShortText(Object? value) {
  final s = _checkedUtf8String(value, minBytes: 1, maxBytes: 480);
  if (s.runes.length > 120 || s.trim().isEmpty || s.runes.any(_isCcControl)) {
    _invalid('Invalid companion label or title');
  }
  return s;
}

String ownerCompanionNoteText(Object? value) {
  final s = _checkedUtf8String(
    value,
    minBytes: 0,
    maxBytes: ownerCompanionMaxNoteBytes,
  );
  if (s.runes.any((r) => _isCcControl(r, allowNewlineAndTab: true))) {
    _invalid('Invalid companion note text');
  }
  return s;
}

String ownerCompanionPairingCode(Object? value) {
  final s = _checkedUtf8String(value, minBytes: 43, maxBytes: 128);
  if (s.length < 43 ||
      s.length > 128 ||
      !RegExp(r'^[A-Za-z0-9_-]{43,128}$').hasMatch(s)) {
    _invalid('Invalid companion pairing code');
  }
  return s;
}

/// Exact Converse binding projection (`session_id`, `thread_id`, `epoch`).
/// Never spreads `channel`, `name`, or Agent `harness_version` metadata.
class OwnerCompanionBinding {
  OwnerCompanionBinding(String sessionId, String threadId, int epoch)
    : sessionId = ownerCompanionIdentifier(sessionId),
      threadId = ownerCompanionIdentifier(threadId),
      epoch = ownerCompanionInteger(epoch, min: 1);

  factory OwnerCompanionBinding.fromSession(SessionInfo session) {
    if (session.channel != 'converse') {
      throw const FormatException(
        'Companion binding requires a Converse session',
      );
    }
    return OwnerCompanionBinding(
      session.sessionId,
      session.threadId,
      session.epoch,
    );
  }

  factory OwnerCompanionBinding.fromJson(Object? value) =>
      _from(_object(value, _bindingKeys));

  static OwnerCompanionBinding _from(Map<String, Object?> raw) =>
      OwnerCompanionBinding(
        ownerCompanionIdentifier(raw['session_id']),
        ownerCompanionIdentifier(raw['thread_id']),
        ownerCompanionInteger(raw['epoch'], min: 1),
      );

  final String sessionId;
  final String threadId;
  final int epoch;

  Map<String, Object?> toJson() => <String, Object?>{
    'schema_version': '1.0',
    'session_id': sessionId,
    'thread_id': threadId,
    'epoch': epoch,
  };

  @override
  bool operator ==(Object other) =>
      other is OwnerCompanionBinding &&
      sessionId == other.sessionId &&
      threadId == other.threadId &&
      epoch == other.epoch;

  @override
  int get hashCode => Object.hash(sessionId, threadId, epoch);
}

class OwnerPairingIssued {
  OwnerPairingIssued._({
    required this.binding,
    required this.label,
    required this.pairingCode,
    required this.expiresAt,
  });

  factory OwnerPairingIssued.fromJson(Object? value) {
    final raw = _object(value, {
      ..._bindingKeys,
      'label',
      'pairing_code',
      'expires_at',
    });
    return OwnerPairingIssued._(
      binding: OwnerCompanionBinding._from(raw),
      label: ownerCompanionShortText(raw['label']),
      pairingCode: ownerCompanionPairingCode(raw['pairing_code']),
      expiresAt: ownerCompanionInteger(raw['expires_at'], min: 0),
    );
  }

  final OwnerCompanionBinding binding;
  final String label;
  final String pairingCode;
  final int expiresAt;
}

enum OwnerCompanionDeviceStatus {
  active('active', 'Active'),
  expired('expired', 'Expired'),
  revoked('revoked', 'Revoked'),
  stale('stale', 'Previous conversation');

  const OwnerCompanionDeviceStatus(this.wireValue, this.label);
  final String wireValue;
  final String label;

  static OwnerCompanionDeviceStatus fromWire(Object? value) => switch (value) {
    'active' => OwnerCompanionDeviceStatus.active,
    'expired' => OwnerCompanionDeviceStatus.expired,
    'revoked' => OwnerCompanionDeviceStatus.revoked,
    'stale' => OwnerCompanionDeviceStatus.stale,
    _ => _invalid('Unknown companion device status'),
  };
}

class OwnerCompanionDevice {
  OwnerCompanionDevice._({
    required this.deviceId,
    required this.deviceName,
    required this.label,
    required this.scope,
    required this.binding,
    required this.createdAt,
    required this.expiresAt,
    required this.revokedAt,
    required this.status,
  });

  factory OwnerCompanionDevice.fromJson(Object? value) {
    final raw = _object(value, {
      ..._bindingKeys,
      'device_id',
      'device_name',
      'label',
      'scope',
      'created_at',
      'expires_at',
      'revoked_at',
      'status',
    });
    if (raw['scope'] != 'companion_control') {
      _invalid('Unsupported companion device scope');
    }
    final createdAt = ownerCompanionInteger(raw['created_at'], min: 0);
    final expiresAt = ownerCompanionInteger(raw['expires_at'], min: 0);
    if (expiresAt <= createdAt) {
      _invalid('Invalid companion device expiry');
    }
    final revokedAt = raw['revoked_at'] == null
        ? null
        : ownerCompanionInteger(raw['revoked_at'], min: 0);
    final status = OwnerCompanionDeviceStatus.fromWire(raw['status']);
    if ((status == OwnerCompanionDeviceStatus.revoked) != (revokedAt != null)) {
      _invalid('Inconsistent companion device revocation state');
    }
    return OwnerCompanionDevice._(
      deviceId: ownerCompanionIdentifier(raw['device_id']),
      deviceName: ownerCompanionShortText(raw['device_name']),
      label: ownerCompanionShortText(raw['label']),
      scope: 'companion_control',
      binding: OwnerCompanionBinding._from(raw),
      createdAt: createdAt,
      expiresAt: expiresAt,
      revokedAt: revokedAt,
      status: status,
    );
  }

  final String deviceId;
  final String deviceName;
  final String label;
  final String scope;
  final OwnerCompanionBinding binding;
  final int createdAt;
  final int expiresAt;
  final int? revokedAt;
  final OwnerCompanionDeviceStatus status;

  bool matchesBinding(OwnerCompanionBinding? current) =>
      current != null && binding == current;

  bool canInviteFor(OwnerCompanionBinding? current) =>
      matchesBinding(current) && status == OwnerCompanionDeviceStatus.active;

  bool get canRevoke => status != OwnerCompanionDeviceStatus.revoked;
}

enum OwnerCompanionCallState {
  ringing('ringing', 'Ringing · waiting for companion'),
  acceptedWaitingMedia(
    'accepted_waiting_media',
    'Accepted · waiting for voice connection',
  ),
  declined('declined', 'Declined'),
  missed('missed', 'Missed'),
  ended('ended', 'Ended');

  const OwnerCompanionCallState(this.wireValue, this.label);
  final String wireValue;
  final String label;

  bool get terminal => this == declined || this == missed || this == ended;

  static OwnerCompanionCallState fromWire(Object? value) => switch (value) {
    'ringing' => OwnerCompanionCallState.ringing,
    'accepted_waiting_media' => OwnerCompanionCallState.acceptedWaitingMedia,
    'declined' => OwnerCompanionCallState.declined,
    'missed' => OwnerCompanionCallState.missed,
    'ended' => OwnerCompanionCallState.ended,
    _ => _invalid('Unknown companion call state'),
  };
}

const Set<String> _validEndReasons = <String>{
  'declined',
  'unanswered',
  'owner_ended',
  'device_ended',
  'media_unavailable',
  'device_revoked',
  'device_expired',
  'session_invalidated',
};

class OwnerCompanionCall {
  OwnerCompanionCall._({
    required this.callId,
    required this.sequence,
    required this.deviceId,
    required this.binding,
    required this.title,
    required this.state,
    required this.revision,
    required this.createdAt,
    required this.ringingDeadline,
    required this.mediaDeadline,
    required this.endReason,
    required this.note,
  });

  factory OwnerCompanionCall.fromJson(Object? value) {
    final raw = _object(value, {
      ..._bindingKeys,
      'call_id',
      'sequence',
      'device_id',
      'title',
      'state',
      'revision',
      'created_at',
      'ringing_deadline',
      'media_deadline',
      'end_reason',
      'note',
    });
    final state = OwnerCompanionCallState.fromWire(raw['state']);
    final createdAt = ownerCompanionInteger(raw['created_at'], min: 0);
    final ringingDeadline = ownerCompanionInteger(
      raw['ringing_deadline'],
      min: 0,
    );
    if (ringingDeadline < createdAt) {
      _invalid('Invalid companion ringing deadline');
    }
    final mediaDeadline = raw['media_deadline'] == null
        ? null
        : ownerCompanionInteger(raw['media_deadline'], min: 0);
    final endReasonRaw = raw['end_reason'];
    String? endReason;
    if (endReasonRaw != null) {
      final candidate = ownerCompanionIdentifier(endReasonRaw);
      if (!_validEndReasons.contains(candidate)) {
        _invalid('Unsupported companion end reason');
      }
      endReason = candidate;
    }
    switch (state) {
      case OwnerCompanionCallState.ringing:
        if (mediaDeadline != null || endReason != null) {
          _invalid('Inconsistent ringing call metadata');
        }
      case OwnerCompanionCallState.acceptedWaitingMedia:
        if (mediaDeadline == null || endReason != null) {
          _invalid('Inconsistent waiting-media call metadata');
        }
      case OwnerCompanionCallState.declined:
        if (endReason != 'declined') {
          _invalid('Inconsistent declined call reason');
        }
      case OwnerCompanionCallState.missed:
        if (endReason != 'unanswered') {
          _invalid('Inconsistent missed call reason');
        }
      case OwnerCompanionCallState.ended:
        if (endReason == null ||
            endReason == 'declined' ||
            endReason == 'unanswered') {
          _invalid('Inconsistent ended call reason');
        }
    }
    return OwnerCompanionCall._(
      callId: ownerCompanionIdentifier(raw['call_id']),
      sequence: ownerCompanionInteger(raw['sequence'], min: 1),
      deviceId: ownerCompanionIdentifier(raw['device_id']),
      binding: OwnerCompanionBinding._from(raw),
      title: ownerCompanionShortText(raw['title']),
      state: state,
      revision: ownerCompanionInteger(raw['revision'], min: 1),
      createdAt: createdAt,
      ringingDeadline: ringingDeadline,
      mediaDeadline: mediaDeadline,
      endReason: endReason,
      note: ownerCompanionNoteText(raw['note']),
    );
  }

  final String callId;
  final int sequence;
  final String deviceId;
  final OwnerCompanionBinding binding;
  final String title;
  final OwnerCompanionCallState state;
  final int revision;
  final int createdAt;
  final int ringingDeadline;
  final int? mediaDeadline;
  final String? endReason;
  final String note;

  String? get endReasonLabel => switch (endReason) {
    'declined' => 'Declined on companion',
    'unanswered' => 'No answer before 30s ringing deadline',
    'owner_ended' => 'Ended by host owner',
    'device_ended' => 'Ended on companion',
    'media_unavailable' => 'Voice connection unavailable (15s fallback)',
    'device_revoked' => 'Ended when companion grant was revoked',
    'device_expired' => 'Ended when companion grant expired',
    'session_invalidated' => 'Ended when conversation session changed',
    _ => null,
  };
}

class OwnerCompanionCallPage {
  OwnerCompanionCallPage._({required this.calls, required this.nextAfter});

  factory OwnerCompanionCallPage.fromJson(Object? value) {
    final raw = _object(value, {'schema_version', 'calls', 'next_after'});
    final rows = raw['calls'];
    if (rows is! List || rows.length > ownerCompanionMaxCallPageRows) {
      _invalid('Invalid companion call page');
    }
    final calls = <OwnerCompanionCall>[];
    final seen = <String>{};
    var previousSequence = 0;
    for (final item in rows) {
      final call = OwnerCompanionCall.fromJson(item);
      if (call.sequence <= previousSequence || !seen.add(call.callId)) {
        _invalid('Invalid companion call ordering');
      }
      calls.add(call);
      previousSequence = call.sequence;
    }
    final nextAfter = raw['next_after'] == null
        ? null
        : ownerCompanionInteger(raw['next_after'], min: 1);
    if (nextAfter != null &&
        (calls.isEmpty || nextAfter != calls.last.sequence)) {
      _invalid('Invalid companion call page cursor');
    }
    return OwnerCompanionCallPage._(
      calls: List<OwnerCompanionCall>.unmodifiable(calls),
      nextAfter: nextAfter,
    );
  }

  final List<OwnerCompanionCall> calls;
  final int? nextAfter;
}

List<OwnerCompanionDevice> decodeOwnerCompanionDevicePage(Object? value) {
  final raw = _object(value, {'schema_version', 'devices'});
  final rows = raw['devices'];
  if (rows is! List || rows.length > ownerCompanionMaxDevices) {
    _invalid('Invalid companion device directory');
  }
  final devices = <OwnerCompanionDevice>[];
  final seen = <String>{};
  for (final item in rows) {
    final device = OwnerCompanionDevice.fromJson(item);
    if (!seen.add(device.deviceId)) {
      _invalid('Duplicate companion device identifier');
    }
    devices.add(device);
  }
  return List<OwnerCompanionDevice>.unmodifiable(devices);
}
