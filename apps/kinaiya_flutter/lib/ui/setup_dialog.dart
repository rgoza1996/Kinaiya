import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../config/connection.dart';
import '../config/credential_picker.dart';
import '../state/app_state.dart';
import 'visual_theme.dart';
import 'widgets.dart';

/// Connection setup: trusted local auto-connect status plus advanced manual
/// service URLs and owner access token field.
/// Everything stays in memory; nothing is persisted, logged or URL-encoded.
class SetupDialog extends StatefulWidget {
  const SetupDialog({
    super.key,
    required this.state,
    this.embedded = false,
    this.privacyOnly = false,
    this.localSignInExchange,
  });
  final AppState state;
  final bool embedded, privacyOnly;
  final LocalSignInExchange? localSignInExchange;

  @override
  State<SetupDialog> createState() => _SetupDialogState();
}

class _SetupDialogState extends State<SetupDialog> {
  late final TextEditingController _session;
  late final TextEditingController _agent;
  late final TextEditingController _converse;
  late final TextEditingController _decisions;
  late final TextEditingController _token;
  final FocusNode _connectFocusNode = FocusNode(
    debugLabel: 'setup-connect-primary-action',
  );
  final FocusNode _okFocusNode = FocusNode(
    debugLabel: 'setup-validation-alert-ok',
  );
  final FocusScopeNode _alertFocusScopeNode = FocusScopeNode(
    debugLabel: 'setup-validation-alert-scope',
  );
  FocusNode? _triggerFocusNode;
  BackendProfile _profile = BackendProfile.clean;
  String? _validationModalMessage;
  late bool _showServiceEndpointFields;
  bool _showAdvanced = false;

  @override
  void initState() {
    super.initState();
    final c = widget.state.connection ?? ConnectionProfile.defaults();
    _session = TextEditingController(text: c.sessionUrl);
    _agent = TextEditingController(text: c.agentUrl);
    _converse = TextEditingController(text: c.converseUrl);
    _decisions = TextEditingController(text: c.decisionsUrl ?? '');
    _token = TextEditingController();
    _profile = c.backendProfile;
    final isLocalFirstRun =
        widget.localSignInExchange != null ||
        widget.state.isTrustedLocalFirstRun ||
        kIsWeb;
    _showServiceEndpointFields = !isLocalFirstRun;
    if ((widget.localSignInExchange != null ||
            widget.state.localSignInExchange != null) &&
        widget.state.localSignInState == LocalHostSignInState.idle &&
        !widget.state.connected) {
      unawaited(_retryLocalSignIn());
    }
  }

  Future<void> _retryLocalSignIn() async {
    if (mounted) {
      setState(() => _validationModalMessage = null);
    }
    await widget.state.attemptLocalSignIn(
      exchange: widget.localSignInExchange,
      backendProfile: _profile,
    );
    if (!mounted) return;
    final c = widget.state.connection;
    if (c != null) {
      _session.text = c.sessionUrl;
      _agent.text = c.agentUrl;
      _converse.text = c.converseUrl;
      _decisions.text = c.decisionsUrl ?? '';
    }
  }

  @override
  void dispose() {
    _session.dispose();
    _agent.dispose();
    _converse.dispose();
    _decisions.dispose();
    _token.dispose();
    _connectFocusNode.dispose();
    _okFocusNode.dispose();
    _alertFocusScopeNode.dispose();
    super.dispose();
  }

