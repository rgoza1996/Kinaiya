import 'dart:async';

import 'package:flutter/material.dart';

import 'controller.dart';
import 'models.dart';

class CompanionApp extends StatelessWidget {
  const CompanionApp({super.key, this.controller});
  final CompanionController? controller;

  @override
  Widget build(BuildContext context) => MaterialApp(
    title: 'Kinaiya Companion',
    theme: ThemeData(colorSchemeSeed: const Color(0xFF141091)),
    home: CompanionHome(controller: controller),
  );
}

class CompanionHome extends StatefulWidget {
  const CompanionHome({super.key, this.controller});
  final CompanionController? controller;
  @override
  State<CompanionHome> createState() => _CompanionHomeState();
}

class _CompanionHomeState extends State<CompanionHome>
    with WidgetsBindingObserver {
  late CompanionController _state;
  late bool _ownsController;
  final _host = TextEditingController();
  final _code = TextEditingController();
  final _name = TextEditingController(text: 'My companion');
  final _form = GlobalKey<FormState>();

  @override
  void initState() {
    super.initState();
    _ownsController = widget.controller == null;
    _state = widget.controller ?? CompanionController();
    WidgetsBinding.instance.addObserver(this);
  }

  @override
  void didUpdateWidget(CompanionHome oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.controller != oldWidget.controller) {
      if (_ownsController) _state.dispose();
      _ownsController = widget.controller == null;
      _state = widget.controller ?? CompanionController();
      _state.setForeground(
        WidgetsBinding.instance.lifecycleState == AppLifecycleState.resumed,
      );
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) =>
      _state.setForeground(state == AppLifecycleState.resumed);

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    if (_ownsController) _state.dispose();
    _host.dispose();
    _code.dispose();
    _name.dispose();
    super.dispose();
  }

  Future<void> _pair() async {
    if (!_form.currentState!.validate()) return;
    final code = _code.text.trim();
    _code.clear();
    FocusScope.of(context).unfocus();
    await _state.pair(_host.text.trim(), code, _name.text.trim());
  }

  Widget _pairing() => Form(
    key: _form,
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        const Text(
          'Pair with your Kinaiya host',
          style: TextStyle(fontSize: 24, fontWeight: FontWeight.bold),
        ),
        const SizedBox(height: 12),
        const Text(
          'Ask your host for a one-time pairing invitation. This companion can receive call invitations and notes. Voice calling is still in development.',
        ),
        const SizedBox(height: 24),
        TextFormField(
          key: const ValueKey('companion-host'),
          controller: _host,
          decoration: const InputDecoration(
            labelText: 'Host HTTPS address',
            hintText: 'https://your-host.example',
          ),
          keyboardType: TextInputType.url,
          autocorrect: false,
          maxLength: 512,
          validator: (value) => value == null || value.trim().isEmpty
              ? 'Enter your host address'
              : null,
        ),
        TextFormField(
          key: const ValueKey('companion-code'),
          controller: _code,
          decoration: const InputDecoration(labelText: 'One-time pairing code'),
          obscureText: true,
          autocorrect: false,
          enableSuggestions: false,
          maxLength: 128,
          validator: (value) =>
              value != null &&
                  RegExp(r'^[A-Za-z0-9_-]{43,128}$').hasMatch(value.trim())
              ? null
              : 'Enter the complete pairing code',
        ),
        TextFormField(
          key: const ValueKey('companion-name'),
          controller: _name,
          decoration: const InputDecoration(labelText: 'Device name'),
          maxLength: 120,
          validator: (value) => value == null || value.trim().isEmpty
              ? 'Enter a device name'
              : null,
        ),
        const SizedBox(height: 16),
        FilledButton(
          onPressed: _state.pairing ? null : _pair,
          child: Text(_state.pairing ? 'Pairing…' : 'Pair companion'),
        ),
        const SizedBox(height: 12),
        const Text(
          'The grant stays in memory. Closing or disconnecting this companion requires a new invitation; revoke it on the host to invalidate it there.',
        ),
      ],
    ),
  );

  Widget _call(CompanionCall call) => Card(
    key: ValueKey('call-${call.callId}'),
    child: Padding(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text(call.title, style: Theme.of(context).textTheme.titleLarge),
          const SizedBox(height: 8),
          Text(call.state.label, key: ValueKey('call-state-${call.callId}')),
          if (call.endReason == 'media_unavailable')
            const Text('The voice connection was unavailable.'),
          if (call.note.isNotEmpty) ...[
            const Divider(),
            const Text(
              'From your host',
              style: TextStyle(fontWeight: FontWeight.bold),
            ),
            // Literal Text only: notes never become markup, links or actions.
            SelectableText(
              call.note,
              key: ValueKey('call-note-${call.callId}'),
            ),
          ],
          if (!call.state.terminal) ...[
            const SizedBox(height: 12),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                if (call.state == CompanionCallState.ringing) ...[
                  FilledButton(
                    onPressed: _state.acting || _state.pending != null
                        ? null
                        : () => unawaited(_state.action(call, 'answer')),
                    child: const Text('Answer'),
                  ),
                  OutlinedButton(
                    onPressed: _state.acting || _state.pending != null
                        ? null
                        : () => unawaited(_state.action(call, 'decline')),
                    child: const Text('Decline'),
                  ),
                ] else
                  OutlinedButton(
                    onPressed: _state.acting || _state.pending != null
                        ? null
                        : () => unawaited(_state.action(call, 'end')),
                    child: const Text('End'),
                  ),
              ],
            ),
          ],
        ],
      ),
    ),
  );

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: _state,
    builder: (context, _) => Scaffold(
      appBar: AppBar(title: const Text('Kinaiya Companion')),
      body: SafeArea(
        child: Center(
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 720),
            child: ListView(
              padding: const EdgeInsets.all(20),
              children: [
                if (_state.message != null)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 16),
                    child: Semantics(
                      liveRegion: true,
                      child: Text(_state.message!),
                    ),
                  ),
                if (_state.paired) ...[
                  Text(
                    _state.device!.label,
                    style: Theme.of(context).textTheme.headlineSmall,
                  ),
                  Text(_state.device!.name),
                  const SizedBox(height: 8),
                  const Text(
                    'Keep this companion open to receive invitations. Voice calling is still in development.',
                  ),
                  Wrap(
                    spacing: 8,
                    children: [
                      TextButton(
                        onPressed: _state.refreshing
                            ? null
                            : () => unawaited(_state.refresh()),
                        child: Text(
                          _state.refreshing ? 'Refreshing…' : 'Refresh',
                        ),
                      ),
                      TextButton(
                        onPressed: _state.disconnect,
                        child: const Text('Disconnect and forget'),
                      ),
                    ],
                  ),
                  if (_state.pending != null) ...[
                    const Text(
                      'The last action is unconfirmed. Retry sends the same request.',
                    ),
                    OutlinedButton(
                      onPressed: _state.acting
                          ? null
                          : () => unawaited(_state.retryAction()),
                      child: const Text('Retry unconfirmed action'),
                    ),
                  ],
                  if (_state.calls.isEmpty)
                    const Padding(
                      padding: EdgeInsets.all(20),
                      child: Text('Waiting for an invitation from your host.'),
                    ),
                  ..._state.calls.map(_call),
                ] else
                  _pairing(),
              ],
            ),
          ),
        ),
      ),
    ),
  );
}
