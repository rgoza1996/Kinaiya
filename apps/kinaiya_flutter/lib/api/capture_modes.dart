/// Converse-owned capture selection and exact original-request proof.
library;

import 'dart:convert';

import 'package:crypto/crypto.dart';

enum CaptureMode {
  adaptiveLive('adaptive_live', 'Adaptive Live'),
  rollingWindows('rolling_windows', 'Rolling Windows'),
  legacySilence('legacy_silence', 'Legacy Silence · Advanced');

  const CaptureMode(this.wire, this.label);
  final String wire, label;
}

const _invalid = FormatException('Invalid Converse capture mode proof');
const _contract = 'converse_capture.v1';
const _boundary = 'next_capture';
const _maxGeneration = 9007199254740991;

Map<String, Object?> _object(Object? value, Set<String> fields) {
  if (value is! Map<String, Object?> ||
      value.length != fields.length ||
      !value.keys.every(fields.contains)) {
    throw _invalid;
  }
  return value;
}

String _string(Object? value, int length) {
  if (value is! String ||
      value.length != length ||
      RegExp(r'^[a-f0-9]+$').stringMatch(value) != value) {
    throw _invalid;
  }
  return value;
}

int _integer(Object? value, int maximum, [int minimum = 0]) {
  if (value is! int || value < minimum || value > maximum) throw _invalid;
  return value;
}

bool _flag(Object? value) {
  if (value is! bool) throw _invalid;
  return value;
}

CaptureMode? _mode(Object? value) {
  if (value == null) return null;
  for (final mode in CaptureMode.values) {
    if (value == mode.wire) return mode;
  }
  throw _invalid;
}

void _version(Map<String, Object?> value) {
  if (value['contract'] != _contract) throw _invalid;
}

class CaptureModeSelection {
  const CaptureModeSelection.host() : mode = null;
  const CaptureModeSelection.saved(CaptureMode value) : mode = value;
  factory CaptureModeSelection.fromJson(Object? value) {
    final raw = _object(value, {'source', 'mode'});
    final mode = _mode(raw['mode']);
    if ((raw['source'] != 'host' || mode != null) &&
        (raw['source'] != 'saved' || mode == null)) {
      throw _invalid;
    }
    return mode == null
        ? const CaptureModeSelection.host()
        : CaptureModeSelection.saved(mode);
  }
  final CaptureMode? mode;
  bool get isHost => mode == null;
  Map<String, Object?> get json => Map.unmodifiable({
    'mode': mode?.wire,
    'source': isHost ? 'host' : 'saved',
  });
  bool matches(CaptureModeSelection other) => mode == other.mode;
}

class CaptureModeSnapshot {
  CaptureModeSnapshot.fromJson(Object? value) {
    final raw = _object(value, {
      'contract',
      'backend_id',
      'authority_epoch',
      'authority_generation',
      'revision',
      'state_token',
      'selection',
      'default_mode',
      'selected_mode',
      'capture_state',
      'adaptive_live_enabled',
      'change_boundary',
      'writable',
      'max_changes',
    });
    _version(raw);
    backendId = _string(raw['backend_id'], 32);
    epoch = _string(raw['authority_epoch'], 32);
    generation = _integer(raw['authority_generation'], _maxGeneration);
    revision = _integer(raw['revision'], 1000);
    stateToken = _string(raw['state_token'], 64);
    selection = CaptureModeSelection.fromJson(raw['selection']);
    defaultMode = _mode(raw['default_mode']);
    selectedMode = _mode(raw['selected_mode']);
    adaptiveLiveEnabled = _flag(raw['adaptive_live_enabled']);
    writable = _flag(raw['writable']);
    if (raw['capture_state'] != 'not_observed' ||
        raw['change_boundary'] != _boundary ||
        raw['max_changes'] is! int ||
        raw['max_changes'] != 1000 ||
        (writable && revision == 1000) ||
        selectedMode != (selection.isHost ? defaultMode : selection.mode)) {
      throw _invalid;
    }
  }
  late final String backendId, epoch, stateToken;
  late final int generation, revision;
  late final CaptureModeSelection selection;
  late final CaptureMode? defaultMode, selectedMode;
  late final bool writable, adaptiveLiveEnabled;

  bool sameObservation(CaptureModeSnapshot other) =>
      backendId == other.backendId &&
      epoch == other.epoch &&
      generation == other.generation &&
      revision == other.revision &&
      stateToken == other.stateToken &&
      defaultMode == other.defaultMode &&
      selection.matches(other.selection);
}

