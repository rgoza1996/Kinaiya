/// The device-only foreground control protocol. No media or owner credentials.
library;

import 'dart:convert';

const int companionMaxInteger = 9007199254740991;

Map<String, Object?> object(Object? raw) {
  if (raw is! Map<String, Object?> || raw['schema_version'] != '1.0') {
    throw const FormatException('Unsupported companion response');
  }
  return raw;
}

String text(Map<String, Object?> raw, String key, {int max = 128}) {
  final value = raw[key];
  if (value is! String ||
      value.length > max ||
      value.contains('\u0000') ||
      utf8.encode(value).length > max ||
      utf8.decode(utf8.encode(value)) != value) {
    throw const FormatException('Invalid companion text');
  }
  return value;
}

int integer(Map<String, Object?> raw, String key) {
  final value = raw[key];
  if (value is! int || value < 1 || value > companionMaxInteger) {
    throw const FormatException('Invalid companion counter');
  }
  return value;
}

class CompanionDevice {
  CompanionDevice.fromJson(Object? value) {
    final raw = object(value);
    deviceId = text(raw, 'device_id');
    name = text(raw, 'device_name', max: 480);
    label = text(raw, 'label', max: 480);
    sessionId = text(raw, 'session_id');
    threadId = text(raw, 'thread_id');
    epoch = integer(raw, 'epoch');
    expiresAt = integer(raw, 'expires_at');
    if (raw['scope'] != 'companion_control' || raw['status'] != 'active') {
      throw const FormatException('Companion grant is unavailable');
    }
  }

  late final String deviceId, name, label, sessionId, threadId;
  late final int epoch, expiresAt;
}

enum CompanionCallState {
  ringing,
  acceptedWaitingMedia,
  declined,
  missed,
  ended;

  bool get terminal => this == declined || this == missed || this == ended;
  String get label => switch (this) {
    ringing => 'Incoming invitation',
    acceptedWaitingMedia => 'Accepted · waiting for voice connection',
    declined => 'Declined',
    missed => 'Missed',
    ended => 'Ended',
  };
}

class CompanionCall {
  CompanionCall.fromJson(Object? value) {
    final raw = object(value);
    callId = text(raw, 'call_id');
    deviceId = text(raw, 'device_id');
    sessionId = text(raw, 'session_id');
    threadId = text(raw, 'thread_id');
    epoch = integer(raw, 'epoch');
    sequence = integer(raw, 'sequence');
    revision = integer(raw, 'revision');
    title = text(raw, 'title', max: 480);
    note = text(raw, 'note', max: 4096);
    endReason = raw['end_reason'] == null ? null : text(raw, 'end_reason');
    state = switch (raw['state']) {
      'ringing' => CompanionCallState.ringing,
      'accepted_waiting_media' => CompanionCallState.acceptedWaitingMedia,
      'declined' => CompanionCallState.declined,
      'missed' => CompanionCallState.missed,
      'ended' => CompanionCallState.ended,
      _ => throw const FormatException('Unknown companion call state'),
    };
  }

  late final String callId, deviceId, sessionId, threadId, title, note;
  late final String? endReason;
  late final int epoch, sequence, revision;
  late final CompanionCallState state;

  bool belongsTo(CompanionDevice device) =>
      deviceId == device.deviceId &&
      sessionId == device.sessionId &&
      threadId == device.threadId &&
      epoch == device.epoch;
}
