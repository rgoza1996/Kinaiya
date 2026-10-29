/// Owner-only Converse memory dialog (ADR 0052).
///
/// Lists and inspects saved notes plus explicit saved-access and selected-use
/// controls for the current Converse session/thread/epoch. Opening this
/// dialog never creates a session, captures a note, configures a service,
/// recalls context, measures a prompt or invokes inference. Inference flags
/// never gate these owner controls; each service enforces owner authentication.
///
/// Lifetime rules enforced here:
/// - Every asynchronous response and action rechecks the captured client
///   identity, [AppState.connectionGeneration] and full binding before
///   touching widget state. Late or superseded completions are dropped and
///   never clear newer busy/action ownership.
/// - Scope or reconnect invalidation discards local snapshots and selection
///   without writes and without replaying anything.
/// - Uncertain (unconfirmed) writes keep a module-level latch keyed by
///   canonical service endpoint plus binding, surviving close/reopen and
///   reconnects to the same host. The latch settles only on known completion
///   of its own request or a strictly advanced observed revision/generation
///   (or a confirmed different runtime identity). Equal refreshes never
///   settle it; no automatic resubmission ever occurs.
library;

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../api/client.dart';
import '../api/memory_client.dart';
import '../api/memory_models.dart';
import '../state/app_state.dart';

/// Maximum unresolved owner scopes kept in RAM. New writes are refused at
/// this bound instead of evicting another scope's latch.
const int memoryUnresolvedScopeBound = 32;

/// Largest selectable note set for one explicit use action.
const int memoryMaxSelection = 8;

/// One dispatched-but-unresolved owner write. [uncertain] is true once the
/// transport reports the request may still arrive (timeout/5xx after
/// dispatch or late-send cleanup that cannot confirm the outcome).
class _WriteOp {
  _WriteOp({
    required this.expectedRevision,
    required this.expectedRuntimeId,
    required this.expectedGeneration,
  });

  final Object token = Object();
  bool uncertain = false;
  final int? expectedRevision;
  final String? expectedRuntimeId;
  final int? expectedGeneration;
}

/// Unresolved owner writes, keyed by canonical endpoint plus binding. This is
/// RAM for the running owner app only; nothing here is persisted.
final Map<String, List<_WriteOp>> _unresolvedOps = {};

String _canonicalEndpoint(String raw) {
  final uri = Uri.tryParse(raw.trim());
  if (uri == null || !uri.hasScheme || !uri.hasAuthority) return raw.trim();
  final scheme = uri.scheme.toLowerCase();
  final defaultPort = scheme == 'https' ? 443 : 80;
  final port = uri.port == defaultPort ? '' : ':${uri.port}';
  final host = uri.host.contains(':')
      ? '[${uri.host.toLowerCase()}]'
      : uri.host.toLowerCase();
  final path = uri.normalizePath().path.replaceAll(RegExp(r'/+$'), '');
  return '$scheme://$host$port$path';
}

String _scopeKey(String endpoint, MemoryBinding binding) =>
    '${_canonicalEndpoint(endpoint)}\x00'
    '${binding.sessionId}\x00${binding.threadId}\x00${binding.epoch}';

int _unresolvedScopeCount() {
  var count = 0;
  for (final ops in _unresolvedOps.values) {
    if (ops.isNotEmpty) count++;
  }
  return count;
}

bool _scopeHasUnresolved(String key) =>
    key.isNotEmpty && (_unresolvedOps[key]?.isNotEmpty ?? false);

bool _scopeHasUncertain(String key) =>
    key.isNotEmpty && (_unresolvedOps[key]?.any((op) => op.uncertain) ?? false);

/// Records a latch before dispatch. Returns null when the bounded store is
/// full for a new scope, in which case the caller refuses the write.
_WriteOp? _recordOp(
  String key, {
  int? expectedRevision,
  String? expectedRuntimeId,
  int? expectedGeneration,
}) {
  if (key.isEmpty) return null;
  if (!_unresolvedOps.containsKey(key) &&
      _unresolvedScopeCount() >= memoryUnresolvedScopeBound) {
    return null;
  }
  if ((_unresolvedOps[key]?.length ?? 0) >= 8) return null;
  final op = _WriteOp(
    expectedRevision: expectedRevision,
    expectedRuntimeId: expectedRuntimeId,
    expectedGeneration: expectedGeneration,
  );
  (_unresolvedOps[key] ??= <_WriteOp>[]).add(op);
  return op;
}

void _removeOp(String key, Object token) {
  final ops = _unresolvedOps[key];
  if (ops == null) return;
  ops.removeWhere((op) => op.token == token);
  if (ops.isEmpty) _unresolvedOps.remove(key);
}

