import 'package:flutter/material.dart';

import '../config/connection.dart';
import '../state/app_state.dart';

/// Connection setup: service URLs plus an owner Bearer [REDACTED] password field.
/// Everything stays in memory; nothing is persisted, logged or URL-encoded.
class SetupDialog extends StatefulWidget {
  const SetupDialog({super.key, required this.state});
  final AppState state;

  @override
  State<SetupDialog> createState() => _SetupDialogState();
}

class _SetupDialogState extends State<SetupDialog> {
  late final TextEditingController _session;
  late final TextEditingController _agent;
  late final TextEditingController _converse;
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
    _token = TextEditingController();
    _profile = c.backendProfile;
  }

  @override
  void dispose() {
    _session.dispose();
    _agent.dispose();
    _converse.dispose();
    _token.dispose();
    super.dispose();
  }

  void _connect() {
    final profile = ConnectionProfile(
      sessionUrl: _session.text,
      agentUrl: _agent.text,
      converseUrl: _converse.text,
      ownerToken: _token.text,
      backendProfile: _profile,
    );
    final problem = profile.validate();
    if (problem != null) {
      setState(() => _error = problem);
      return;
    }
    widget.state.connect(profile);
    Navigator.of(context).pop();
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('Connect to Kinaiya services'),
      content: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            TextField(
              controller: _session,
              decoration: const InputDecoration(
                labelText: 'Session service URL',
              ),
              keyboardType: TextInputType.url,
            ),
            TextField(
              controller: _agent,
              decoration: const InputDecoration(labelText: 'Agent service URL'),
              keyboardType: TextInputType.url,
            ),
            TextField(
              controller: _converse,
              decoration: const InputDecoration(
                labelText: 'Converse service URL',
              ),
              keyboardType: TextInputType.url,
            ),
            TextField(
              controller: _token,
              decoration: const InputDecoration(
                labelText: 'Owner Bearer [REDACTED]',
                helperText: 'Memory only: never stored, logged or put in URLs.',
              ),
              obscureText: true,
              enableSuggestions: false,
              autocorrect: false,
            ),
            const SizedBox(height: 12),
            DropdownButtonFormField<BackendProfile>(
              initialValue: _profile,
              decoration: const InputDecoration(labelText: 'Backend profile'),
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
                  child: Text(
                    _error!,
                    style: const TextStyle(color: Colors.red),
                  ),
                ),
              ),
          ],
        ),
      ),
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
