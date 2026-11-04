/// Owner-only Companions panel for pairing, device directory, and foreground
/// invitation controls (ADR0045, ADR0046, ADR0110).
///
/// Opening or closing this panel performs only owner `GET` reads (`listDevices`
/// and, when a current Converse session binding is present, `listCalls`). It
/// never creates a session, issues a pairing code, sends an invitation,
/// touches microphone/audio/playback, alters owner drafts, or invokes
/// inference.
library;

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:uuid/uuid.dart';

import '../api/client.dart';
import '../api/companion_client.dart';
import '../api/companion_models.dart';
import '../state/app_state.dart';
import 'design_tokens.dart';

const int companionUnresolvedScopeBound = 32;
const _uuid = Uuid();
int _companionObservationSerial = 0;

class _UncertainInvite {
  _UncertainInvite({
    required this.deviceId,
    required this.title,
    required this.idempotencyKey,
    required this.baselineCallIds,
  });

  final String deviceId;
  final String title;
  final String idempotencyKey;
  final Set<String> baselineCallIds;
}

class _UncertainEnd {
  _UncertainEnd({
    required this.callId,
    required this.deviceId,
    required this.sequence,
    required this.expectedRevision,
    required this.idempotencyKey,
  });

  final String callId;
  final String deviceId;
  final int sequence;
  final int expectedRevision;
  final String idempotencyKey;
}

class _UncertainNote {
  _UncertainNote({
    required this.callId,
    required this.deviceId,
    required this.sequence,
    required this.expectedRevision,
    required this.draftNote,
  });

  final String callId;
  final String deviceId;
  final int sequence;
  final int expectedRevision;
  final String draftNote;
  int? reconciledRevision;
  int? reconcileAfterObservation;
  bool get needsGetReconciliation => reconciledRevision == null;
}

class _ScopeUncertainty {
  _UncertainInvite? invite;
  final Map<String, _UncertainEnd> ends = <String, _UncertainEnd>{};
  final Map<String, _UncertainNote> notes = <String, _UncertainNote>{};
  final Set<Object> inFlight = <Object>{};

  bool get isEmpty =>
      invite == null && ends.isEmpty && notes.isEmpty && inFlight.isEmpty;
}

final Map<String, _ScopeUncertainty> _unresolvedScopes =
    <String, _ScopeUncertainty>{};

/// Clears retained companion uncertainty state in tests.
@visibleForTesting
void resetCompanionUncertaintyForTest() {
  _unresolvedScopes.clear();
  _companionObservationSerial = 0;
}

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

String _scopeKey(String endpoint, OwnerCompanionBinding binding) =>
    '${_canonicalEndpoint(endpoint)}\x00'
    '${binding.sessionId}\x00${binding.threadId}\x00${binding.epoch}';

_ScopeUncertainty? _ensureScopeUncertainty(String key) {
  if (key.isEmpty) return null;
  final existing = _unresolvedScopes[key];
  if (existing != null) return existing;
  final activeCount = _unresolvedScopes.values
      .where((entry) => !entry.isEmpty)
      .length;
  if (activeCount >= companionUnresolvedScopeBound) return null;
  return _unresolvedScopes[key] = _ScopeUncertainty();
}

void _pruneScopeUncertainty(String key) {
  final entry = _unresolvedScopes[key];
  if (entry != null && entry.isEmpty) {
    _unresolvedScopes.remove(key);
  }
}

/// Unobtrusive entry beside Voice options and Memory. Always enabled: opening
/// the dialog performs only owner reads.
class ConverseCompanionsButton extends StatelessWidget {
  const ConverseCompanionsButton({super.key, required this.state});

  final AppState state;

  @override
  Widget build(BuildContext context) => Tooltip(
    message: 'Pair companions and manage foreground invitations',
    child: TextButton.icon(
      key: const ValueKey('converse-companions'),
      icon: const Icon(Icons.devices_outlined),
      label: const Text(
        'Companions',
        maxLines: 1,
        overflow: TextOverflow.ellipsis,
      ),
      onPressed: () =>
          showConverseCompanionsDialog(context: context, state: state),
    ),
  );
}

/// Opens the owner Companions dialog. Presentation and owner-read only on open.
Future<void> showConverseCompanionsDialog({
  required BuildContext context,
  required AppState state,
  int Function()? nowSeconds,
  Duration pollInterval = const Duration(seconds: 2),
}) => showDialog<void>(
  context: context,
  builder: (_) => ConverseCompanionsDialog(
    state: state,
    nowSeconds: nowSeconds,
    pollInterval: pollInterval,
  ),
);

class ConverseCompanionsDialog extends StatefulWidget {
  const ConverseCompanionsDialog({
    super.key,
    required this.state,
    this.embedded = false,
    this.nowSeconds,
    this.pollInterval = const Duration(seconds: 2),
  });

  final AppState state;
  final bool embedded;
  final int Function()? nowSeconds;
  final Duration pollInterval;

  @override
  State<ConverseCompanionsDialog> createState() =>
      _ConverseCompanionsDialogState();
}