/// Settles namespace latches only on strictly advanced observed revisions.
/// Equal refreshes never settle an uncertain write.
void _settleNamespaceOps(String key, int observedRevision) {
  final ops = _unresolvedOps[key];
  if (ops == null) return;
  ops.removeWhere(
    (op) =>
        op.expectedRevision != null && observedRevision > op.expectedRevision!,
  );
  if (ops.isEmpty) _unresolvedOps.remove(key);
}

/// Settles runtime latches on a strictly advanced generation on the same
/// runtime. Only a current accepted read may establish runtime replacement:
/// a late acknowledgment from a retired runtime cannot fence newer work.
void _settleRuntimeOps(
  String key,
  String observedRuntimeId,
  int observedGeneration, {
  bool observeRuntimeReplacement = false,
}) {
  final ops = _unresolvedOps[key];
  if (ops == null) return;
  ops.removeWhere((op) {
    if (op.expectedRuntimeId == null || op.expectedGeneration == null) {
      return false;
    }
    if (observedRuntimeId != op.expectedRuntimeId) {
      return observeRuntimeReplacement;
    }
    return observedGeneration > op.expectedGeneration!;
  });
  if (ops.isEmpty) _unresolvedOps.remove(key);
}

/// Unobtrusive entry beside Voice options. Always enabled: opening the dialog
/// performs only owner reads and never requires history, inference
/// permission, or capture availability.
class ConverseMemoryButton extends StatelessWidget {
  const ConverseMemoryButton({super.key, required this.state});

  final AppState state;

  @override
  Widget build(BuildContext context) => Tooltip(
    message: 'Saved Converse notes for this conversation',
    child: TextButton.icon(
      key: const ValueKey('converse-memory'),
      icon: const Icon(Icons.note_alt_outlined),
      label: const Text('Memory', maxLines: 1, overflow: TextOverflow.ellipsis),
      onPressed: () => showConverseMemoryDialog(context: context, state: state),
    ),
  );
}

/// Opens the owner memory dialog. Presentation only: opening or closing it
/// never touches capture, drafts, audio, sessions, or Agent navigation.
Future<void> showConverseMemoryDialog({
  required BuildContext context,
  required AppState state,
}) => showDialog<void>(
  context: context,
  builder: (_) => ConverseMemoryDialog(state: state),
);

class ConverseMemoryDialog extends StatefulWidget {
  const ConverseMemoryDialog({super.key, required this.state});

  final AppState state;

  @override
  State<ConverseMemoryDialog> createState() => _ConverseMemoryDialogState();
}

class _ConverseMemoryDialogState extends State<ConverseMemoryDialog> {
  KinaiyaClient? _client;
  int _generation = 0;
  MemoryBinding? _binding;

  MemoryNamespaceState? _ns;
  String? _nsError;
  MemoryCatalog? _catalog;
  String? _catalogError;
  MemoryUseState? _use;
  MemoryUseState? _lastUseForStop;
  String? _useError;

  int _readToken = 0;
  int _pendingReads = 0;

  final Set<String> _selected = {};

  String? _detailFor;
  MemoryInspection? _detail;
  String? _detailError;
  bool _detailBusy = false;
  int _detailToken = 0;

  bool _nsBusy = false;
  int _nsAction = 0;
  bool _useBusy = false;
  int _useAction = 0;

  String? _notice;
  bool _refreshRequired = false;

  @override
  void initState() {
    super.initState();
    _captureScope();
    widget.state.addListener(_onExternal);
    _loadAll();
  }

  @override
  void dispose() {
    widget.state.removeListener(_onExternal);
    super.dispose();
  }

  void _captureScope() {
    final live = widget.state;
    _client = live.client;
    _generation = live.connectionGeneration;
    final session = live.converseSession;
    MemoryBinding? binding;
    if (session != null) {
      try {
        binding = MemoryBinding(
          session.sessionId,
          session.threadId,
          session.epoch,
        );
      } on FormatException {
        binding = null;
      }
    }
    _binding = binding;
  }

  bool _scopeMatches() {
    final live = widget.state;
    if (!identical(_client, live.client)) return false;
    if (_generation != live.connectionGeneration) return false;
    final session = live.converseSession;
    if (session == null) return _binding == null;
    if (_binding == null) return false;
    return _binding!.sessionId == session.sessionId &&
        _binding!.threadId == session.threadId &&
        _binding!.epoch == session.epoch;
  }

  String get _namespaceKey {
    final client = _client;
    final binding = _binding;
    if (client == null || binding == null) return '';
    return _scopeKey(client.sessionBase, binding);
  }

  String get _runtimeKey {
    final client = _client;
    final binding = _binding;
    if (client == null || binding == null) return '';
    return _scopeKey(client.converseBase, binding);
  }

  /// External AppState change: discard invalid scope state and selection
  /// without writes. A manual refresh adopts the new scope explicitly.
  void _onExternal() {
    if (!mounted || _scopeMatches()) return;
    _captureStale();
  }

