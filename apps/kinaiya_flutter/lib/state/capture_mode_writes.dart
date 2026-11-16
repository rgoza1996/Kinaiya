/// Bounded RAM-only drafts and exact save recovery, scoped to Converse and owner.
library;

import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:uuid/uuid.dart';

import '../api/capture_modes.dart';
import '../api/capture_modes_client.dart';

class CaptureModeDraft {
  CaptureModeDraft(this.base) : selection = base.selection;
  final CaptureModeSnapshot base;
  CaptureModeSelection selection;
  bool get dirty => !selection.matches(base.selection);
}

class CaptureModeScope {
  CaptureModeScope._(this.owner, this.snapshot)
    : draft = CaptureModeDraft(snapshot);
  final CaptureModeApi owner;
  CaptureModeSnapshot snapshot;
  CaptureModeDraft draft;
  PendingCaptureModeChange? pending;
  String? notice;
  bool isCurrentBackend = true;
}

class PendingCaptureModeChange {
  PendingCaptureModeChange._(this.scope, this.request);
  final CaptureModeScope scope;
  final CaptureModeChange request;
  bool started = false, checking = false, complete = false;
  String notice = 'Saving selection for the next capture…';
}

final captureModeWrites = CaptureModeWrites();

class CaptureModeWrites extends ChangeNotifier {
  final List<CaptureModeScope> _scopes = [];

  void _observed(CaptureModeApi api, String backendId) {
    for (final scope in _scopes) {
      if (api.sameOwner(scope.owner)) {
        scope.isCurrentBackend = scope.snapshot.backendId == backendId;
      }
    }
  }

  CaptureModeScope? open(CaptureModeApi api, CaptureModeSnapshot snapshot) {
    for (final scope in _scopes) {
      if (!api.sameOwner(scope.owner) ||
          scope.snapshot.backendId != snapshot.backendId) {
        continue;
      }
      if (snapshot.generation < scope.snapshot.generation ||
          snapshot.revision < scope.snapshot.revision) {
        throw const FormatException(
          'Converse capture observation moved backwards',
        );
      }
      _observed(api, snapshot.backendId);
      scope.snapshot = snapshot;
      if (scope.pending == null && !scope.draft.dirty) {
        scope.draft = CaptureModeDraft(snapshot);
      }
      return scope;
    }
    _observed(api, snapshot.backendId);
    if (_scopes.length >= 16) {
      final idle = _scopes.indexWhere(
        (s) => s.pending == null && !s.draft.dirty,
      );
      if (idle < 0) return null;
      _scopes.removeAt(idle);
    }
    final scope = CaptureModeScope._(api, snapshot);
    _scopes.add(scope);
    return scope;
  }

  void reset(CaptureModeScope scope) {
    if (!_scopes.contains(scope) || scope.pending != null) return;
    scope.draft = CaptureModeDraft(scope.snapshot);
    scope.notice = null;
    notifyListeners();
  }

  void select(CaptureModeScope scope, CaptureModeSelection selection) {
    if (!_scopes.contains(scope) || scope.pending != null) return;
    scope.draft.selection = selection;
    scope.notice = null;
    notifyListeners();
  }

  PendingCaptureModeChange? begin(CaptureModeScope scope, CaptureModeApi api) {
    if (!_scopes.contains(scope) ||
        !api.sameOwner(scope.owner) ||
        !scope.isCurrentBackend ||
        !api.hasCapacity ||
        !scope.snapshot.writable ||
        scope.pending != null ||
        !scope.draft.dirty ||
        !scope.draft.base.sameObservation(scope.snapshot)) {
      return null;
    }
    final request = CaptureModeChange.create(
      scope.draft.base,
      scope.draft.selection,
      'capture-${const Uuid().v4()}',
    );
    final pending = PendingCaptureModeChange._(scope, request);
    scope.pending = pending;
    scope.notice = null;
    notifyListeners();
    return pending;
  }

  bool _registered(PendingCaptureModeChange pending) =>
      !pending.complete &&
      _scopes.contains(pending.scope) &&
      identical(pending.scope.pending, pending);

  void _changed(PendingCaptureModeChange pending) {
    if (_registered(pending)) notifyListeners();
  }

  void _settle(
    PendingCaptureModeChange pending, {
    required bool saved,
    String? notice,
  }) {
    if (!_registered(pending)) return;
    pending.complete = true;
    final scope = pending.scope;
    scope.pending = null;
    if (saved) {
      scope.draft = CaptureModeDraft(scope.snapshot);
      scope.notice =
          notice ??
          'Selection saved for the next Call. No microphone state is observed by this receipt.';
    } else {
      scope.notice =
          notice ??
          'The original save did not apply and cannot apply. Review the inspected selection before saving again.';
    }
    notifyListeners();
  }

  /// An original is dispatched once. Recovery is read-only exact lookup.
  Future<void> send(
    PendingCaptureModeChange pending,
    CaptureModeApi api,
  ) async {
    if (!_registered(pending) ||
        pending.started ||
        !api.sameOwner(pending.scope.owner)) {
      return;
    }
    pending.started = true;
    final scope = pending.scope;
    if (!api.hasCapacity ||
        !scope.isCurrentBackend ||
        !scope.snapshot.writable ||
        !pending.request.matchesSnapshot(scope.snapshot)) {
      // No transport was entered and this operation can never be sent again.
      _settle(
        pending,
        saved: false,
        notice:
            'Save was not sent. Inspect the current Converse capture selection before another change.',
      );
      return;
    }
    final attempt = api.change(pending.request);
    try {
      await attempt.result;
      _settle(pending, saved: true);
    } catch (_) {
      if (!attempt.dispatched) {
        _settle(pending, saved: false);
      } else {
        pending.notice =
            'Save outcome is unconfirmed. Check save status before another change.';
        _changed(pending);
      }
    }
  }

  Future<void> lookup(
    PendingCaptureModeChange pending,
    CaptureModeApi api,
  ) async {
    if (!_registered(pending) ||
        pending.checking ||
        !api.sameOwner(pending.scope.owner) ||
        !pending.scope.isCurrentBackend ||
        !api.hasCapacity) {
      return;
    }
    pending.checking = true;
    _changed(pending);
    try {
      final proof = await api.lookup(pending.request).result;
      if (proof.saved || proof.fenced) {
        _settle(pending, saved: proof.saved);
      } else {
        pending.notice =
            'The original save was not observed and could still apply. Keep checking its status.';
      }
    } catch (_) {
      pending.notice =
          'The original save status is unavailable. Its outcome remains unconfirmed.';
    } finally {
      pending.checking = false;
      _changed(pending);
    }
  }
}
