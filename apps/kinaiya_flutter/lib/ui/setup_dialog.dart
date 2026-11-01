import 'package:flutter/material.dart';
import 'package:flutter/foundation.dart';

import '../config/connection.dart';
import '../config/credential_picker.dart';
import '../state/app_state.dart';

/// Connection setup: service URLs plus an owner access token password field.
/// Everything stays in memory; nothing is persisted, logged or URL-encoded.
class SetupDialog extends StatefulWidget {
  const SetupDialog({
    super.key,
    required this.state,
    this.embedded = false,
    this.privacyOnly = false,
  });
  final AppState state;
  final bool embedded, privacyOnly;

  @override
  State<SetupDialog> createState() => _SetupDialogState();
}

class _SetupDialogState extends State<SetupDialog> {
  late final TextEditingController _session;
  late final TextEditingController _agent;
  late final TextEditingController _converse;
  late final TextEditingController _decisions;
  late final TextEditingController _token;
  BackendProfile _profile = BackendProfile.clean;
  String? _error;

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
  }

  @override
  void dispose() {
    _session.dispose();
    _agent.dispose();
    _converse.dispose();
    _decisions.dispose();
    _token.dispose();
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
              widget.embedded &&
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
      setState(() => _error = problem);
      return;
    }
    widget.state.connect(profile);
    if (!widget.embedded) {
      Navigator.of(context).pop();
    } else {
      setState(() => _error = null);
    }
  }

  @override
  Widget build(BuildContext context) {
    final form = SingleChildScrollView(
      padding: const EdgeInsets.only(top: 8),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (!widget.privacyOnly) ...[
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
              decoration: const InputDecoration(labelText: 'Agent service URL'),
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
                helperText: 'Memory only: never stored, logged or put in URLs.',
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
                      _error = null;
                    });
                  }
                },
                icon: const Icon(Icons.folder_open),
                label: const Text('Load local access token'),
              ),
            ],
          ],
          const SizedBox(height: 12),
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
            child: Text(
              _profile.note,
              style: Theme.of(context).textTheme.bodySmall,
            ),
          ),
          if (_error != null)
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: Semantics(
                liveRegion: true,
                child: Text(_error!, style: const TextStyle(color: Colors.red)),
              ),
            ),
        ],
      ),
    );
    if (widget.embedded) {
      return Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Expanded(child: form),
          const SizedBox(height: 16),
          Align(
            alignment: Alignment.centerLeft,
            child: FilledButton(
              onPressed: _connect,
              child: Text(
                widget.privacyOnly ? 'Apply disclosure choice' : 'Connect',
              ),
            ),
          ),
          const SizedBox(height: 12),
          Text(widget.state.connectionError ?? 'Not connected'),
          const SizedBox(height: 8),
          Text(widget.state.decisionsModelOptionsStatus),
        ],
      );
    }
    return AlertDialog(
      title: const Text('Connect to Kinaiya services'),
      content: form,
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('Cancel'),
        ),
        FilledButton(onPressed: _connect, child: const Text('Connect')),
      ],
    );
  }
}