  void _connect() {
    final profile = ConnectionProfile(
      sessionUrl: _session.text,
      agentUrl: _agent.text,
      converseUrl: _converse.text,
      decisionsUrl: _decisions.text,
      ownerToken:
          _token.text.isEmpty &&
              (widget.state.connection?.ownerToken.isNotEmpty ?? false) &&
              _session.text == widget.state.connection?.sessionUrl &&
              _agent.text == widget.state.connection?.agentUrl &&
              _converse.text == widget.state.connection?.converseUrl &&
              _decisions.text.trim() ==
                  (widget.state.connection?.decisionsUrl ?? '')
          ? widget.state.connection!.ownerToken
          : _token.text,
      backendProfile: _profile,
    );
    final problem = profile.validate();
    if (problem != null) {
      _triggerFocusNode = _connectFocusNode;
      setState(() => _validationModalMessage = problem);
      _okFocusNode.requestFocus();
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted && _validationModalMessage != null) {
          _okFocusNode.requestFocus();
        }
      });
      return;
    }
    widget.state.connect(profile);
    if (!widget.embedded) {
      Navigator.of(context).pop();
    } else {
      setState(() => _validationModalMessage = null);
    }
  }

  void _dismissValidationAlert() {
    if (_validationModalMessage == null) return;
    final restoreNode = _triggerFocusNode ?? _connectFocusNode;
    setState(() => _validationModalMessage = null);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted &&
          _validationModalMessage == null &&
          restoreNode.canRequestFocus) {
        restoreNode.requestFocus();
      }
    });
  }

  String get _connectionStatusText {
    final error = widget.state.connectionError;
    if (error != null && error.isNotEmpty) return error;
    if (!widget.state.connected) return 'Not connected';
    if (!widget.state.sessionAuthenticated) {
      return 'Connection pending or unauthenticated — host session not yet verified.';
    }
    return 'Connected and authenticated with host.';
  }

  ActionOutcomeState get _connectionOutcomeState {
    final error = widget.state.connectionError;
    if (error == 'Checking services…') return ActionOutcomeState.inProgress;
    if (widget.state.connected && widget.state.sessionAuthenticated) {
      return ActionOutcomeState.confirmed;
    }
    if ((widget.state.connected && !widget.state.sessionAuthenticated) ||
        (widget.state.sessionAuthenticated &&
            !widget.state.connected &&
            !widget.state.agentOnline &&
            !widget.state.converseOnline)) {
      return ActionOutcomeState.unconfirmed;
    }
    if (error != null &&
        error.isNotEmpty &&
        error != 'Connection is not configured yet.') {
      return ActionOutcomeState.failed;
    }
    return ActionOutcomeState.idle;
  }

  bool get _hasExistingMemoryToken =>
      widget.embedded &&
      (widget.state.connection?.ownerToken.isNotEmpty ?? false);

  Widget _buildLocalHostCard(BuildContext context) {
    final textTheme = Theme.of(context).textTheme;
    final scheme = Theme.of(context).colorScheme;
    final isConnecting =
        widget.state.localSignInState == LocalHostSignInState.connecting ||
        widget.state.connectionError == 'Checking services…';
    final isConnected =
        widget.state.connected && widget.state.sessionAuthenticated;

    return Container(
      key: const Key('local-host-connection-card'),
      margin: const EdgeInsets.only(bottom: 12),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: scheme.surfaceContainerLow,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: scheme.outlineVariant),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'Local Host Connection',
            style: textTheme.titleSmall?.copyWith(fontWeight: FontWeight.w600),
          ),
          const SizedBox(height: 6),
          if (isConnecting)
            Text(
              'Connecting to local Kinaiya host...',
              key: const Key('local-host-status-connecting'),
              style: textTheme.bodyMedium,
            )
          else if (isConnected) ...[
            Text(
              'Connected to local Kinaiya host',
              key: const Key('local-host-status-connected'),
              style: textTheme.bodyMedium?.copyWith(
                fontWeight: FontWeight.w600,
              ),
            ),
            const SizedBox(height: 4),
            Text(
              widget.state.hostRoleStatusSummary,
              key: const Key('local-host-role-summary'),
              style: textTheme.bodySmall?.copyWith(
                color: scheme.onSurfaceVariant,
              ),
            ),
          ] else ...[
            Text(
              'Local automatic sign-in unavailable',
              key: const Key('local-host-status-unavailable'),
              style: textTheme.bodyMedium,
            ),
            const SizedBox(height: 4),
            Text(
              widget.state.sessionAuthenticated && !widget.state.agentOnline
                  ? 'Agent host unreachable'
                  : (widget.state.localSignInError ??
                        'Retry automatic local sign-in or open Advanced connection to enter custom URLs or a manual owner token.'),
              key: const Key('local-host-role-summary'),
              style: textTheme.bodySmall?.copyWith(
                color: scheme.onSurfaceVariant,
              ),
            ),
          ],
          const SizedBox(height: 8),
          Wrap(
            spacing: 8,
            runSpacing: 4,
            children: [
              if (!isConnecting && !isConnected)
                OutlinedButton.icon(
                  key: const Key('local-host-retry-signin'),
                  onPressed: _retryLocalSignIn,
                  icon: const Icon(Icons.refresh, size: 18),
                  label: const Text('Retry local sign-in'),
                ),
              TextButton.icon(
                key: const Key('setup-toggle-endpoint-fields'),
                onPressed: () => setState(
                  () =>
                      _showServiceEndpointFields = !_showServiceEndpointFields,
                ),
                icon: Icon(
                  _showServiceEndpointFields
                      ? Icons.expand_less
                      : Icons.expand_more,
                  size: 18,
                ),
                label: Text(
                  _showServiceEndpointFields
                      ? 'Hide advanced connection'
                      : 'Advanced connection',
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }

  String get _endpointsSummary {
    final session = _session.text.trim();
    final agent = _agent.text.trim();
    final converse = _converse.text.trim();
    final decisions = _decisions.text.trim();
    return 'Session: ${session.isEmpty ? 'unset' : session} · '
        'Agent: ${agent.isEmpty ? 'unset' : agent} · '
        'Converse: ${converse.isEmpty ? 'unset' : converse} · '
        'Decisions: ${decisions.isEmpty ? 'not configured' : decisions}';
  }

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: widget.state,
    builder: (context, _) {
      final textTheme = Theme.of(context).textTheme;
      final scheme = Theme.of(context).colorScheme;
      final form = SingleChildScrollView(
        padding: const EdgeInsets.only(top: 8),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              widget.privacyOnly
                  ? 'Choose whether Kinaiya may use only local/self-hosted models or also allow configured cloud providers.'
                  : 'Connect to your Kinaiya host using its service URLs and your owner access token. Credentials stay in memory only for this session.',
              key: const ValueKey('setup-user-guide'),
              style: textTheme.bodyMedium?.copyWith(
                color: scheme.onSurfaceVariant,
              ),
            ),
            const SizedBox(height: 12),
            if (!widget.privacyOnly) ...[
              _buildLocalHostCard(context),
              Wrap(
                alignment: WrapAlignment.spaceBetween,
                crossAxisAlignment: WrapCrossAlignment.center,
                spacing: 8,
                runSpacing: 4,
                children: [
                  Text(
                    'Host service endpoints & owner token',
                    style: textTheme.titleSmall?.copyWith(
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  TextButton.icon(
                    key: const ValueKey('setup-endpoints-toggle'),
                    onPressed: () => setState(
                      () => _showServiceEndpointFields =
                          !_showServiceEndpointFields,
                    ),
                    icon: Icon(
                      _showServiceEndpointFields
                          ? Icons.expand_less
                          : Icons.expand_more,
                      size: 18,
                    ),
                    label: Text(
                      _showServiceEndpointFields
                          ? 'Collapse service URLs'
                          : 'Edit service URLs',
                    ),
                  ),
                ],
              ),
              if (!_showServiceEndpointFields) ...[
                Padding(
                  padding: const EdgeInsets.only(bottom: 12),
                  child: Text(
                    _endpointsSummary,
                    key: const ValueKey('setup-endpoints-summary'),
                    style: textTheme.bodySmall?.copyWith(
                      color: scheme.onSurfaceVariant,
                    ),
                  ),
                ),
              ] else ...[
                const SizedBox(height: 8),
                TextField(
                  controller: _session,
                  decoration: const InputDecoration(
                    labelText: 'Session service URL',
                  ),
                  keyboardType: TextInputType.url,
                ),
                const SizedBox(height: 16),
                TextField(
                  controller: _agent,
                  decoration: const InputDecoration(
                    labelText: 'Agent service URL',
                  ),
                  keyboardType: TextInputType.url,
                ),
                const SizedBox(height: 16),
                TextField(
                  controller: _converse,
                  decoration: const InputDecoration(
                    labelText: 'Converse service URL',
                  ),
                  keyboardType: TextInputType.url,
                ),
                const SizedBox(height: 16),
                TextField(
                  controller: _decisions,
                  decoration: const InputDecoration(
                    labelText: 'Decisions service URL (optional)',
                    helperText:
                        'Use its explicit independent URL. Blank leaves Decisions model options unconfigured.',
                  ),
                  keyboardType: TextInputType.url,
                ),
                const SizedBox(height: 16),
                TextField(
                  controller: _token,
                  decoration: const InputDecoration(
                    labelText: 'Owner access token',
                    helperText:
                        'Export with kinaiya --data-dir <dir> owner-login (owner-login.txt) or use Load local access token. Memory only: never stored, logged, or put in URLs.',
                  ),
                  obscureText: true,
                  enableSuggestions: false,
                  autocorrect: false,
                ),
                if (kIsWeb) ...[
                  const SizedBox(height: 8),
                  TextButton.icon(
                    onPressed: () async {
                      final token = await chooseCredential();
                      if (mounted && token != null) {
                        setState(() {
                          _token.text = token;
                          _validationModalMessage = null;
                        });
                      }
                    },
                    icon: const Icon(Icons.folder_open),
                    label: const Text('Load local access token'),
                  ),
                ],
              ],
              if (_hasExistingMemoryToken && _token.text.isEmpty)
                Padding(
                  padding: const EdgeInsets.only(top: 6),
                  child: Text(
                    'An owner token is currently held in memory for the active URLs. Leave blank to keep it when reconnecting to the same URLs, or enter a new token.',
                    key: const ValueKey('setup-existing-token-hint'),
                    style: textTheme.bodySmall?.copyWith(
                      color: scheme.onSurfaceVariant,
                    ),
                  ),
                ),
            ],
            const SizedBox(height: 12),
            Text(
              'Data-sharing & inference disclosure',
              key: const ValueKey('setup-privacy-guide'),
              style: textTheme.titleSmall?.copyWith(
                fontWeight: FontWeight.w600,
              ),
            ),
            const SizedBox(height: 6),
            DropdownButtonFormField<BackendProfile>(
              initialValue: _profile,
              isExpanded: true,
              decoration: const InputDecoration(
                labelText: 'Inference disclosure',
              ),
              items: BackendProfile.values
                  .map((p) => DropdownMenuItem(value: p, child: Text(p.label)))
                  .toList(),
              onChanged: (p) =>
                  setState(() => _profile = p ?? BackendProfile.clean),
            ),
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: Text(_profile.note, style: textTheme.bodySmall),
            ),
            const SizedBox(height: 12),
            Align(
              alignment: Alignment.centerLeft,
              child: TextButton.icon(
                key: const ValueKey('setup-advanced-toggle'),
                onPressed: () => setState(() => _showAdvanced = !_showAdvanced),
                icon: Icon(
                  _showAdvanced ? Icons.expand_less : Icons.expand_more,
                  size: 18,
                ),
                label: Text(
                  _showAdvanced
                      ? 'Hide advanced connection & protocol details'
                      : 'Advanced connection & protocol details',
                ),
              ),
            ),
            if (_showAdvanced)
              Container(
                key: const ValueKey('setup-advanced-panel'),
                margin: const EdgeInsets.only(top: 4),
                padding: const EdgeInsets.all(12),
                decoration: BoxDecoration(
                  color: scheme.surfaceContainerLow,
                  borderRadius: BorderRadius.circular(12),
                  border: Border.all(color: scheme.outlineVariant),
                ),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      'Session authentication: ${widget.state.sessionAuthenticated ? 'Verified' : 'Not verified'}',
                      style: textTheme.bodySmall,
                    ),
                    const SizedBox(height: 4),
                    Text(
                      'Service reachability: Agent ${widget.state.agentOnline ? 'reachable' : 'Agent host unreachable'} · Converse ${widget.state.converseOnline ? 'reachable' : 'Converse optional / not enabled'}',
                      style: textTheme.bodySmall,
                    ),
                    const SizedBox(height: 4),
                    Text(
                      widget.state.decisionsModelOptionsStatus,
                      style: textTheme.bodySmall,
                    ),
                    const SizedBox(height: 4),
                    Text(
                      'Connection generation: ${widget.state.connectionGeneration}',
                      style: textTheme.bodySmall,
                    ),
                    const SizedBox(height: 4),
                    Text(
                      'Protocol security: Authenticated requests require loopback (127.0.0.1 / localhost / ::1) or HTTPS. Owner tokens remain in RAM only and are never written to disk, URLs, or logs.',
                      style: textTheme.bodySmall?.copyWith(
                        color: scheme.onSurfaceVariant,
                      ),
                    ),
                  ],
                ),
              ),
          ],
        ),
      );
      final hasValidationAlert = _validationModalMessage != null;
      final Widget dialogBody = widget.embedded
          ? Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Expanded(child: form),
                const SizedBox(height: 16),
                Align(
                  alignment: Alignment.centerLeft,
                  child: FilledButton(
                    key: const ValueKey('setup-connect-primary-action'),
                    focusNode: _connectFocusNode,
                    style: kinaiyaPrimaryButtonStyle(
                      Theme.of(context).colorScheme,
                    ),
                    onPressed: _connect,
                    child: Text(
                      widget.privacyOnly
                          ? 'Apply disclosure choice'
                          : 'Connect',
                    ),
                  ),
                ),
                const SizedBox(height: 12),
                ActionOutcomeFeedback(
                  key: const ValueKey('setup-connection-outcome'),
                  messageKey: const ValueKey('setup-connection-status'),
                  state: _connectionOutcomeState,
                  message: _connectionStatusText,
                ),
              ],
            )
          : AlertDialog(
              title: const Text('Connect to Kinaiya services'),
              content: form,
              actions: [
                TextButton(
                  onPressed: () => Navigator.of(context).pop(),
                  child: const Text('Cancel'),
                ),
                FilledButton(
                  key: const ValueKey('setup-connect-primary-action'),
                  focusNode: _connectFocusNode,
                  onPressed: _connect,
                  child: const Text('Connect'),
                ),
              ],
            );
      return Stack(
        children: [
          ExcludeSemantics(
            excluding: hasValidationAlert,
            child: ExcludeFocus(
              excluding: hasValidationAlert,
              child: IgnorePointer(
                ignoring: hasValidationAlert,
                child: dialogBody,
              ),
            ),
          ),
          if (hasValidationAlert)
            _buildValidationAlertOverlay(scheme, textTheme),
        ],
      );
    },
  );

  Widget _buildValidationAlertOverlay(
    ColorScheme scheme,
    TextTheme textTheme,
  ) => Positioned.fill(
    child: CallbackShortcuts(
      bindings: <ShortcutActivator, VoidCallback>{
        const SingleActivator(LogicalKeyboardKey.escape):
            _dismissValidationAlert,
      },
      child: FocusScope(
        node: _alertFocusScopeNode,
        child: Stack(
          alignment: Alignment.center,
          children: [
            const Positioned.fill(
              child: ModalBarrier(
                key: ValueKey('setup-validation-alert-barrier'),
                dismissible: false,
                color: Colors.black54,
              ),
            ),
            AlertDialog(
              key: const ValueKey('setup-validation-alert-dialog'),
              title: Row(
                children: [
                  Icon(Icons.warning_amber_rounded, color: scheme.error),
                  const SizedBox(width: 8),
                  const Expanded(child: Text('Cannot connect to Kinaiya host')),
                ],
              ),
              content: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 440),
                child: Semantics(
                  liveRegion: true,
                  child: Text(
                    _validationModalMessage!,
                    key: const ValueKey('setup-validation-error'),
                    style: textTheme.bodyMedium,
                  ),
                ),
              ),
              actions: [
                FilledButton(
                  key: const ValueKey('setup-validation-alert-ok'),
                  focusNode: _okFocusNode,
                  autofocus: true,
                  style: kinaiyaPrimaryButtonStyle(scheme),
                  onPressed: _dismissValidationAlert,
                  child: const Text('OK'),
                ),
              ],
            ),
          ],
        ),
      ),
    ),
  );
}
