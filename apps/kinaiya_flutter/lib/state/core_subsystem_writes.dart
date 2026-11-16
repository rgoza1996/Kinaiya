/// Bounded RAM-only drafts and exact save recovery, scoped to Core and owner.
library;

import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:uuid/uuid.dart';

import '../api/core_subsystems.dart';
import '../api/core_subsystems_client.dart';

class CoreSubsystemDraft {
  CoreSubsystemDraft(this.base) : selection = base.selection;
  final CoreSubsystemSnapshot base;
  CoreSubsystemSelection selection;
  bool get dirty => !selection.matches(base.selection);
}

class CoreSubsystemScope {
  CoreSubsystemScope._(this.owner, this.snapshot)
    : draft = CoreSubsystemDraft(snapshot);
  final CoreSubsystemApi owner;
  CoreSubsystemSnapshot snapshot;
  CoreSubsystemDraft draft;
  PendingCoreSubsystemChange? pending;
  String? notice;
  bool isCurrentBackend = true;
}

class PendingCoreSubsystemChange {
  PendingCoreSubsystemChange._(this.scope, this.request);
  final CoreSubsystemScope scope;
  final CoreSubsystemChange request;
  bool started = false, checking = false, complete = false;
  String notice = 'Saving selection for the next launch…';
}

final coreSubsystemWrites = CoreSubsystemWrites();

class CoreSubsystemWrites extends ChangeNotifier {
  final List<CoreSubsystemScope> _scopes = [];

  void _observed(CoreSubsystemApi api, String backendId) {
    for (final scope in _scopes) {
      if (api.sameOwner(scope.owner)) {
        scope.isCurrentBackend = scope.snapshot.backendId == backendId;
      }
    }
  }

  CoreSubsystemScope? open(
    CoreSubsystemApi api,
    CoreSubsystemSnapshot snapshot,
  ) {
    for (final scope in _scopes) {
      if (!api.sameOwner(scope.owner) ||
          scope.snapshot.backendId != snapshot.backendId) {
        continue;
      }
      if (snapshot.generation < scope.snapshot.generation ||
          snapshot.revision < scope.snapshot.revision) {
        throw const FormatException(
          'Core subsystem observation moved backwards',
        );
      }
      _observed(api, snapshot.backendId);
      scope.snapshot = snapshot;
      if (scope.pending == null && !scope.draft.dirty) {
        scope.draft = CoreSubsystemDraft(snapshot);
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
    final scope = CoreSubsystemScope._(api, snapshot);
    _scopes.add(scope);
    return scope;
  }

  void reset(CoreSubsystemScope scope) {
    if (!_scopes.contains(scope) || scope.pending != null) return;
    scope.draft = CoreSubsystemDraft(scope.snapshot);
    scope.notice = null;
    notifyListeners();
  }

  void select(CoreSubsystemScope scope, CoreSubsystemSelection selection) {
    if (!_scopes.contains(scope) || scope.pending != null) return;
    scope.draft.selection = selection;
    scope.notice = null;
    notifyListeners();
  }

  PendingCoreSubsystemChange? begin(
    CoreSubsystemScope scope,
    CoreSubsystemApi api,
  ) {
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
    final request = CoreSubsystemChange.create(
      scope.draft.base,
      scope.draft.selection,
      'core-${const Uuid().v4()}',
    );
    final pending = PendingCoreSubsystemChange._(scope, request);
    scope.pending = pending;
    scope.notice = null;
    notifyListeners();
    return pending;
  }

  bool _registered(PendingCoreSubsystemChange pending) =>
      !pending.complete &&
      _scopes.contains(pending.scope) &&
      identical(pending.scope.pending, pending);

  void _changed(PendingCoreSubsystemChange pending) {
    if (_registered(pending)) notifyListeners();
  }

  void _settle(
    PendingCoreSubsystemChange pending, {
    required bool saved,
    String? notice,
  }) {
    if (!_registered(pending)) return;
    pending.complete = true;
    final scope = pending.scope;
    scope.pending = null;
    if (saved) {
      scope.draft = CoreSubsystemDraft(scope.snapshot);
      scope.notice =
          notice ??
          'Selection saved for the next managed host launch. Running services are not observed here.';
    } else {
      scope.notice =
          notice ??
          'The original save did not apply and cannot apply. Review the inspected selection before saving again.';
    }
    notifyListeners();
  }

  /// An original is dispatched once. Recovery is read-only exact lookup.
  Future<void> send(
    PendingCoreSubsystemChange pending,
    CoreSubsystemApi api,
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
            'Save was not sent. Inspect the current Core selection before another change.',
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
    PendingCoreSubsystemChange pending,
    CoreSubsystemApi api,
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
