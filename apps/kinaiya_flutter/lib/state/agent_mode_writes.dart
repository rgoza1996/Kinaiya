/// RAM-only operating-mode drafts and uncertain original requests.
library;

import 'dart:async';
import 'package:flutter/foundation.dart';
import '../api/agent_control.dart';
import '../api/agent_control_client.dart';

enum AgentModeOutcome { recorded, refused }

class AgentModeWriteScope {
  AgentModeWriteScope._(this.owner, this.snapshot) : selection = snapshot.mode;
  final AgentControlApi owner;
  AgentControlSnapshot snapshot;
  AgentMode selection;
  PendingAgentModeChange? pending;
  String? notice;
  bool get dirty => selection != snapshot.mode;
}

class PendingAgentModeChange {
  PendingAgentModeChange._(this.scope, this.request);
  final AgentModeWriteScope scope;
  final AgentModeChange request;
  int mutationOwners = 0, lookupOwners = 0;
  bool uncertain = false, timedOut = false, complete = false;
  String notice = 'Saving Agent operating mode…';
}

// The registry outlives panels, API facades and same-owner reconnects.
final agentModeWrites = AgentModeWrites();

class AgentModeWrites extends ChangeNotifier {
  AgentModeWrites({this.displayDeadline = const Duration(seconds: 8)});
  final Duration displayDeadline;
  final List<AgentModeWriteScope> _scopes = [];
  bool _disposed = false;

  AgentModeWriteScope? open(
    AgentControlApi api,
    AgentControlSnapshot snapshot,
  ) {
    if (_disposed) return null;
    for (final scope in _scopes) {
      if (api.sameOwner(scope.owner) &&
          scope.snapshot.backendId == snapshot.backendId) {
        final clean = !scope.dirty;
        scope.snapshot = snapshot;
        if (scope.pending == null && clean) scope.selection = snapshot.mode;
        return scope;
      }
    }
    // Bound retained drafts as well as logical operations. Dirty or unresolved
    // scopes never make room by silently discarding an owner's intent.
    if (_scopes.length >= 16) {
      final idle = _scopes.indexWhere(
        (scope) => scope.pending == null && !scope.dirty,
      );
      if (idle < 0) return null;
      _scopes.removeAt(idle);
    }
    final scope = AgentModeWriteScope._(api, snapshot);
    _scopes.add(scope);
    return scope;
  }

  void reset(AgentModeWriteScope scope) {
    if (!_scopes.contains(scope) || scope.pending != null) return;
    scope.selection = scope.snapshot.mode;
    scope.notice = null;
    _notify();
  }

  bool _registered(PendingAgentModeChange operation) =>
      !operation.complete &&
      _scopes.contains(operation.scope) &&
      identical(operation.scope.pending, operation);

  bool _scope(
    PendingAgentModeChange operation,
    AgentControlApi api,
    AgentControlSnapshot? state,
  ) =>
      state != null &&
      api.sameOwner(operation.scope.owner) &&
      state.backendId == operation.request.backendId;

  PendingAgentModeChange? begin(
    AgentModeWriteScope scope,
    AgentControlApi api,
    AgentModeChange request,
  ) {
    final state = scope.snapshot;
    if (_disposed ||
        !_scopes.contains(scope) ||
        !state.controlWritable ||
        !api.sameOwner(scope.owner) ||
        state.backendId != request.backendId ||
        state.instanceId != request.instanceId ||
        state.revision != request.revision ||
        !state.availableModes.contains(request.mode) ||
        !api.hasCapacity ||
        scope.pending != null) {
      return null;
    }
    final operation = PendingAgentModeChange._(scope, request);
    scope.pending = operation;
    scope.notice = null;
    return operation;
  }

  bool canRetry(
    PendingAgentModeChange operation,
    AgentControlApi api,
    AgentControlSnapshot? state,
  ) =>
      !_disposed &&
      _registered(operation) &&
      _scope(operation, api, state) &&
      operation.scope.snapshot.matches(state!) &&
      state.controlWritable &&
      state.instanceId == operation.request.instanceId &&
      state.availableModes.contains(operation.request.mode) &&
      api.hasCapacity &&
      operation.mutationOwners < 2 &&
      (operation.uncertain ||
          operation.timedOut ||
          operation.mutationOwners == 0);