class _ConverseCompanionsDialogState extends State<ConverseCompanionsDialog>
    with WidgetsBindingObserver {
  KinaiyaClient? _client;
  int _generation = 0;
  OwnerCompanionBinding? _binding;

  List<OwnerCompanionDevice> _devices = const <OwnerCompanionDevice>[];
  List<OwnerCompanionCall> _calls = const <OwnerCompanionCall>[];
  bool _devicesLoaded = false;
  bool _callsLoaded = false;
  String? _devicesError;
  String? _callsError;
  String? _notice;

  bool _refreshing = false;
  int _readToken = 0;
  int _actionToken = 0;
  bool _acting = false;
  bool _issuingPairing = false;
  bool _staleScope = false;
  bool _visible = true;

  Timer? _pollTimer;
  Timer? _expiryTimer;

  OwnerPairingIssued? _issuedPairing;
  String? _pairingError;

  final TextEditingController _pairingLabelController = TextEditingController(
    text: 'Foreground companion',
  );
  final Map<String, TextEditingController> _inviteTitleControllers =
      <String, TextEditingController>{};
  final Map<String, TextEditingController> _noteControllers =
      <String, TextEditingController>{};
  final Set<String> _dirtyNoteCallIds = <String>{};

  int _now() => widget.nowSeconds != null
      ? widget.nowSeconds!()
      : DateTime.now().millisecondsSinceEpoch ~/ 1000;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    final lifecycle = WidgetsBinding.instance.lifecycleState;
    _visible = lifecycle == null || lifecycle == AppLifecycleState.resumed;
    _captureScope();
    widget.state.addListener(_onExternal);
    _restoreUncertaintyDrafts();
    unawaited(_refresh());
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    final nextVisible = state == AppLifecycleState.resumed;
    if (_visible == nextVisible) return;
    _visible = nextVisible;
    _pollTimer?.cancel();
    if (_visible && mounted && !_staleScope) {
      unawaited(_refresh());
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    widget.state.removeListener(_onExternal);
    _pollTimer?.cancel();
    _expiryTimer?.cancel();
    _issuedPairing = null;
    _pairingLabelController.dispose();
    for (final controller in _inviteTitleControllers.values) {
      controller.dispose();
    }
    for (final controller in _noteControllers.values) {
      controller.dispose();
    }
    super.dispose();
  }

  void _captureScope() {
    final live = widget.state;
    _client = live.client;
    _generation = live.connectionGeneration;
    final session = live.converseSession;
    OwnerCompanionBinding? binding;
    if (session != null && session.channel == 'converse') {
      try {
        binding = OwnerCompanionBinding.fromSession(session);
      } on FormatException {
        binding = null;
      }
    }
    _binding = binding;
    _staleScope = false;
  }

  bool _scopeMatches() {
    final live = widget.state;
    if (!identical(_client, live.client)) return false;
    if (_generation != live.connectionGeneration) return false;
    final session = live.converseSession;
    if (session == null || session.channel != 'converse') {
      return _binding == null;
    }
    if (_binding == null) return false;
    return _binding!.sessionId == session.sessionId &&
        _binding!.threadId == session.threadId &&
        _binding!.epoch == session.epoch;
  }

  bool get _ownsClient =>
      mounted && !_staleScope && _scopeMatches() && _client != null;

  bool get _ownsScope => _ownsClient && _binding != null;

  String get _currentScopeKey {
    final client = _client;
    final binding = _binding;
    if (client == null || binding == null) return '';
    return _scopeKey(client.sessionBase, binding);
  }

  _ScopeUncertainty? get _currentUncertainty =>
      _unresolvedScopes[_currentScopeKey];

  void _restoreUncertaintyDrafts() {
    final uncertainty = _currentUncertainty;
    if (uncertainty == null) return;
    for (final note in uncertainty.notes.values) {
      final controller = _noteControllers.putIfAbsent(
        note.callId,
        () => TextEditingController(text: note.draftNote),
      );
      controller.text = note.draftNote;
      _dirtyNoteCallIds.add(note.callId);
    }
  }

  void _onExternal() {
    if (!mounted || _scopeMatches()) return;
    _pollTimer?.cancel();
    _expiryTimer?.cancel();
    setState(() {
      _staleScope = true;
      _readToken++;
      _actionToken++;
      _refreshing = false;
      _acting = false;
      _issuingPairing = false;
      _issuedPairing = null;
      _pairingError = null;
      _devices = const <OwnerCompanionDevice>[];
      _calls = const <OwnerCompanionCall>[];
      _devicesLoaded = false;
      _callsLoaded = false;
      _devicesError = null;
      _callsError = null;
      _dirtyNoteCallIds.clear();
      _notice =
          'The conversation or connection changed. '
          'Choose Refresh to load the current scope. Pending requests retain their original recovery state.';
    });
  }

  void _manualRefresh() {
    _pollTimer?.cancel();
    final sameScope = _scopeMatches();
    if (!sameScope) {
      _expiryTimer?.cancel();
      _issuedPairing = null;
      _pairingError = null;
      _dirtyNoteCallIds.clear();
    }
    _captureScope();
    _restoreUncertaintyDrafts();
    setState(() {
      _readToken++;
      _refreshing = false;
      _devicesError = null;
      _callsError = null;
      _notice = null;
    });
    unawaited(_refresh());
  }

  void _scheduleNextPoll() {
    _pollTimer?.cancel();
    if (!mounted ||
        !_visible ||
        _staleScope ||
        _refreshing ||
        _client == null ||
        widget.pollInterval <= Duration.zero) {
      return;
    }
    _pollTimer = Timer(widget.pollInterval, () {
      if (mounted && _visible && !_staleScope && !_refreshing) {
        unawaited(_refresh());
      }
    });
  }

  void _startExpiryTicker() {
    _expiryTimer?.cancel();
    if (_issuedPairing == null) return;
    _expiryTimer = Timer.periodic(const Duration(seconds: 1), (timer) {
      if (!mounted || _issuedPairing == null) {
        timer.cancel();
        return;
      }
      final remaining = _issuedPairing!.expiresAt - _now();
      if (remaining <= 0) {
        timer.cancel();
        setState(() {
          _issuedPairing = null;
          _notice =
              'The one-time pairing code expired after 120 seconds. Issue a new code when ready.';
        });
      } else {
        setState(() {});
      }
    });
  }

  Future<void> _refresh() async {
    if (!mounted || _staleScope || _refreshing) return;
    final client = _client;
    final binding = _binding;
    if (client == null) {
      setState(() {
        _devicesLoaded = false;
        _callsLoaded = false;
      });
      return;
    }
    _pollTimer?.cancel();
    final token = ++_readToken;
    final observation = ++_companionObservationSerial;
    setState(() {
      _refreshing = true;
    });
    List<OwnerCompanionDevice>? loadedDevices;
    String? devicesProblem;
    List<OwnerCompanionCall>? loadedCalls;
    String? callsProblem;
    try {
      try {
        loadedDevices = await client.companion.listDevices();
      } on OwnerCompanionFailure catch (e) {
        devicesProblem = e.message;
      } catch (_) {
        devicesProblem = 'Could not load companion devices.';
      }
      if (binding != null) {
        try {
          loadedCalls = await client.companion.listCalls(binding);
        } on OwnerCompanionFailure catch (e) {
          callsProblem = e.message;
        } catch (_) {
          callsProblem = 'Could not load companion invitations.';
        }
      }
      if (!mounted || token != _readToken || !_scopeMatches()) return;
      setState(() {
        if (loadedDevices != null) {
          _devices = loadedDevices;
          _devicesLoaded = true;
          _devicesError = null;
        } else {
          _devicesError = devicesProblem;
        }
        if (binding != null) {
          if (loadedCalls != null) {
            _mergeCalls(
              loadedCalls,
              reconcileNotes: true,
              observation: observation,
            );
            _callsLoaded = true;
            _callsError = null;
          } else {
            _callsError = callsProblem;
          }
        } else {
          _calls = const <OwnerCompanionCall>[];
          _callsLoaded = false;
          _callsError = null;
        }
      });
    } finally {
      if (mounted && token == _readToken && _scopeMatches()) {
        setState(() {
          _refreshing = false;
        });
        _scheduleNextPoll();
      }
    }
  }

  void _mergeCalls(
    Iterable<OwnerCompanionCall> incoming, {
    bool reconcileNotes = false,
    int? observation,
  }) {
    final authoritative = incoming.toList();
    final byId = <String, OwnerCompanionCall>{
      for (final call in _calls) call.callId: call,
    };
    for (final call in authoritative) {
      final previous = byId[call.callId];
      if (previous == null || previous.revision <= call.revision) {
        byId[call.callId] = call;
      }
    }
    final sorted = byId.values.toList()
      ..sort((left, right) => right.sequence.compareTo(left.sequence));
    _calls = sorted;

    for (final call in sorted) {
      final controller = _noteControllers.putIfAbsent(
        call.callId,
        () => TextEditingController(text: call.note),
      );
      if (!_dirtyNoteCallIds.contains(call.callId)) {
        controller.text = call.note;
      }
    }

    final key = _currentScopeKey;
    final uncertainty = _unresolvedScopes[key];
    if (uncertainty == null) return;

    final pendingInvite = uncertainty.invite;
    if (pendingInvite != null &&
        !uncertainty.inFlight.contains(pendingInvite)) {
      final observed = authoritative.any(
        (call) =>
            call.deviceId == pendingInvite.deviceId &&
            (!pendingInvite.baselineCallIds.contains(call.callId) ||
                !call.state.terminal),
      );
      if (observed) {
        uncertainty.invite = null;
      }
    }

    for (final call in authoritative) {
      final pendingEnd = uncertainty.ends[call.callId];
      if (pendingEnd != null &&
          !uncertainty.inFlight.contains(pendingEnd) &&
          (call.state.terminal ||
              call.revision > pendingEnd.expectedRevision)) {
        uncertainty.ends.remove(call.callId);
      }
      if (reconcileNotes) {
        final pendingNote = uncertainty.notes[call.callId];
        if (pendingNote != null &&
            !uncertainty.inFlight.contains(pendingNote) &&
            call.revision >= byId[call.callId]!.revision &&
            observation != null &&
            pendingNote.reconcileAfterObservation != null &&
            observation > pendingNote.reconcileAfterObservation!) {
          if (call.note == pendingNote.draftNote &&
              call.revision > pendingNote.expectedRevision) {
            uncertainty.notes.remove(call.callId);
            _dirtyNoteCallIds.remove(call.callId);
            _noteControllers[call.callId]?.text = call.note;
          } else {
            pendingNote.reconciledRevision = call.revision;
          }
        }
      }
    }
    _pruneScopeUncertainty(key);
  }

  Future<void> _issuePairing() async {
    if (!_ownsScope || _issuingPairing || _acting) return;
    final client = _client!;
    final binding = _binding!;
    final label = _pairingLabelController.text.trim();
    final token = ++_actionToken;
    setState(() {
      _issuingPairing = true;
      _pairingError = null;
      _notice = null;
    });
    try {
      final issued = await client.companion.issuePairing(binding, label: label);
      if (!mounted || token != _actionToken || !_scopeMatches()) return;
      setState(() {
        _issuedPairing = issued;
        _pairingError = null;
        _notice =
            'One-time pairing code issued in memory. Copy it before it expires.';
      });
      _startExpiryTicker();
    } on OwnerCompanionFailure catch (e) {
      if (!mounted || token != _actionToken || !_scopeMatches()) return;
      _expiryTimer?.cancel();
      setState(() {
        _issuedPairing = null;
        _pairingError = e.unconfirmed
            ? 'Pairing issuance was interrupted. A lost pairing code cannot be recovered; issue a new code when ready.'
            : e.message;
      });
    } finally {
      if (mounted && token == _actionToken && _scopeMatches()) {
        setState(() {
          _issuingPairing = false;
        });
      }
    }
  }

  Future<void> _copyPairingCode() async {
    final issued = _issuedPairing;
    if (issued == null) return;
    final remaining = issued.expiresAt - _now();
    if (remaining <= 0) {
      setState(() {
        _issuedPairing = null;
        _notice =
            'The one-time pairing code expired. Issue a new code when ready.';
      });
      return;
    }
    await Clipboard.setData(ClipboardData(text: issued.pairingCode));
    if (!mounted) return;
    setState(() {
      _notice = 'Pairing code copied to clipboard.';
    });
  }

  TextEditingController _inviteControllerFor(OwnerCompanionDevice device) =>
      _inviteTitleControllers.putIfAbsent(
        device.deviceId,
        () => TextEditingController(text: 'Companion call'),
      );

  Future<void> _inviteDevice(OwnerCompanionDevice device) async {
    if (!_ownsScope || _acting || _issuingPairing) return;
    final key = _currentScopeKey;
    final existing = _unresolvedScopes[key]?.invite;
    if (existing != null) return;
    final scope = _ensureScopeUncertainty(key);
    if (scope == null) {
      setState(() {
        _notice =
            'Too many unresolved companion scopes are retained. Reconcile existing requests first.';
      });
      return;
    }
    final title = _inviteControllerFor(device).text.trim();
    final attempt = _UncertainInvite(
      deviceId: device.deviceId,
      title: title,
      idempotencyKey: _uuid.v4(),
      baselineCallIds: Set<String>.unmodifiable(_calls.map((c) => c.callId)),
    );
    await _dispatchInvite(attempt);
  }

  Future<void> _retryUncertainInvite() async {
    final attempt = _currentUncertainty?.invite;
    if (!_ownsScope || _acting || _issuingPairing || attempt == null) return;
    await _dispatchInvite(attempt);
  }

  Future<void> _dispatchInvite(_UncertainInvite attempt) async {
    final client = _client!;
    final binding = _binding!;
    final scopeKey = _currentScopeKey;
    final scope = _unresolvedScopes[scopeKey]!;
    if (scope.inFlight.contains(attempt)) return;
    final wasRetained = identical(scope.invite, attempt);
    if (scope.invite != null && !wasRetained) return;
    // The scope, captured client and immutable payload outlive this widget.
    // A pending write cannot be proved absent by a concurrent GET.
    scope.invite = attempt;
    scope.inFlight.add(attempt);
    final token = ++_actionToken;
    setState(() {
      _acting = true;
      _notice = null;
    });
    var reconcileAfter409 = false;
    try {
      final created = await client.companion.inviteCall(
        binding,
        deviceId: attempt.deviceId,
        title: attempt.title,
        idempotencyKey: attempt.idempotencyKey,
      );
      if (identical(scope.invite, attempt)) scope.invite = null;
      if (!mounted || token != _actionToken || !_scopeMatches()) return;
      setState(() {
        _mergeCalls(<OwnerCompanionCall>[created]);
        _callsLoaded = true;
        _notice = 'Invitation sent to companion.';
      });
    } on OwnerCompanionFailure catch (e) {
      if (!e.unconfirmed &&
          (!wasRetained || e.status == 409) &&
          identical(scope.invite, attempt)) {
        scope.invite = null;
      }
      if (!mounted || token != _actionToken || !_scopeMatches()) return;
      if (e.unconfirmed) {
        setState(() {
          _notice =
              'Invitation request is unconfirmed. Retry sends the exact original request, or Refresh to inspect current calls.';
        });
      } else {
        if (e.status == 409) {
          reconcileAfter409 = true;
        }
        setState(() {
          _notice = e.message;
        });
      }
    } finally {
      scope.inFlight.remove(attempt);
      _pruneScopeUncertainty(scopeKey);
      if (mounted && token == _actionToken && _scopeMatches()) {
        setState(() {
          _acting = false;
        });
        if (reconcileAfter409) {
          await _refresh();
        }
      }
    }
  }

  Future<void> _revokeDevice(OwnerCompanionDevice device) async {
    if (!_ownsClient || _acting || _issuingPairing || !device.canRevoke) return;
    final client = _client!;
    final token = ++_actionToken;
    setState(() {
      _acting = true;
      _notice = null;
    });
    try {
      final revoked = await client.companion.revokeDevice(device.deviceId);
      if (!mounted || token != _actionToken || !_scopeMatches()) return;
      setState(() {
        _devices = <OwnerCompanionDevice>[
          for (final item in _devices)
            if (item.deviceId == revoked.deviceId) revoked else item,
        ];
        _notice = 'Revoked companion ${revoked.deviceName}.';
      });
      await _refresh();
    } on OwnerCompanionFailure catch (e) {
      if (!mounted || token != _actionToken || !_scopeMatches()) return;
      setState(() {
        _notice = e.message;
      });
    } finally {
      if (mounted && token == _actionToken && _scopeMatches()) {
        setState(() {
          _acting = false;
        });
      }
    }
  }

  Future<void> _inspectCall(OwnerCompanionCall call) async {
    if (!_ownsScope || _acting || _issuingPairing) return;
    final client = _client!;
    final binding = _binding!;
    final token = ++_actionToken;
    final observation = ++_companionObservationSerial;
    setState(() {
      _acting = true;
      _notice = null;
    });
    try {
      final inspected = await client.companion.getCall(
        call.callId,
        expectedBinding: binding,
        expectedDeviceId: call.deviceId,
      );
      if (!mounted || token != _actionToken || !_scopeMatches()) return;
      setState(() {
        _mergeCalls(
          <OwnerCompanionCall>[inspected],
          reconcileNotes: true,
          observation: observation,
        );
        _notice =
            'Reconciled invitation ${inspected.title} (revision ${inspected.revision}).';
      });
    } on OwnerCompanionFailure catch (e) {
      if (!mounted || token != _actionToken || !_scopeMatches()) return;
      setState(() {
        _notice = e.message;
      });
    } finally {
      if (mounted && token == _actionToken && _scopeMatches()) {
        setState(() {
          _acting = false;
        });
      }
    }
  }

  Future<void> _endCall(OwnerCompanionCall call) async {
    if (!_ownsScope || _acting || _issuingPairing || call.state.terminal) {
      return;
    }
    final scopeKey = _currentScopeKey;
    if (_unresolvedScopes[scopeKey]?.ends.containsKey(call.callId) == true) {
      return;
    }
    final scope = _ensureScopeUncertainty(scopeKey);
    if (scope == null) {
      setState(() {
        _notice =
            'Too many unresolved companion scopes are retained. Reconcile existing requests first.';
      });
      return;
    }
    final attempt = _UncertainEnd(
      callId: call.callId,
      deviceId: call.deviceId,
      sequence: call.sequence,
      expectedRevision: call.revision,
      idempotencyKey: _uuid.v4(),
    );
    await _dispatchEnd(attempt);
  }

  Future<void> _retryUncertainEnd(String callId) async {
    final attempt = _currentUncertainty?.ends[callId];
    if (!_ownsScope || _acting || _issuingPairing || attempt == null) return;
    await _dispatchEnd(attempt);
  }

  Future<void> _dispatchEnd(_UncertainEnd attempt) async {
    final client = _client!;
    final binding = _binding!;
    final scopeKey = _currentScopeKey;
    final scope = _unresolvedScopes[scopeKey]!;
    if (scope.inFlight.contains(attempt)) return;
    final wasRetained = identical(scope.ends[attempt.callId], attempt);
    if (scope.ends.containsKey(attempt.callId) && !wasRetained) return;
    scope.ends[attempt.callId] = attempt;
    scope.inFlight.add(attempt);
    final token = ++_actionToken;
    setState(() {
      _acting = true;
      _notice = null;
    });
    var reconcileCall = false;
    try {
      final ended = await client.companion.endCall(
        binding,
        callId: attempt.callId,
        deviceId: attempt.deviceId,
        sequence: attempt.sequence,
        expectedRevision: attempt.expectedRevision,
        idempotencyKey: attempt.idempotencyKey,
      );
      if (identical(scope.ends[attempt.callId], attempt)) {
        scope.ends.remove(attempt.callId);
      }
      if (!mounted || token != _actionToken || !_scopeMatches()) return;
      setState(() {
        _mergeCalls(<OwnerCompanionCall>[ended]);
        _notice = 'Ended invitation ${ended.title}.';
      });
    } on OwnerCompanionFailure catch (e) {
      if (!e.unconfirmed &&
          (!wasRetained || e.status == 409) &&
          identical(scope.ends[attempt.callId], attempt)) {
        scope.ends.remove(attempt.callId);
      }
      if (!mounted || token != _actionToken || !_scopeMatches()) return;
      if (e.unconfirmed) {
        setState(() {
          _notice =
              'End request is unconfirmed. Retry sends the exact original request, or Inspect to reconcile.';
        });
      } else {
        if (e.status == 409) {
          reconcileCall = true;
        }
        setState(() {
          _notice = e.message;
        });
      }
    } finally {
      scope.inFlight.remove(attempt);
      _pruneScopeUncertainty(scopeKey);
      if (mounted && token == _actionToken && _scopeMatches()) {
        setState(() {
          _acting = false;
        });
        if (reconcileCall) {
          final current = _calls
              .where((c) => c.callId == attempt.callId)
              .firstOrNull;
          if (current != null) {
            await _inspectCall(current);
          } else {
            await _refresh();
          }
        }
      }
    }
  }

  bool _canEditCallNote(OwnerCompanionCall call) {
    if (!_ownsScope || call.binding != _binding) return false;
    final device = _devices
        .where((d) => d.deviceId == call.deviceId)
        .firstOrNull;
    if (device == null) return false;
    return device.canInviteFor(_binding);
  }

  Future<void> _saveNote(OwnerCompanionCall call) async {
    if (!_canEditCallNote(call) || _acting || _issuingPairing) return;
    final scopeKey = _currentScopeKey;
    final existingUncertain = _unresolvedScopes[scopeKey]?.notes[call.callId];
    if (existingUncertain != null && existingUncertain.needsGetReconciliation) {
      return;
    }
    final scope = _ensureScopeUncertainty(scopeKey);
    if (scope == null) {
      setState(() {
        _notice =
            'Too many unresolved companion scopes are retained. Reconcile existing requests first.';
      });
      return;
    }
    final expectedRevision =
        existingUncertain?.reconciledRevision ?? call.revision;
    final draft = _noteControllers[call.callId]?.text ?? call.note;
    final attempt = _UncertainNote(
      callId: call.callId,
      deviceId: call.deviceId,
      sequence: call.sequence,
      expectedRevision: expectedRevision,
      draftNote: draft,
    );
    scope.notes[call.callId] = attempt;
    scope.inFlight.add(attempt);
    final client = _client!;
    final binding = _binding!;
    final token = ++_actionToken;
    setState(() {
      _acting = true;
      _notice = null;
    });
    try {
      final updated = await client.companion.updateNote(
        binding,
        callId: call.callId,
        deviceId: call.deviceId,
        sequence: call.sequence,
        expectedRevision: expectedRevision,
        note: draft,
      );
      if (identical(scope.notes[call.callId], attempt)) {
        scope.notes.remove(call.callId);
      }
      if (!mounted || token != _actionToken || !_scopeMatches()) return;
      _dirtyNoteCallIds.remove(call.callId);
      setState(() {
        _mergeCalls(<OwnerCompanionCall>[updated]);
        _notice = 'Saved literal note for ${updated.title}.';
      });
    } on OwnerCompanionFailure catch (e) {
      if (!e.unconfirmed &&
          e.status != 409 &&
          identical(scope.notes[call.callId], attempt)) {
        scope.notes.remove(call.callId);
      }
      if (!mounted || token != _actionToken || !_scopeMatches()) return;
      if (e.unconfirmed || e.status == 409) {
        _dirtyNoteCallIds.add(call.callId);
        setState(() {
          _notice = e.unconfirmed
              ? 'Note update is unconfirmed. Reconcile this call (GET) before another note edit.'
              : 'Call revision changed before note save. Reconcile this call (GET) before saving again.';
        });
      } else {
        setState(() {
          _notice = e.message;
        });
      }
    } finally {
      // Reads already pending when this write settled cannot establish a new
      // CAS baseline, even if their old snapshot arrives after the failure.
      attempt.reconcileAfterObservation = ++_companionObservationSerial;
      scope.inFlight.remove(attempt);
      _pruneScopeUncertainty(scopeKey);
      if (mounted && token == _actionToken && _scopeMatches()) {
        setState(() {
          _acting = false;
        });
      }
    }
  }

  Widget _buildPairingSection(BuildContext context) {
    final theme = Theme.of(context);
    final binding = _binding;
    final issued = _issuedPairing;
    final remaining = issued == null ? 0 : issued.expiresAt - _now();
    return Card(
      key: const ValueKey('companion-pairing-card'),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Semantics(
              header: true,
              child: Text(
                'Pair a foreground companion',
                style: theme.textTheme.titleMedium?.copyWith(
                  fontWeight: FontWeight.w700,
                ),
              ),
            ),
            const SizedBox(height: 8),
            Text(
              binding == null
                  ? 'Start or select a Converse session first to issue a 120-second one-time pairing code.'
                  : 'Issues a 120-second single-use code bound to Converse session ${binding.sessionId} (epoch ${binding.epoch}). Codes stay in memory only.',
              style: theme.textTheme.bodyMedium,
            ),
            const SizedBox(height: 12),
            TextField(
              key: const ValueKey('companion-pairing-label'),
              controller: _pairingLabelController,
              enabled: _ownsScope && !_issuingPairing && !_acting,
              maxLength: 120,
              decoration: const InputDecoration(
                labelText: 'Companion label',
                hintText: 'Foreground companion',
              ),
            ),
            const SizedBox(height: 8),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              crossAxisAlignment: WrapCrossAlignment.center,
              children: [
                FilledButton(
                  key: const ValueKey('companion-issue-pairing'),
                  onPressed: _ownsScope && !_issuingPairing && !_acting
                      ? _issuePairing
                      : null,
                  child: Text(
                    _issuingPairing ? 'Issuing…' : 'Issue pairing code',
                  ),
                ),
              ],
            ),
            if (_pairingError != null) ...[
              const SizedBox(height: 12),
              Semantics(
                liveRegion: true,
                child: Text(
                  _pairingError!,
                  key: const ValueKey('companion-pairing-error'),
                  style: TextStyle(color: theme.colorScheme.error),
                ),
              ),
            ],
            if (issued != null && remaining > 0) ...[
              const Divider(height: 24),
              Text(
                'One-time pairing code (${issued.label})',
                style: theme.textTheme.labelLarge,
              ),
              const SizedBox(height: 4),
              SelectableText(
                issued.pairingCode,
                key: const ValueKey('companion-pairing-code'),
                style: theme.textTheme.bodyLarge?.copyWith(
                  fontFamily: 'monospace',
                  fontWeight: FontWeight.w600,
                ),
              ),
              const SizedBox(height: 4),
              Text(
                'Expires in ${remaining}s · RAM-only (never stored or logged)',
                key: const ValueKey('companion-pairing-expiry'),
                style: theme.textTheme.bodySmall,
              ),
              const SizedBox(height: 8),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  OutlinedButton.icon(
                    key: const ValueKey('companion-copy-pairing-code'),
                    onPressed: _copyPairingCode,
                    icon: const Icon(Icons.copy_outlined, size: 18),
                    label: const Text('Copy pairing code'),
                  ),
                ],
              ),
            ],
          ],
        ),
      ),
    );
  }

  Widget _buildDeviceCard(BuildContext context, OwnerCompanionDevice device) {
    final theme = Theme.of(context);
    final inCurrentBinding = device.matchesBinding(_binding);
    final canInvite = device.canInviteFor(_binding);
    final hasOpenCall = _calls.any(
      (call) => call.deviceId == device.deviceId && !call.state.terminal,
    );
    final pendingInvite = _currentUncertainty?.invite;
    return Card(
      key: ValueKey('companion-device-${device.deviceId}'),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Wrap(
              spacing: 8,
              runSpacing: 4,
              alignment: WrapAlignment.spaceBetween,
              crossAxisAlignment: WrapCrossAlignment.center,
              children: [
                Text(
                  device.deviceName,
                  style: theme.textTheme.titleMedium?.copyWith(
                    fontWeight: FontWeight.w600,
                  ),
                ),
                Chip(
                  key: ValueKey('companion-device-status-${device.deviceId}'),
                  label: Text(
                    inCurrentBinding
                        ? device.status.label
                        : '${device.status.label} · other conversation',
                  ),
                ),
              ],
            ),
            const SizedBox(height: 4),
            Text(
              'Label: ${device.label} · Session ${device.binding.sessionId} (epoch ${device.binding.epoch})',
              style: theme.textTheme.bodySmall,
            ),
            if (!inCurrentBinding) ...[
              const SizedBox(height: 6),
              Text(
                'Bound to another Converse session or epoch. Read-only here except owner revocation.',
                key: ValueKey('companion-device-readonly-${device.deviceId}'),
                style: theme.textTheme.bodySmall,
              ),
            ],
            if (canInvite) ...[
              const SizedBox(height: 12),
              TextField(
                key: ValueKey('companion-invite-title-${device.deviceId}'),
                controller: _inviteControllerFor(device),
                enabled:
                    !_acting &&
                    !_issuingPairing &&
                    !hasOpenCall &&
                    pendingInvite == null,
                maxLength: 120,
                decoration: const InputDecoration(
                  labelText: 'Invitation title',
                  hintText: 'Companion call',
                ),
              ),
            ],
            const SizedBox(height: 8),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                if (canInvite)
                  FilledButton(
                    key: ValueKey('companion-invite-${device.deviceId}'),
                    onPressed:
                        !_acting &&
                            !_issuingPairing &&
                            !hasOpenCall &&
                            pendingInvite == null
                        ? () => unawaited(_inviteDevice(device))
                        : null,
                    child: Text(
                      hasOpenCall ? 'Invitation open' : 'Invite companion',
                    ),
                  ),
                if (pendingInvite != null &&
                    pendingInvite.deviceId == device.deviceId)
                  OutlinedButton(
                    key: const ValueKey('companion-retry-invite'),
                    onPressed:
                        !_acting &&
                            !_issuingPairing &&
                            _currentUncertainty?.inFlight.contains(
                                  pendingInvite,
                                ) !=
                                true
                        ? () => unawaited(_retryUncertainInvite())
                        : null,
                    child: const Text('Retry unconfirmed invite'),
                  ),
                OutlinedButton(
                  key: ValueKey('companion-revoke-${device.deviceId}'),
                  onPressed:
                      _ownsClient &&
                          !_acting &&
                          !_issuingPairing &&
                          device.canRevoke
                      ? () => unawaited(_revokeDevice(device))
                      : null,
                  child: Text(device.canRevoke ? 'Revoke' : 'Revoked'),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildCallCard(BuildContext context, OwnerCompanionCall call) {
    final theme = Theme.of(context);
    final uncertainty = _currentUncertainty;
    final pendingEnd = uncertainty?.ends[call.callId];
    final pendingNote = uncertainty?.notes[call.callId];
    final noteFenced =
        pendingNote != null && pendingNote.needsGetReconciliation;
    final canEditNote = _canEditCallNote(call);
    final noteController = _noteControllers.putIfAbsent(
      call.callId,
      () => TextEditingController(text: call.note),
    );

    return Card(
      key: ValueKey('companion-call-${call.callId}'),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Wrap(
              spacing: 8,
              runSpacing: 4,
              alignment: WrapAlignment.spaceBetween,
              crossAxisAlignment: WrapCrossAlignment.center,
              children: [
                Text(
                  call.title,
                  style: theme.textTheme.titleMedium?.copyWith(
                    fontWeight: FontWeight.w600,
                  ),
                ),
                Text(
                  'Rev ${call.revision} · #${call.sequence}',
                  style: theme.textTheme.bodySmall,
                ),
              ],
            ),
            const SizedBox(height: 6),
            Text(
              call.state.label,
              key: ValueKey('companion-call-state-${call.callId}'),
              style: theme.textTheme.bodyMedium?.copyWith(
                fontWeight: FontWeight.w600,
              ),
            ),
            if (call.state == OwnerCompanionCallState.acceptedWaitingMedia) ...[
              const SizedBox(height: 4),
              Text(
                'Answer does not connect audio. Preserves 15s media_unavailable fallback while voice transport is unimplemented.',
                key: ValueKey('companion-call-waiting-note-${call.callId}'),
                style: theme.textTheme.bodySmall,
              ),
            ],
            if (call.endReasonLabel != null) ...[
              const SizedBox(height: 4),
              Text(
                call.endReasonLabel!,
                key: ValueKey('companion-call-reason-${call.callId}'),
                style: theme.textTheme.bodySmall,
              ),
            ],
            if (call.note.isNotEmpty) ...[
              const Divider(height: 20),
              Text(
                'Retained literal note',
                style: theme.textTheme.labelMedium?.copyWith(
                  fontWeight: FontWeight.w600,
                ),
              ),
              const SizedBox(height: 4),
              SelectableText(
                call.note,
                key: ValueKey('companion-call-note-${call.callId}'),
              ),
            ],
            if (canEditNote) ...[
              const SizedBox(height: 12),
              TextField(
                key: ValueKey('companion-note-input-${call.callId}'),
                controller: noteController,
                enabled: !_acting && !_issuingPairing && !noteFenced,
                maxLines: 3,
                minLines: 1,
                onChanged: (_) => _dirtyNoteCallIds.add(call.callId),
                decoration: InputDecoration(
                  labelText: 'Literal call note',
                  helperText: noteFenced
                      ? 'Reconcile this call (Inspect) before another note edit.'
                      : 'Plain literal text only (up to 4096 UTF-8 bytes).',
                ),
              ),
            ],
            const SizedBox(height: 12),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                if (canEditNote)
                  FilledButton.tonal(
                    key: ValueKey('companion-save-note-${call.callId}'),
                    onPressed: !_acting && !_issuingPairing && !noteFenced
                        ? () => unawaited(_saveNote(call))
                        : null,
                    child: const Text('Save note'),
                  ),
                if (!call.state.terminal)
                  OutlinedButton(
                    key: ValueKey('companion-end-${call.callId}'),
                    onPressed:
                        !_acting && !_issuingPairing && pendingEnd == null
                        ? () => unawaited(_endCall(call))
                        : null,
                    child: const Text('End call'),
                  ),
                if (pendingEnd != null)
                  OutlinedButton(
                    key: ValueKey('companion-retry-end-${call.callId}'),
                    onPressed:
                        !_acting &&
                            !_issuingPairing &&
                            uncertainty?.inFlight.contains(pendingEnd) != true
                        ? () => unawaited(_retryUncertainEnd(call.callId))
                        : null,
                    child: const Text('Retry unconfirmed end'),
                  ),
                TextButton(
                  key: ValueKey('companion-inspect-${call.callId}'),
                  onPressed: _ownsScope && !_acting && !_issuingPairing
                      ? () => unawaited(_inspectCall(call))
                      : null,
                  child: Text(noteFenced ? 'Reconcile call' : 'Inspect call'),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildBody(BuildContext context) {
    final theme = Theme.of(context);
    return SingleChildScrollView(
      key: const ValueKey('companion-panel-scroll'),
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        mainAxisSize: MainAxisSize.min,
        children: [
          Wrap(
            spacing: 8,
            runSpacing: 8,
            alignment: WrapAlignment.spaceBetween,
            crossAxisAlignment: WrapCrossAlignment.center,
            children: [
              Semantics(
                header: true,
                child: Text(
                  'Companions',
                  style: theme.textTheme.headlineSmall?.copyWith(
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ),
              TextButton.icon(
                key: const ValueKey('companion-refresh'),
                onPressed: _refreshing ? null : _manualRefresh,
                icon: const Icon(Icons.refresh, size: 18),
                label: Text(_refreshing ? 'Refreshing…' : 'Refresh'),
              ),
            ],
          ),
          const SizedBox(height: 4),
          Text(
            'Foreground companion pairing, device directory, and call signaling.',
            style: theme.textTheme.bodySmall,
          ),
          if (_notice != null) ...[
            const SizedBox(height: 12),
            Semantics(
              liveRegion: true,
              child: Card(
                color: theme.colorScheme.surfaceContainerHighest,
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Text(
                    _notice!,
                    key: const ValueKey('companion-notice'),
                  ),
                ),
              ),
            ),
          ],
          if (_client == null) ...[
            const SizedBox(height: 16),
            const Text(
              'Connect to the Session service first to inspect or manage companions.',
              key: ValueKey('companion-disconnected'),
            ),
          ] else ...[
            const SizedBox(height: 16),
            _buildPairingSection(context),
            const SizedBox(height: 20),
            Semantics(
              header: true,
              child: Text(
                'Paired companion devices (${_devices.length}/$ownerCompanionMaxDevices)',
                style: theme.textTheme.titleMedium?.copyWith(
                  fontWeight: FontWeight.w700,
                ),
              ),
            ),
            const SizedBox(height: 8),
            if (_devicesError != null)
              Text(
                _devicesError!,
                key: const ValueKey('companion-devices-error'),
                style: TextStyle(color: theme.colorScheme.error),
              )
            else if (_devicesLoaded && _devices.isEmpty)
              const Text(
                'No companion devices are registered on this host.',
                key: ValueKey('companion-devices-empty'),
              )
            else
              ..._devices.map((device) => _buildDeviceCard(context, device)),
            const SizedBox(height: 20),
            Semantics(
              header: true,
              child: Text(
                'Current conversation invitations',
                style: theme.textTheme.titleMedium?.copyWith(
                  fontWeight: FontWeight.w700,
                ),
              ),
            ),
            const SizedBox(height: 8),
            if (_binding == null)
              const Text(
                'No active Converse session binding is selected.',
                key: ValueKey('companion-calls-no-binding'),
              )
            else if (_callsError != null)
              Text(
                _callsError!,
                key: const ValueKey('companion-calls-error'),
                style: TextStyle(color: theme.colorScheme.error),
              )
            else if (_callsLoaded && _calls.isEmpty)
              const Text(
                'No companion invitations recorded for this Converse session.',
                key: ValueKey('companion-calls-empty'),
              )
            else
              ..._calls.map((call) => _buildCallCard(context, call)),
          ],
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final content = _buildBody(context);
    if (widget.embedded) return content;
    final media = MediaQuery.of(context);
    return Dialog(
      insetPadding: const EdgeInsets.all(12),
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(KinaiyaDesign.radius),
      ),
      child: ConstrainedBox(
        constraints: BoxConstraints(
          maxWidth: 760,
          maxHeight: (media.size.height - 24).clamp(180.0, 900.0),
        ),
        child: CallbackShortcuts(
          bindings: <ShortcutActivator, VoidCallback>{
            const SingleActivator(LogicalKeyboardKey.escape): () =>
                Navigator.of(context).maybePop(),
          },
          child: Focus(
            autofocus: true,
            child: Semantics(
              namesRoute: true,
              scopesRoute: true,
              explicitChildNodes: true,
              label: 'Companions',
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Flexible(child: content),
                  const Divider(height: 1),
                  Padding(
                    padding: const EdgeInsets.fromLTRB(16, 6, 16, 6),
                    child: Align(
                      alignment: Alignment.centerRight,
                      child: TextButton(
                        key: const ValueKey('companion-close'),
                        onPressed: () => Navigator.of(context).maybePop(),
                        child: const Text('Close'),
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}