class CaptureModeChange {
  CaptureModeChange.fromJson(Object? value) {
    final raw = _object(value, {
      'contract',
      'backend_id',
      'authority_epoch',
      'authority_generation',
      'expected_revision',
      'state_token',
      'expected_default_mode',
      'idempotency_key',
      'selection',
    });
    _version(raw);
    backendId = _string(raw['backend_id'], 32);
    epoch = _string(raw['authority_epoch'], 32);
    generation = _integer(raw['authority_generation'], _maxGeneration);
    revision = _integer(raw['expected_revision'], 1000);
    stateToken = _string(raw['state_token'], 64);
    defaultMode = _mode(raw['expected_default_mode']);
    selection = CaptureModeSelection.fromJson(raw['selection']);
    final id = raw['idempotency_key'];
    if (id is! String ||
        id.isEmpty ||
        id.length > 128 ||
        RegExp(r'^[A-Za-z0-9:._-]+$').stringMatch(id) != id) {
      throw _invalid;
    }
    key = id;
    // Both levels are sorted ASCII keys; all values in this contract are ASCII.
    body = Map.unmodifiable({
      'authority_epoch': epoch,
      'authority_generation': generation,
      'backend_id': backendId,
      'contract': _contract,
      'expected_default_mode': defaultMode?.wire,
      'expected_revision': revision,
      'idempotency_key': key,
      'selection': selection.json,
      'state_token': stateToken,
    });
    final bytes = utf8.encode(jsonEncode(body));
    if (bytes.length > 4096) throw _invalid;
    digest = sha256.convert(bytes).toString();
  }

  factory CaptureModeChange.create(
    CaptureModeSnapshot base,
    CaptureModeSelection selection,
    String key,
  ) => CaptureModeChange.fromJson({
    'contract': _contract,
    'backend_id': base.backendId,
    'authority_epoch': base.epoch,
    'authority_generation': base.generation,
    'expected_revision': base.revision,
    'state_token': base.stateToken,
    'expected_default_mode': base.defaultMode?.wire,
    'idempotency_key': key,
    'selection': selection.json,
  });

  late final String backendId, epoch, stateToken, key, digest;
  late final int generation, revision;
  late final CaptureMode? defaultMode;
  late final CaptureModeSelection selection;
  late final Map<String, Object?> body;

  bool matchesSnapshot(CaptureModeSnapshot snapshot) =>
      backendId == snapshot.backendId &&
      epoch == snapshot.epoch &&
      generation == snapshot.generation &&
      revision == snapshot.revision &&
      stateToken == snapshot.stateToken &&
      defaultMode == snapshot.defaultMode;
}

class CaptureModeReceipt {
  CaptureModeReceipt.fromJson(Object? value, CaptureModeChange original) {
    final raw = _object(value, {
      'contract',
      'status',
      'backend_id',
      'authority_epoch',
      'authority_generation',
      'revision',
      'idempotency_key',
      'selection',
      'default_mode_observed',
      'selected_mode_observed',
      'change_boundary',
    });
    _version(raw);
    selection = CaptureModeSelection.fromJson(raw['selection']);
    revision = _integer(raw['revision'], 1000, 1);
    if (raw['status'] != 'saved' ||
        raw['backend_id'] != original.backendId ||
        raw['authority_epoch'] != original.epoch ||
        _integer(raw['authority_generation'], _maxGeneration) !=
            original.generation ||
        revision != original.revision + 1 ||
        raw['idempotency_key'] != original.key ||
        !selection.matches(original.selection) ||
        _mode(raw['default_mode_observed']) != original.defaultMode ||
        _mode(raw['selected_mode_observed']) !=
            (selection.isHost ? original.defaultMode : selection.mode) ||
        raw['change_boundary'] != _boundary) {
      throw _invalid;
    }
  }
  late final CaptureModeSelection selection;
  late final int revision;
}

class CaptureModeLookup {
  CaptureModeLookup.fromJson(Object? value, CaptureModeChange original) {
    final raw = _object(value, {
      'contract',
      'backend_id',
      'authority_epoch',
      'authority_generation',
      'request_sha256',
      'outcome',
      'receipt',
      'mutation_fenced',
    });
    _version(raw);
    final epoch = _string(raw['authority_epoch'], 32);
    final generation = _integer(raw['authority_generation'], _maxGeneration);
    fenced = _flag(raw['mutation_fenced']);
    if (raw['backend_id'] != original.backendId ||
        raw['request_sha256'] != original.digest ||
        generation < original.generation) {
      throw _invalid;
    }
    saved = raw['outcome'] == 'saved';
    if (saved) {
      receipt = CaptureModeReceipt.fromJson(raw['receipt'], original);
      if (fenced) throw _invalid;
    } else {
      receipt = null;
      if (raw['outcome'] != 'unobserved' ||
          raw['receipt'] != null ||
          ((generation > original.generation || epoch != original.epoch) &&
              !fenced)) {
        throw _invalid;
      }
    }
  }
  late final bool saved, fenced;
  late final CaptureModeReceipt? receipt;
}