  void _captureStale() {
    setState(() {
      _invalidateReads();
      _nsAction++;
      _useAction++;
      _ns = null;
      _nsError = null;
      _catalog = null;
      _catalogError = null;
      _use = null;
      _lastUseForStop = null;
      _useError = null;
      _selected.clear();
      _detail = null;
      _detailError = null;
      _detailFor = null;
      _detailBusy = false;
      _nsBusy = false;
      _useBusy = false;
      _pendingReads = 0;
      _refreshRequired = false;
      _notice =
          'The conversation or connection changed. '
          'Choose Refresh to load the current scope. No change was sent.';
    });
  }

  bool get _ownsScope =>
      mounted && _scopeMatches() && _client != null && _binding != null;

  void _invalidateReads() {
    _readToken++;
    _detailToken++;
    _pendingReads = 0;
    _detailBusy = false;
  }

  void _refresh() {
    final lastUse = _ownsScope ? (_use ?? _lastUseForStop) : null;
    _captureScope();
    setState(() {
      _invalidateReads();
      _nsAction++;
      _useAction++;
      _ns = null;
      _nsError = null;
      _catalog = null;
      _catalogError = null;
      _use = null;
      _lastUseForStop = lastUse;
      _useError = null;
      _selected.clear();
      _detail = null;
      _detailError = null;
      _detailFor = null;
      _detailBusy = false;
      _nsBusy = false;
      _useBusy = false;
      _pendingReads = 0;
      _refreshRequired = false;
      _notice = null;
    });
    _loadAll();
  }

  void _loadAll() {
    final client = _client;
    final binding = _binding;
    if (client == null || binding == null) {
      // No request is sent without a configured owner client and scope.
      setState(() {
        _pendingReads = 0;
        _refreshRequired = false;
      });
      return;
    }
    final token = ++_readToken;
    setState(() {
      _pendingReads = 3;
      _nsError = null;
      _catalogError = null;
      _useError = null;
      _refreshRequired = false;
    });
    unawaited(_readNamespace(client, binding, token));
    unawaited(_readCatalog(client, binding, token));
    unawaited(_readUse(client, binding, token));
  }

  bool _ownsRead(int token) =>
      mounted && token == _readToken && _scopeMatches();

  void _finishRead() {
    _pendingReads = _pendingReads > 0 ? _pendingReads - 1 : 0;
  }

  Future<void> _readNamespace(
    KinaiyaClient client,
    MemoryBinding binding,
    int token,
  ) async {
    MemoryNamespaceState? result;
    MemoryFailure? failure;
    try {
      result = await client.memory.state(binding);
    } on MemoryFailure catch (e) {
      failure = e;
    } catch (_) {
      failure = const MemoryFailure('Memory request failed');
    }
    if (result != null && result.binding != binding) {
      result = null;
      failure = const MemoryFailure('Invalid memory response');
    }
    if (!_ownsRead(token)) return;
    setState(() {
      _finishRead();
      if (result != null) {
        if (_ns != null && result.revision < _ns!.revision) return;
        _ns = result;
        _nsError = null;
        _settleNamespaceOps(_namespaceKey, result.revision);
      } else {
        _nsError = _readProblem(failure!);
      }
    });
  }

  Future<void> _readCatalog(
    KinaiyaClient client,
    MemoryBinding binding,
    int token,
  ) async {
    MemoryCatalog? result;
    MemoryFailure? failure;
    try {
      result = await client.memory.catalog(binding);
    } on MemoryFailure catch (e) {
      failure = e;
    } catch (_) {
      failure = const MemoryFailure('Memory request failed');
    }
    if (result != null && result.binding != binding) {
      result = null;
      failure = const MemoryFailure('Invalid memory response');
    }
    if (!_ownsRead(token)) return;
    setState(() {
      _finishRead();
      if (result != null) {
        if (_ns != null && result.state.revision < _ns!.revision) {
          _catalogError =
              'Saved notes changed. Refresh before selecting notes.';
          return;
        }
        // A failed catalog never hides a positive runtime snapshot: the two
        // reads apply independently, and Stop stays available from [_use].
        _catalog = result;
        _catalogError = null;
        _ns = result.state;
        _settleNamespaceOps(_namespaceKey, result.state.revision);
        final active = <String>{
          for (final entry in result.entries)
            if (entry.state == 'active') entry.nodeId,
        };
        _selected.removeWhere((id) => !active.contains(id));
        while (_selected.length > memoryMaxSelection) {
          _selected.remove(_selected.last);
        }
      } else {
        _catalogError = _readProblem(failure!);
      }
    });
  }

