/// RAM-only execution drafts and recovery, bound to private owner and runtime DB.
library;

import 'dart:async';
import 'package:flutter/foundation.dart';
import '../api/agent_runtime_client.dart';
import '../api/agent_runtime_settings.dart';
import 'options_runtime_editor.dart';

enum AgentRuntimeOutcome { recorded, refused }

/// Uses the native codepoint bound without changing the shared form widget.
class AgentRuntimeDraft extends OptionsRuntimeDraft {
  AgentRuntimeDraft(super.base);

  @override
  Map<String, Object?>? get settings {
    final maxSteps = int.tryParse(steps), cap = int.tryParse(timeout);
    if (maxSteps == null ||
        maxSteps < 1 ||
        maxSteps > 2000 ||
        cap == null ||
        cap < 1 ||
        cap > 120) {
      return null;
    }
    try {
      return {
        'file_writes': fileWrites,
        'shell_commands': shellCommands,
        'tool_network': toolNetwork,
        'max_steps': maxSteps,
        'shell_timeout_s': cap,
        'working_directory': canonicalRuntimeDirectory(directory),
      };
    } on FormatException {
      return null;
    }
  }
}

class AgentRuntimeWriteScope {
  AgentRuntimeWriteScope._(this.owner, this.backendId, this.snapshot)
    : draft = AgentRuntimeDraft(snapshot);
  final AgentRuntimeApi owner;
  final String backendId;
  AgentRuntimeSnapshot snapshot;
  AgentRuntimeDraft draft;
  PendingAgentRuntimeChange? pending;
  String? notice;
}

class PendingAgentRuntimeChange {
  PendingAgentRuntimeChange._(this.scope, this.request) : draft = scope.draft;
  final AgentRuntimeWriteScope scope;
  final AgentRuntimeChange request;
  final AgentRuntimeDraft draft;
  int mutationOwners = 0, lookupOwners = 0;
  bool uncertain = false, timedOut = false, complete = false;
  String notice = 'Saving Agent execution settings…';
  bool get sending => mutationOwners > 0;
}

final agentRuntimeWrites = AgentRuntimeWrites();

class AgentRuntimeWrites extends ChangeNotifier {
  AgentRuntimeWrites({this.displayDeadline = const Duration(seconds: 8)});
  final Duration displayDeadline;
  final List<AgentRuntimeWriteScope> _scopes = [];

  /// Clean idle drafts may be replaced; unresolved work and dirty drafts cannot.
  /// At most sixteen retained scopes also bounds draft-only RAM retention.
  AgentRuntimeWriteScope? open(
    AgentRuntimeApi api,
    AgentRuntimeSnapshot snapshot,
  ) {
    final authority = snapshot.writeAuthority;
    if (authority == null) return null;
    for (final scope in _scopes) {
      if (api.sameOwner(scope.owner) &&
          scope.backendId == authority.backendId) {
        scope.snapshot = snapshot;
        if (scope.pending == null && !scope.draft.dirty) {
          scope.draft = AgentRuntimeDraft(snapshot);
        }
        return scope;
      }
    }
    if (_scopes.length >= 16) {
      final idle = _scopes.indexWhere(
        (scope) => scope.pending == null && !scope.draft.dirty,
      );
      if (idle < 0) return null;
      _scopes.removeAt(idle);
    }
    final scope = AgentRuntimeWriteScope._(api, authority.backendId, snapshot);
    _scopes.add(scope);
    return scope;
  }

  void reset(AgentRuntimeWriteScope scope) {
    if (!_scopes.contains(scope) || scope.pending != null) return;
    scope.draft = AgentRuntimeDraft(scope.snapshot);
    scope.notice = null;
    notifyListeners();
  }

  bool _registered(PendingAgentRuntimeChange operation) =>
      !operation.complete &&
      _scopes.contains(operation.scope) &&
      identical(operation.scope.pending, operation);

  bool _scope(
    PendingAgentRuntimeChange operation,
    AgentRuntimeApi api,
    AgentRuntimeAuthority? authority,
  ) =>
      authority != null &&
      api.sameOwner(operation.scope.owner) &&
      authority.backendId == operation.request.backendId;

