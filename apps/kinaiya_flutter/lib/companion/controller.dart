import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:uuid/uuid.dart';

import 'client.dart';
import 'models.dart';

class PendingCallAction {
  PendingCallAction(this.call, this.action) : key = const Uuid().v4();
  final CompanionCall call;
  final String action, key;
}

/// Owns exactly one foreground device grant and serial polling lifetime.
class CompanionController extends ChangeNotifier {
  CompanionController({CompanionClient Function(String)? clientFactory})
    : _factory = clientFactory ?? CompanionClient.new;

  final CompanionClient Function(String) _factory;
  CompanionClient? _client;
  CompanionDevice? device;
  List<CompanionCall> calls = const [];
  String? message;
  PendingCallAction? pending;
  bool pairing = false, refreshing = false, acting = false;
  bool _disposed = false, _foreground = true;
  int _generation = 0;
  Timer? _timer;

  bool get paired => device != null;
  bool _current(int generation) => !_disposed && generation == _generation;

  Future<void> pair(String host, String code, String name) async {
    if (_disposed) return;
    final generation = _retireConnection();
    if (!_current(generation)) return;
    pairing = true;
    _notify();
    if (!_current(generation)) return;
    try {
      final client = _factory(host);
      _client = client;
      final granted = await client.pair(code, name);
      if (!_current(generation)) {
        client.close();
        return;
      }
      device = granted;
      message = null;
    } catch (error) {
      if (_current(generation)) {
        _client?.close();
        _client = null;
        message = error is CompanionFailure
            ? error.message
            : 'Pairing failed. Ask the host for a new invitation and try again.';
      }
    } finally {
      if (_current(generation)) {
        pairing = false;
        _notify();
        if (paired) await refresh();
      }
    }
  }

  void _merge(Iterable<CompanionCall> incoming) {
    final byId = {for (final call in calls) call.callId: call};
    for (final call in incoming) {
      final previous = byId[call.callId];
      if (previous == null || previous.revision <= call.revision) {
        byId[call.callId] = call;
      }
    }
    calls = byId.values.toList()
      ..sort((left, right) => right.sequence.compareTo(left.sequence));
    final uncertain = pending;
    if (!acting &&
        uncertain != null &&
        calls.any(
          (call) =>
              call.callId == uncertain.call.callId &&
              call.revision > uncertain.call.revision,
        )) {
      // An authoritative newer snapshot reconciles uncertainty, without claiming
      // which actor caused the transition or resubmitting a different command.
      pending = null;
    }
  }

  Future<void> refresh() async {
    final client = _client, granted = device;
    if (_disposed ||
        !_foreground ||
        refreshing ||
        client == null ||
        granted == null) {
      return;
    }
    _timer?.cancel();
    final generation = _generation;
    refreshing = true;
    _notify();
    try {
      final result = await client.calls(granted);
      if (!_current(generation)) return;
      _merge(result);
      if (pending == null) message = null;
    } catch (error) {
      if (_current(generation)) _failure(error);
    } finally {
      if (_current(generation)) {
        refreshing = false;
        _notify();
        _schedule();
      }
    }
  }

  Future<void> action(CompanionCall call, String action) async {
    if (_disposed || acting || pending != null || !paired) return;
    pending = PendingCallAction(call, action);
    await retryAction();
  }

  Future<void> retryAction() async {
    final client = _client, granted = device, operation = pending;
    if (_disposed ||
        acting ||
        client == null ||
        granted == null ||
        operation == null) {
      return;
    }
    final generation = _generation;
    acting = true;
    _notify();
    var reconcile = false;
    try {
      final result = await client.act(
        granted,
        operation.call,
        operation.action,
        operation.key,
      );
      if (!_current(generation)) return;
      _merge([result]);
      pending = null;
      message = null;
    } catch (error) {
      if (_current(generation)) {
        _failure(error);
        if (error is CompanionFailure && error.status == 409) {
          pending = null;
          reconcile = true;
        }
      }
    } finally {
      if (_current(generation)) {
        acting = false;
        _notify();
        if (reconcile) await refresh();
      }
    }
  }

  void _failure(Object error) {
    if (error is CompanionFailure) {
      if (error.status == 401 || error.status == 403 || error.disconnect) {
        disconnect();
      }
      message = error.message;
    } else {
      message = 'The host returned an unsupported response. Try refreshing.';
    }
    _notify();
  }

  void setForeground(bool foreground) {
    if (_disposed) return;
    _foreground = foreground;
    _timer?.cancel();
    if (foreground) unawaited(refresh());
  }

  void _schedule() {
    if (!_disposed && _foreground && paired) {
      _timer = Timer(const Duration(seconds: 2), () => unawaited(refresh()));
    }
  }

  void disconnect() {
    _retireConnection();
    _notify();
  }

  int _retireConnection() {
    final generation = ++_generation;
    final previous = _client;
    _timer?.cancel();
    _client = null;
    device = null;
    calls = const [];
    pending = null;
    pairing = refreshing = acting = false;
    message = null;
    previous?.close();
    return generation;
  }

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  @override
  void dispose() {
    _disposed = true;
    disconnect();
    super.dispose();
  }
}