  Future<void> _readUse(
    KinaiyaClient client,
    MemoryBinding binding,
    int token,
  ) async {
    MemoryUseState? result;
    MemoryFailure? failure;
    try {
      result = await client.memory.useState(binding);
    } on MemoryFailure catch (e) {
      failure = e;
    } catch (_) {
      failure = const MemoryFailure('Memory request failed');
    }
    if (result != null && result.binding != binding) {
      result = null;
      failure = const MemoryFailure('Invalid memory response');
    }
    if (!_ownsRead(token)) return;
    setState(() {
      _finishRead();
      if (result != null) {
        final previous = _use ?? _lastUseForStop;
        _use = result;
        _lastUseForStop = result;
        _useError = null;
        _settleRuntimeOps(
          _runtimeKey,
          result.runtimeId,
          result.useGeneration,
          observeRuntimeReplacement: true,
        );
        if (previous != null && previous.runtimeId != result.runtimeId) {
          // A runtime restart clears local selection and starts use off.
          _selected.clear();
          _notice =
              'The conversation service restarted or changed. '
              'Check its current use, then tick notes again to use them.';
        }
      } else {
        _useError = _readProblem(failure!);
      }
    });
  }

  static String _readProblem(MemoryFailure failure) {
    switch (failure.status) {
      case 404:
        return 'Converse memory is not available on this backend. '
            'The conversation still works without it.';
      case 401:
      case 403:
        return 'Owner access was refused for saved notes. '
            'Sign in as the owner, then choose Refresh.';
      case 409:
        return 'Another change landed first. '
            'Choose Refresh to see the current values.';
    }
    final message = failure.message;
    if (message.contains('timed out')) {
      return 'The memory request timed out. Nothing on this screen '
          'was changed. Choose Refresh to check the current values.';
    }
    if (failure.status != null) {
      return 'The memory service refused the request '
          '(status ${failure.status}). Choose Refresh to try again.';
    }
    return 'The memory request failed. Check the connection, '
        'then choose Refresh to try again.';
  }

  void _inspect(String nodeId) {
    final client = _client;
    final binding = _binding;
    if (client == null || binding == null || !_scopeMatches()) {
      return;
    }
    final token = ++_detailToken;
    setState(() {
      _detailFor = nodeId;
      _detail = null;
      _detailError = null;
      _detailBusy = true;
    });
    unawaited(() async {
      MemoryInspection? result;
      MemoryFailure? failure;
      try {
        result = await client.memory.inspect(binding, nodeId);
      } on MemoryFailure catch (e) {
        failure = e;
      } catch (_) {
        failure = const MemoryFailure('Memory request failed');
      }
      // The transport already verifies binding and node ID; fence late or
      // superseded inspections here as well.
      if (!mounted || token != _detailToken || !_scopeMatches()) return;
      if (result != null &&
          (result.binding != binding || result.node.nodeId != nodeId)) {
        result = null;
        failure = const MemoryFailure('Invalid memory response');
      }
      setState(() {
        _detailBusy = false;
        if (result != null) {
          _detail = result;
          _detailError = null;
        } else {
          _detailError = _readProblem(failure!);
        }
      });
    }());
  }

  void _configureNamespace(bool enabled) {
    final client = _client;
    final binding = _binding;
    final snapshot = _ns;
    if (client == null ||
        binding == null ||
        snapshot == null ||
        !_ownsScope ||
        _nsBusy ||
        _refreshRequired) {
      return;
    }
    final key = _namespaceKey;
    if (_scopeHasUnresolved(key)) {
      setState(() {
        _notice =
            'A saved-access change is still unresolved. Refresh to check '
            'what the service has now. Unchanged values do not prove it '
            'failed, so further saved-access changes stay paused.';
      });
      return;
    }
    final op = _recordOp(key, expectedRevision: snapshot.revision);
    if (op == null) {
      setState(() {
        _notice =
            'Earlier saved-access requests are still unresolved. '
            'Refresh to check their current state before changing access.';
      });
      return;
    }
    final revision = snapshot.revision;
    final token = ++_nsAction;
    setState(() {
      _invalidateReads();
      _nsBusy = true;
      _notice = null;
      _refreshRequired = false;
    });
    unawaited(() async {
      MemoryNamespaceState? result;
      MemoryFailure? failure;
      try {
        result = await client.memory.configure(
          binding,
          expectedRevision: revision,
          enabled: enabled,
        );
      } on MemoryFailure catch (e) {
        failure = e;
      } catch (_) {
        failure = const MemoryFailure('Memory request failed');
      }
      // Settle the module latch independently of widget state so closing
      // the dialog cannot erase it; token-checked below for UI updates.
      if (failure != null && failure.unconfirmed) {
        op.uncertain = true;
      } else {
        _removeOp(key, op.token);
        if (result != null) _settleNamespaceOps(key, result.revision);
      }
      if (!mounted || !_scopeMatches() || token != _nsAction) return;
      setState(() {
        _nsBusy = false;
        if (result != null) {
          _ns = result;
          _catalog = null;
          _selected.clear();
          _nsError = null;
          _notice = enabled
              ? 'Saved access is on. Refresh to load the current notes.'
              : 'Saved access is off. Your saved notes are kept. Refresh to inspect them.';
        } else if (failure!.status == 409) {
          // CAS conflict clears the actionable snapshot: manual refresh
          // is required before any further change.
          _ns = null;
          _catalog = null;
          _selected.clear();
          _nsError =
              'Another change landed first. Choose Refresh to see the '
              'current values, then try again if still needed.';
          _refreshRequired = true;
        } else if (failure.unconfirmed) {
          _notice =
              'This change may still arrive. Refresh to check what the '
              'service has now. Unchanged values do not prove it failed, '
              'so further saved-access changes stay paused.';
        } else {
          _nsError = _readProblem(failure);
        }
      });
    }());
  }