  bool canLookup(
    PendingAgentModeChange operation,
    AgentControlApi api,
    AgentControlSnapshot? state,
  ) =>
      !_disposed &&
      _registered(operation) &&
      _scope(operation, api, state) &&
      api.hasCapacity &&
      operation.lookupOwners == 0;

  AgentModeOutcome? _settle(
    PendingAgentModeChange operation,
    AgentModeOutcome outcome,
  ) {
    if (!_registered(operation)) return null;
    operation.complete = true;
    final scope = operation.scope;
    scope.pending = null;
    if (outcome == AgentModeOutcome.recorded) {
      // This is historical proof. Only a fresh snapshot may update mode.
      scope.selection = scope.snapshot.mode;
      scope.notice =
          'Agent mode save confirmed in history. Refresh to inspect the current mode.';
    } else {
      scope.notice =
          'The earlier mode save was not applied and is permanently fenced. '
          'Review the current mode before another save.';
    }
    _notify();
    return outcome;
  }

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  void _changed(PendingAgentModeChange operation) {
    if (_registered(operation)) _notify();
  }

  Future<AgentModeOutcome?> retry(
    PendingAgentModeChange operation,
    AgentControlApi api,
    AgentControlSnapshot? state,
  ) async {
    if (!canRetry(operation, api, state)) return null;
    operation.mutationOwners++;
    operation.timedOut = false;
    operation.notice = 'Saving Agent operating mode…';
    final attempt = api.change(operation.request);
    // Actual owners remain counted after public timeout and logical settlement.
    unawaited(
      attempt.settled.then((_) {
        operation.mutationOwners--;
        _changed(operation);
      }),
    );
    _changed(operation);
    final display = Timer(displayDeadline, () {
      if (_registered(operation) && operation.mutationOwners > 0) {
        operation.timedOut = true;
        operation.uncertain = true;
        operation.notice =
            'Still waiting for the host. The mode may have been saved.';
        _changed(operation);
      }
    });
    try {
      await attempt.result;
      return _settle(operation, AgentModeOutcome.recorded);
    } catch (_) {
      if (_registered(operation)) {
        operation.uncertain = operation.uncertain || attempt.dispatched;
        operation.notice = operation.uncertain
            ? 'Mode save is unconfirmed. Check the exact original save before another change.'
            : 'The request was not sent. Retry the same save when transport capacity is available.';
        _changed(operation);
      }
      return null;
    } finally {
      display.cancel();
    }
  }

  Future<AgentModeOutcome?> lookup(
    PendingAgentModeChange operation,
    AgentControlApi api,
    AgentControlSnapshot? state,
  ) async {
    if (!canLookup(operation, api, state)) return null;
    operation.lookupOwners++;
    operation.notice = 'Checking the exact original mode save…';
    final attempt = api.lookup(operation.request);
    unawaited(
      attempt.settled.then((_) {
        operation.lookupOwners--;
        _changed(operation);
      }),
    );
    _changed(operation);
    final display = Timer(displayDeadline, () {
      if (_registered(operation) && operation.lookupOwners > 0) {
        operation.uncertain = true;
        operation.notice =
            'Still waiting for save status. The earlier mode save remains unknown.';
        _changed(operation);
      }
    });
    try {
      final proof = await attempt.result;
      if (proof.receipt != null || proof.refused) {
        return _settle(
          operation,
          proof.receipt != null
              ? AgentModeOutcome.recorded
              : AgentModeOutcome.refused,
        );
      }
    } catch (_) {
      // Errors, missing records and standalone GETs cannot prove non-commit.
    } finally {
      display.cancel();
    }
    if (_registered(operation)) {
      operation.uncertain = true;
      operation.notice =
          'No settled result is confirmed. The earlier mode save remains unknown.';
      _changed(operation);
    }
    return null;
  }

  @override
  void dispose() {
    _disposed = true;
    super.dispose();
  }
}