  PendingAgentRuntimeChange? begin(
    AgentRuntimeWriteScope scope,
    AgentRuntimeApi api,
    AgentRuntimeChange request,
  ) {
    final snapshot = scope.snapshot, authority = snapshot.writeAuthority;
    if (!_scopes.contains(scope) ||
        authority == null ||
        !authority.writable ||
        !api.sameOwner(scope.owner) ||
        scope.backendId != request.backendId ||
        authority.instanceId != request.instanceId ||
        snapshot.revision != request.revision ||
        snapshot.stateToken != request.body['expected_state'] ||
        snapshot.busy ||
        !api.hasCapacity ||
        scope.pending != null ||
        _scopes.where((value) => value.pending != null).length >= 16) {
      return null;
    }
    final operation = PendingAgentRuntimeChange._(scope, request);
    scope.pending = operation;
    scope.notice = null;
    return operation;
  }

  bool canRetry(
    PendingAgentRuntimeChange operation,
    AgentRuntimeApi api,
    AgentRuntimeAuthority? authority,
  ) =>
      _registered(operation) &&
      _scope(operation, api, authority) &&
      authority!.writable &&
      authority.instanceId == operation.request.instanceId &&
      api.hasCapacity &&
      operation.mutationOwners < 2 &&
      (operation.uncertain ||
          operation.timedOut ||
          operation.mutationOwners == 0);

  bool canLookup(
    PendingAgentRuntimeChange operation,
    AgentRuntimeApi api,
    AgentRuntimeAuthority? authority,
  ) =>
      _registered(operation) &&
      _scope(operation, api, authority) &&
      api.hasCapacity &&
      operation.lookupOwners == 0;

  AgentRuntimeOutcome? _settle(
    PendingAgentRuntimeChange operation,
    AgentRuntimeOutcome outcome,
  ) {
    if (!_registered(operation)) return null;
    operation.complete = true;
    final scope = operation.scope;
    scope.pending = null;
    if (outcome == AgentRuntimeOutcome.recorded) {
      // A receipt is historical. Build a fresh draft only from the next active
      // snapshot, never from the receipt's requested YOLO or settings.
      if (identical(scope.draft, operation.draft)) {
        scope.draft = AgentRuntimeDraft(scope.snapshot);
      }
      scope.notice =
          'Agent settings save confirmed. Review the refreshed active values.';
    } else {
      scope.notice =
          'The earlier save was not applied and is permanently fenced. '
          'Your draft is retained. Review active values before another save.';
    }
    notifyListeners();
    return outcome;
  }

  void _changed(PendingAgentRuntimeChange operation) {
    if (_registered(operation)) notifyListeners();
  }

  Future<AgentRuntimeOutcome?> retry(
    PendingAgentRuntimeChange operation,
    AgentRuntimeApi api,
    AgentRuntimeAuthority? authority,
  ) async {
    if (!canRetry(operation, api, authority)) return null;
    operation.mutationOwners++;
    operation.timedOut = false;
    operation.notice = 'Saving Agent execution settings…';
    final attempt = api.change(operation.request);
    unawaited(
      attempt.settled.then((_) {
        operation.mutationOwners--;
        _changed(operation);
      }),
    );
    _changed(operation);
    final display = Timer(displayDeadline, () {
      if (_registered(operation) && operation.sending) {
        operation.timedOut = true;
        operation.uncertain = true;
        operation.notice =
            'Still waiting for the host. The settings may have been saved.';
        _changed(operation);
      }
    });
    try {
      await attempt.result;
      return _settle(operation, AgentRuntimeOutcome.recorded);
    } catch (_) {
      if (_registered(operation)) {
        operation.uncertain = operation.uncertain || attempt.dispatched;
        operation.notice = operation.uncertain
            ? 'Save result is unconfirmed. Check the exact saved result before another execution change.'
            : 'The request was not sent. Retry the same save when transport capacity is available.';
        _changed(operation);
      }
      return null;
    } finally {
      display.cancel();
    }
  }

  Future<AgentRuntimeOutcome?> lookup(
    PendingAgentRuntimeChange operation,
    AgentRuntimeApi api,
    AgentRuntimeAuthority? authority,
  ) async {
    if (!canLookup(operation, api, authority)) return null;
    operation.lookupOwners++;
    operation.notice = 'Checking the exact execution settings save…';
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
            'Still waiting for save status. The earlier save remains unknown.';
        _changed(operation);
      }
    });
    try {
      final proof = await attempt.result;
      if (proof.receipt != null || proof.refused) {
        return _settle(
          operation,
          proof.receipt != null
              ? AgentRuntimeOutcome.recorded
              : AgentRuntimeOutcome.refused,
        );
      }
    } catch (_) {
      // Missing, refused, foreign and malformed reads are not non-commit proof.
    } finally {
      display.cancel();
    }
    if (_registered(operation)) {
      operation.uncertain = true;
      operation.notice =
          'No settled result is confirmed. The earlier save remains unknown.';
      _changed(operation);
    }
    return null;
  }
}