  void _configureUse({required bool enable}) {
    final client = _client;
    final binding = _binding;
    final snapshot = enable ? _use : (_use ?? _lastUseForStop);
    if (client == null ||
        binding == null ||
        snapshot == null ||
        !_ownsScope ||
        _useBusy ||
        (enable && _refreshRequired)) {
      return;
    }
    final runtimeKey = _runtimeKey;
    final namespaceKey = _namespaceKey;
    if (enable) {
      if (!_canUseSelected) return;
      // A replacement enable stays blocked while either scope is uncertain.
      // Explicit Stop below may still run while runtime use is uncertain.
      if (_scopeHasUnresolved(runtimeKey) ||
          _scopeHasUnresolved(namespaceKey)) {
        setState(() {
          _notice =
              'A memory change is still unresolved. Refresh to check '
              'what the service has now. Unchanged values do not prove it '
              'failed, so starting use stays paused. You can still stop use.';
        });
        return;
      }
    }
    List<String> selectors = const [];
    if (enable) {
      final active = <String>{
        for (final entry in _catalog?.entries ?? const <MemorySummary>[])
          if (entry.state == 'active') entry.nodeId,
      };
      selectors = _selected.where(active.contains).toList()..sort();
      final namespace = _ns;
      if (namespace == null ||
          !namespace.enabled ||
          namespace.status != 'ready' ||
          _catalog == null ||
          selectors.isEmpty ||
          selectors.length > memoryMaxSelection) {
        return;
      }
    }
    final op = _recordOp(
      runtimeKey,
      expectedRuntimeId: snapshot.runtimeId,
      expectedGeneration: snapshot.useGeneration,
    );
    if (op == null) {
      setState(() {
        _notice =
            'Earlier use requests are still unresolved. '
            'Refresh to check their current state before changing use.';
      });
      return;
    }
    final runtimeId = snapshot.runtimeId;
    final generation = snapshot.useGeneration;
    final token = ++_useAction;
    setState(() {
      _invalidateReads();
      _useBusy = true;
      _notice = null;
      _refreshRequired = false;
    });
    unawaited(() async {
      MemoryUseState? result;
      MemoryFailure? failure;
      try {
        result = await client.memory.configureUse(
          binding,
          expectedRuntimeId: runtimeId,
          expectedGeneration: generation,
          enabled: enable,
          selectorIds: selectors,
        );
      } on MemoryFailure catch (e) {
        failure = e;
      } catch (_) {
        failure = const MemoryFailure('Memory request failed');
      }
      // Settle independently of widget state; an equal-generation off never
      // fences an earlier uncertain enable, and a namespace no-op off never
      // fences a delayed enable either: only a known completion, a
      // strictly advanced generation on the same runtime settles another
      // runtime latch. This acknowledgment cannot establish replacement:
      // it may arrive after a newer runtime's write was already admitted.
      if (failure != null && failure.unconfirmed) {
        op.uncertain = true;
      } else {
        _removeOp(runtimeKey, op.token);
        if (result != null) {
          _settleRuntimeOps(runtimeKey, result.runtimeId, result.useGeneration);
        }
      }
      if (!mounted || !_scopeMatches() || token != _useAction) return;
      setState(() {
        _useBusy = false;
        if (result != null) {
          final previous = _use;
          _use = result;
          _lastUseForStop = result;
          _useError = null;
          if (previous != null && previous.runtimeId != result.runtimeId) {
            _selected.clear();
          }
          _notice = enable
              ? 'Using ${result.selectorIds.length} note'
                    '${result.selectorIds.length == 1 ? '' : 's'}. '
                    'This means configured, not ready.'
              : 'Use is off for this conversation.';
        } else if (failure!.status == 409) {
          _use = null;
          _lastUseForStop = null;
          _useError =
              'Another change landed first. Choose Refresh to see the '
              'current values, then try again if still needed.';
          _refreshRequired = true;
        } else if (failure.unconfirmed) {
          _notice =
              'This change may still arrive. Refresh to check what the '
              'service has now. Unchanged values do not prove it failed.';
        } else {
          _useError = _readProblem(failure);
        }
      });
    }());
  }

