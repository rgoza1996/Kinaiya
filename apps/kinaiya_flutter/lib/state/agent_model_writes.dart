/// RAM-only model recovery. Native serialized proof alone settles uncertainty.
library;

import 'dart:async';
import 'package:flutter/foundation.dart';
import '../api/agent_model_client.dart';
import '../api/agent_model_settings.dart';

enum AgentModelOutcome { recorded, refused }

class PendingAgentModelChange {
  PendingAgentModelChange._(this.owner, this.request);
  final AgentModelApi owner;
  final AgentModelChange request;
  int mutationOwners = 0, lookupOwners = 0;
  bool uncertain = false, timedOut = false, complete = false;
  String notice = 'Saving model settings…';
  bool get sending => mutationOwners > 0;
}

final agentModelWrites = AgentModelWrites();

class AgentModelWrites extends ChangeNotifier {
  AgentModelWrites({this.displayDeadline = const Duration(seconds: 8)});
  final Duration displayDeadline;
  final List<PendingAgentModelChange> _pending = [];

  bool _registered(PendingAgentModelChange operation) =>
      !operation.complete &&
      _pending.any((value) => identical(value, operation));

  bool _scope(
    PendingAgentModelChange operation,
    AgentModelApi api,
    AgentModelAuthority? authority,
  ) =>
      authority != null &&
      api.sameOwner(operation.owner) &&
      authority.backendId == operation.request.backendId;

  PendingAgentModelChange? visible(
    AgentModelApi api,
    AgentModelAuthority? authority,
  ) {
    for (final operation in _pending) {
      if (_scope(operation, api, authority)) return operation;
    }
    return null;
  }

  PendingAgentModelChange? begin(
    AgentModelApi api,
    AgentModelAuthority authority,
    AgentModelChange request,
  ) {
    if (!authority.writable ||
        authority.backendId != request.backendId ||
        authority.instanceId != request.instanceId ||
        !api.hasCapacity ||
        _pending.length >= 16 ||
        visible(api, authority) != null) {
      return null;
    }
    final operation = PendingAgentModelChange._(api, request);
    _pending.add(operation);
    return operation;
  }

  bool canRetry(
    PendingAgentModelChange operation,
    AgentModelApi api,
    AgentModelAuthority? authority,
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
    PendingAgentModelChange operation,
    AgentModelApi api,
    AgentModelAuthority? authority,
  ) =>
      _registered(operation) &&
      _scope(operation, api, authority) &&
      api.hasCapacity &&
      operation.lookupOwners == 0;

  bool _settle(PendingAgentModelChange operation) {
    if (!_registered(operation)) return false;
    operation.complete = true;
    _pending.removeWhere((value) => identical(value, operation));
    notifyListeners();
    return true;
  }

  void _changed(PendingAgentModelChange operation) {
    if (_registered(operation)) notifyListeners();
  }

  Future<AgentModelOutcome?> retry(
    PendingAgentModelChange operation,
    AgentModelApi api,
    AgentModelAuthority? authority,
  ) async {
    if (!canRetry(operation, api, authority)) return null;
    operation.mutationOwners++;
    operation.timedOut = false;
    operation.notice = 'Saving model settings…';
    final attempt = api.change(operation.request);
    // An attempt slot ends only with genuine transport settlement, including
    // after this record is confirmed and removed from the visible registry.
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
            'Still waiting for the host. The change may have been saved.';
        _changed(operation);
      }
    });
    try {
      await attempt.result;
      return _settle(operation) ? AgentModelOutcome.recorded : null;
    } catch (_) {
      if (_registered(operation)) {
        operation.uncertain = operation.uncertain || attempt.dispatched;
        operation.notice = operation.uncertain
            ? 'Save result is unconfirmed. Check the exact saved result before another change.'
            : 'The request was not sent. Retry the same save when transport capacity is available.';
        _changed(operation);
      }
      return null;
    } finally {
      display.cancel();
    }
  }

  Future<AgentModelOutcome?> lookup(
    PendingAgentModelChange operation,
    AgentModelApi api,
    AgentModelAuthority? authority,
  ) async {
    if (!canLookup(operation, api, authority)) return null;
    operation.lookupOwners++;
    operation.notice = 'Checking the exact saved result…';
    final attempt = api.lookup(operation.request);
    unawaited(
      attempt.settled.then((_) {
        operation.lookupOwners--;
        _changed(operation);
      }),
    );
    _changed(operation);
    try {
      final proof = await attempt.result;
      if (proof.receipt != null || proof.refused) {
        return _settle(operation)
            ? (proof.receipt != null
                  ? AgentModelOutcome.recorded
                  : AgentModelOutcome.refused)
            : null;
      }
    } catch (_) {
      // A refusal/missing/malformed/foreign read is not proof of non-commit.
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