  bool get _canUseSelected {
    final namespace = _ns;
    final catalog = _catalog;
    final use = _use;
    if (namespace == null ||
        catalog == null ||
        use == null ||
        _pendingReads > 0 ||
        namespace.revision != catalog.state.revision ||
        namespace.enabled != catalog.state.enabled ||
        namespace.status != catalog.state.status ||
        namespace.nodeCount != catalog.state.nodeCount ||
        !namespace.enabled ||
        namespace.status != 'ready' ||
        _nsBusy ||
        _useBusy ||
        _refreshRequired ||
        !_ownsScope) {
      return false;
    }
    if (_scopeHasUnresolved(_runtimeKey) ||
        _scopeHasUnresolved(_namespaceKey)) {
      return false;
    }
    if (_selected.isEmpty || _selected.length > memoryMaxSelection) {
      return false;
    }
    final active = <String>{
      for (final entry in catalog.entries)
        if (entry.state == 'active') entry.nodeId,
    };
    return _selected.every(active.contains);
  }

  bool get _canStop =>
      (_use ?? _lastUseForStop) != null && !_useBusy && _ownsScope;

  void _toggleSelected(String nodeId, bool add) {
    if (!_ownsScope) return;
    setState(() {
      if (add) {
        if (_selected.length < memoryMaxSelection) _selected.add(nodeId);
      } else {
        _selected.remove(nodeId);
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    final size = MediaQuery.sizeOf(context);
    return CallbackShortcuts(
      bindings: <ShortcutActivator, VoidCallback>{
        const SingleActivator(LogicalKeyboardKey.escape): () =>
            Navigator.of(context).pop(),
      },
      child: Focus(
        autofocus: true,
        child: AlertDialog(
          insetPadding: const EdgeInsets.all(16),
          title: Row(
            children: [
              const Expanded(child: Text('Converse memory')),
              Tooltip(
                message: 'Refresh memory',
                child: TextButton(
                  key: const ValueKey('converse-memory-refresh'),
                  style: TextButton.styleFrom(
                    minimumSize: const Size(48, 48),
                    padding: EdgeInsets.zero,
                  ),
                  onPressed: _pendingReads > 0 ? null : _refresh,
                  child: const Icon(
                    Icons.refresh_rounded,
                    semanticLabel: 'Refresh memory',
                  ),
                ),
              ),
            ],
          ),
          content: SizedBox(
            width: (size.width - 80).clamp(200.0, 460.0),
            height: (size.height * 0.6).clamp(200.0, 520.0),
            child: SingleChildScrollView(child: _body(context)),
          ),
          actions: <Widget>[
            _useActions(),
            TextButton(
              key: const ValueKey('converse-memory-close'),
              onPressed: () => Navigator.of(context).pop(),
              child: const Text('CLOSE'),
            ),
          ],
        ),
      ),
    );
  }

  Widget _body(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final children = <Widget>[
      const Text('Choose saved notes to use in this conversation.'),
      const SizedBox(height: 8),
      ..._scopeSection(scheme),
      if (_pendingReads > 0) const Text('Loading saved notes…'),
      if (_ownsScope) ...[
        ..._namespaceSection(scheme),
        ..._useSection(scheme),
        ..._notesSection(scheme),
        ..._footerSection(),
      ],
      if (_notice != null) ...[const SizedBox(height: 8), Text(_notice!)],
    ];
    return FocusTraversalGroup(
      policy: OrderedTraversalPolicy(),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: children,
      ),
    );
  }

  List<Widget> _scopeSection(ColorScheme scheme) {
    final session = widget.state.converseSession;
    final binding = _binding;
    if (_client == null) {
      return const [
        Text(
          'There is no owner connection right now, so saved notes cannot '
          'be loaded. The conversation screen stays usable.',
        ),
      ];
    }
    if (session == null || binding == null) {
      return const [
        Text(
          'There is no current Converse session yet, so there are no saved '
          'notes to show. Start or join a Converse session first; opening '
          'this screen never creates one.',
        ),
      ];
    }
    return [
      Text(
        session.name.isEmpty ? 'This conversation' : session.name,
        style: TextStyle(color: scheme.onSurfaceVariant),
      ),
    ];
  }

  List<Widget> _namespaceSection(ColorScheme scheme) {
    final header = <Widget>[
      const SizedBox(height: 12),
      Text(
        'Saved note access',
        style: TextStyle(fontWeight: FontWeight.bold, color: scheme.onSurface),
      ),
      const Text('Turning access off keeps your notes and prevents their use.'),
    ];
    if (_nsError != null && _ns == null) {
      return [...header, Text(_nsError!)];
    }
    final snapshot = _ns;
    if (snapshot == null) {
      return [
        ...header,
        const Text('Saved access is unknown. Choose Refresh memory.'),
        if (_nsError != null) Text(_nsError!),
      ];
    }
    final uncertain = _scopeHasUncertain(_namespaceKey);
    return [
      ...header,
      Text(
        'Saved access: ${snapshot.enabled ? 'On' : 'Off'} · '
        '${snapshot.nodeCount} note${snapshot.nodeCount == 1 ? '' : 's'} retained',
      ),
      if (snapshot.status == 'quarantined')
        const Text('Saved notes are quarantined and unavailable for use.'),
      if (_nsError != null) Text(_nsError!),
      if (uncertain)
        const Text(
          'A saved-access change may still arrive. Refresh to check what '
          'the service has now. Unchanged values do not prove it failed, '
          'so further saved-access changes stay paused.',
        ),
      if (_refreshRequired && _ns == null)
        const Text(
          'Another change landed first. Choose Refresh memory before '
          'trying again.',
        ),
      Wrap(
        spacing: 8,
        runSpacing: 4,
        children: [
          FilledButton(
            key: const ValueKey('converse-memory-namespace-on'),
            onPressed:
                _nsBusy ||
                    _refreshRequired ||
                    !_ownsScope ||
                    _scopeHasUnresolved(_namespaceKey)
                ? null
                : () => _configureNamespace(true),
            child: Text(_nsBusy ? 'Saving…' : 'Turn saved access on'),
          ),
          OutlinedButton(
            key: const ValueKey('converse-memory-namespace-off'),
            onPressed:
                _nsBusy ||
                    _refreshRequired ||
                    !_ownsScope ||
                    _scopeHasUnresolved(_namespaceKey)
                ? null
                : () => _configureNamespace(false),
            child: Text(_nsBusy ? 'Saving…' : 'Turn saved access off'),
          ),
        ],
      ),
    ];
  }

  List<Widget> _notesSection(ColorScheme scheme) {
    final header = <Widget>[
      const SizedBox(height: 12),
      Text(
        'Saved notes',
        style: TextStyle(fontWeight: FontWeight.bold, color: scheme.onSurface),
      ),
      Text(
        'Select up to $memoryMaxSelection active notes.',
        style: TextStyle(color: scheme.onSurfaceVariant),
      ),
    ];
    if (_catalogError != null && _catalog == null) {
      return [
        ...header,
        Text(_catalogError!),
        const Text(
          'Stopping use can still work below when its own state is known.',
        ),
      ];
    }
    final catalog = _catalog;
    if (catalog == null) {
      return [
        ...header,
        const Text('The note list is unknown. Choose Refresh memory.'),
        if (_catalogError != null) Text(_catalogError!),
      ];
    }
    if (catalog.entries.isEmpty) {
      return [
        ...header,
        const Text('No saved notes yet for this conversation.'),
        if (_catalogError != null) Text(_catalogError!),
      ];
    }
    final rows = <Widget>[
      Text('${_selected.length} of $memoryMaxSelection notes selected.'),
      if (_selected.length >= memoryMaxSelection)
        const Text(
          'You can select up to 8 notes. Untick one to choose '
          'another.',
        ),
      if (_catalogError != null) Text(_catalogError!),
    ];
    for (final entry in catalog.entries) {
      final selectable =
          entry.state == 'active' &&
          (_selected.contains(entry.nodeId) ||
              _selected.length < memoryMaxSelection) &&
          !_useBusy &&
          !_refreshRequired &&
          _ownsScope;
      rows.add(
        Card(
          margin: const EdgeInsets.symmetric(vertical: 4),
          child: Padding(
            padding: const EdgeInsets.all(8),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                if (entry.state == 'active')
                  CheckboxListTile(
                    key: ValueKey('memory-select-${entry.nodeId}'),
                    value: _selected.contains(entry.nodeId),
                    onChanged: !selectable && !_selected.contains(entry.nodeId)
                        ? null
                        : (v) => _toggleSelected(entry.nodeId, v ?? false),
                    controlAffinity: ListTileControlAffinity.leading,
                    contentPadding: EdgeInsets.zero,
                    dense: true,
                    title: Text(
                      '#${entry.ordinal} · Kind: ${entry.kind} · '
                      'State: ${entry.state}',
                    ),
                    subtitle: entry.preview == null
                        ? null
                        : Text(entry.preview!),
                  )
                else
                  MergeSemantics(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          '#${entry.ordinal} · Kind: ${entry.kind} · '
                          'State: ${entry.state}',
                        ),
                        if (entry.preview != null) Text(entry.preview!),
                        const Text('Only active notes can be ticked for use.'),
                      ],
                    ),
                  ),
                Align(
                  alignment: Alignment.centerRight,
                  child: TextButton(
                    key: ValueKey('memory-inspect-${entry.nodeId}'),
                    onPressed:
                        (_detailBusy && _detailFor == entry.nodeId) ||
                            !_ownsScope
                        ? null
                        : () => _inspect(entry.nodeId),
                    child: const Text('Details'),
                  ),
                ),
                if (_detailFor == entry.nodeId) _detailPane(),
              ],
            ),
          ),
        ),
      );
    }
    return [...header, ...rows];
  }

  Widget _detailPane() {
    if (_detailBusy) return const Text('Loading note detail…');
    if (_detailError != null) return Text(_detailError!);
    final inspection = _detail;
    if (inspection == null) return const SizedBox.shrink();
    final node = inspection.node;
    final fact = node.fact;
    final lines = <String>[
      'Note ${node.nodeId} (#${node.ordinal})',
      'Kind: ${node.kind}',
      'State: ${node.state}',
      if (node.body != null) 'Text: ${node.body}',
      if (fact != null) 'Fact ${fact.key}: ${fact.value}',
      'Source event ${node.sourceEventId}, revision ${node.sourceRevision}, '
          'message ${node.sourceSequence}',
      'Scope: ${node.contentScope}, origin: ${node.provenance}',
    ];
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(8),
      decoration: BoxDecoration(
        border: Border.all(color: Theme.of(context).colorScheme.outlineVariant),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Semantics(
            key: const ValueKey('converse-memory-detail-text'),
            label: lines.join('\n'),
            excludeSemantics: true,
            child: SelectableText(lines.join('\n')),
          ),
          Align(
            alignment: Alignment.centerRight,
            child: TextButton(
              key: const ValueKey('converse-memory-detail-hide'),
              onPressed: () => setState(() {
                _detail = null;
                _detailError = null;
                _detailFor = null;
              }),
              child: const Text('Hide details'),
            ),
          ),
        ],
      ),
    );
  }

  List<Widget> _useSection(ColorScheme scheme) {
    final header = <Widget>[
      const SizedBox(height: 12),
      Text(
        'Use in this conversation',
        style: TextStyle(fontWeight: FontWeight.bold, color: scheme.onSurface),
      ),
      const Text(
        'Use turns off when Converse restarts. Saved access and notes are kept.',
      ),
    ];
    final snapshot = _use ?? _lastUseForStop;
    if (snapshot == null) {
      return [
        ...header,
        const Text('Current use is unknown. Choose Refresh memory.'),
        if (_useError != null) Text(_useError!),
      ];
    }
    final uncertain = _scopeHasUncertain(_runtimeKey);
    return [
      ...header,
      if (_use == null)
        const Text(
          'Current use is unknown. Last observed state is shown; Stop may be refused if it changed.',
        ),
      Text(
        'Use: ${snapshot.enabled ? 'On' : 'Off'} · '
        '${snapshot.selectorIds.length} '
        '${snapshot.selectorIds.length == 1 ? 'note' : 'notes'} selected for use',
      ),
      if (snapshot.notice == 'unavailable')
        const Text('Memory is currently unavailable.'),
      if (snapshot.notice == 'context_omitted')
        const Text('Notes were omitted from the latest response context.'),
      if (snapshot.pending) const Text('Memory work is still pending.'),
      if (_useError != null) Text(_useError!),
      if (uncertain)
        const Text(
          'A use change may still arrive. Refresh to check what the '
          'service has now. Unchanged values do not prove it failed.',
        ),
    ];
  }

  Widget _useActions() {
    final compact =
        MediaQuery.sizeOf(context).width <= 400 ||
        MediaQuery.textScalerOf(context).scale(14) > 21;
    return Wrap(
      spacing: 8,
      runSpacing: 4,
      children: [
        FilledButton(
          key: const ValueKey('converse-memory-use'),
          style: FilledButton.styleFrom(minimumSize: const Size(48, 48)),
          onPressed: _canUseSelected ? () => _configureUse(enable: true) : null,
          child: Semantics(
            label: 'Use selected notes (${_selected.length})',
            excludeSemantics: true,
            child: Text(
              _useBusy
                  ? 'Saving…'
                  : compact
                  ? 'Use (${_selected.length})'
                  : 'Use selected notes (${_selected.length})',
            ),
          ),
        ),
        OutlinedButton(
          key: const ValueKey('converse-memory-stop'),
          style: OutlinedButton.styleFrom(minimumSize: const Size(48, 48)),
          // Stop needs no selection and stays available while the
          // namespace catalog is unavailable or use is uncertain, as
          // long as a current runtime snapshot is known.
          onPressed: _canStop ? () => _configureUse(enable: false) : null,
          child: Semantics(
            label: 'Stop using notes',
            excludeSemantics: true,
            child: Text(
              _useBusy
                  ? 'Saving…'
                  : compact
                  ? 'Stop'
                  : 'Stop using notes',
            ),
          ),
        ),
      ],
    );
  }

  List<Widget> _footerSection() => const [
    SizedBox(height: 12),
    Text(
      '“On” means configured; a response may still omit notes when they '
      'cannot be used.',
    ),
  ];
}
