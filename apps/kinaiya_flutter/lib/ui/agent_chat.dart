import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';

import '../api/client.dart';
import '../api/models.dart';
import '../state/app_state.dart';
import 'widgets.dart';

/// Direct Agent Chat: real worker surface with tools, evidence, approvals
/// and cancel. Slash commands are Agent-only and registry-driven; Converse
/// text is never routed here.
class AgentChat extends StatefulWidget {
  const AgentChat({super.key, required this.state});
  final AppState state;

  @override
  State<AgentChat> createState() => _AgentChatState();
}

class _AgentChatState extends State<AgentChat> {
  late final TextEditingController _input;
  final Map<String, TextEditingController> _steer = {};
  Timer? _poll;
  KinaiyaClient? _lastClient;
  int _lastGeneration = 0;

  @override
  void initState() {
    super.initState();
    _input = TextEditingController(text: widget.state.agentDraft);
    _lastClient = _s.client;
    _lastGeneration = _s.connectionGeneration;
    _s.addListener(_handleStateChange);
    _loadRegistry();
    _poll = Timer.periodic(const Duration(seconds: 4), (_) => _refreshTasks());
  }

  @override
  void didUpdateWidget(covariant AgentChat oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.state, widget.state)) {
      oldWidget.state.removeListener(_handleStateChange);
      _lastClient = _s.client;
      _lastGeneration = _s.connectionGeneration;
      _syncConnectionDraft();
      _s.addListener(_handleStateChange);
      _loadRegistry();
    }
  }

  @override
  void dispose() {
    _poll?.cancel();
    _s.removeListener(_handleStateChange);
    _input.dispose();
    for (final c in _steer.values) {
      c.dispose();
    }
    super.dispose();
  }

  AppState get _s => widget.state;

  /// Same-view reconnect: the AppState object is identical but its client
  /// and generation change on connect/disconnect. Reload the registry
  /// without requiring a tab switch. Stale reloads are dropped inside
  /// [AppState.refreshCommands].
  void _handleStateChange() {
    if (!mounted) return;
    if (!identical(_lastClient, _s.client) ||
        _lastGeneration != _s.connectionGeneration) {
      _lastClient = _s.client;
      _lastGeneration = _s.connectionGeneration;
      _syncConnectionDraft();
      _loadRegistry();
      unawaited(_refreshTasks());
    }
  }

  void _syncConnectionDraft() {
    if (_input.text != _s.agentDraft) _input.text = _s.agentDraft;
    for (final controller in _steer.values) {
      controller.clear();
    }
  }

  /// Bounded read-only registry reload; never executes a command.
  Future<void> _loadRegistry() async {
    await _s.refreshCommands();
  }

  Future<void> _refreshTasks() async {
    final api = _s.client;
    final generation = _s.connectionGeneration;
    final session = _s.agentSession;
    if (api == null || !mounted) return;
    if (!_s.ownsConnection(generation, api)) return;
    try {
      final tasks = await api.listTasks();
      if (!mounted || !_s.ownsConnection(generation, api)) return;
      _s.setTasks(tasks);
    } catch (_) {
      if (!mounted || !_s.ownsConnection(generation, api)) return;
    }
    if (session == null) return;
    if (!mounted || !_s.ownsConnection(generation, api)) return;
    // The approval scan belongs to the session captured before the await;
    // a reconnect installing a new session drops this stale read.
    if (_s.agentSession?.sessionId != session.sessionId ||
        _s.agentSession?.threadId != session.threadId ||
        _s.agentSession?.epoch != session.epoch) {
      return;
    }
    try {
      final entries = await api.recent(sessionId: session.sessionId);
      if (!mounted || !_s.ownsConnection(generation, api)) return;
      if (_s.agentSession?.sessionId != session.sessionId ||
          _s.agentSession?.threadId != session.threadId ||
          _s.agentSession?.epoch != session.epoch) {
        return;
      }
      final found = <ApprovalRequest>[];
      final resolved = entries
          .where((e) => e.kind == 'approval.resolved')
          .map((e) => e.payload['approval_id'])
          .toSet();
      for (final e in entries) {
        if (e.kind == 'approval.requested' && e.origin == 'agent-supervisor') {
          final approval = ApprovalRequest.fromEntry(e);
          final matching = _s.tasks.where((t) => t.taskId == approval.taskId);
          if (!resolved.contains(approval.approvalId) &&
              !approval.isExpired &&
              matching.any(
                (t) =>
                    t.state == 'awaiting_approval' &&
                    t.objectiveRevision == approval.objectiveRevision,
              )) {
            found.add(approval);
          }
        }
      }
      _s.recordApprovals(found);
    } catch (_) {}
  }

  Future<void> _send() async {
    final text = _input.text.trim();
    final api = _s.client;
    final generation = _s.connectionGeneration;
    if (text.isEmpty || api == null || !_s.connected || _s.busyAgent) return;
    if (!_s.ownsConnection(generation, api)) return;
    _s.busyAgent = true;
    _s.touch();
    try {
      if (!text.contains('\n') &&
          !text.contains('\r') &&
          RegExp(r'^/[a-z][a-z0-9_-]*(?:\s|$)').hasMatch(text)) {
        await _dispatchSlash(api, generation, text);
        return;
      }
      SessionInfo? session = _s.agentSession;
      if (session == null) {
        try {
          session = await _s.ensureSession('agent', name: 'work');
        } catch (_) {
          // Stale acquisition (reconnect raced the session read) leaves
          // the new connection untouched; a fresh send can retry there.
          if (!_s.ownsConnection(generation, api)) return;
          rethrow;
        }
      }
      // Recheck after the awaited acquisition: a reconnect must not have
      // a side effect submitted from a stale session/client.
      if (!mounted || !_s.ownsConnection(generation, api) || !_s.connected) {
        return;
      }
      final liveApi = _s.client;
      if (liveApi == null || !identical(liveApi, api)) return;
      final task = await liveApi.createTask(
        sessionId: session.sessionId,
        threadId: session.threadId,
        epoch: session.epoch,
        objective: text.startsWith(r'\/') ? text.substring(1) : text,
        idempotencyKey: newIdempotencyKey(),
      );
      // Already-sent requests cannot be recalled, but a stale completion
      // must not mutate the new connection's draft, history or busy state.
      if (!mounted || !_s.ownsConnection(generation, api)) return;
      // Clear only the draft that was actually sent; text typed during the
      // await (same or new connection) survives.
      if (_input.text.trim() == text) {
        _input.clear();
        if (_s.agentDraft.trim() == text) _s.setDraft(AppView.agent, '');
      }
      _s.agentHistory.add(
        ChatMsg(
          role: 'user',
          text: text,
          note: 'Task ${task.taskId} ${task.state}',
        ),
      );
      _s.touch();
      if (_s.ownsConnection(generation, api)) await _refreshTasks();
    } on ApiException catch (e) {
      if (!_s.ownsConnection(generation, api)) return;
      _s.agentNotice = e.status == 409
          ? 'That request conflicts with current work. Refresh and try again.'
          : "Couldn't reach the Agent service. Check the connection.";
      _s.touch();
    } finally {
      // Cleanup belongs to the originating connection only.
      if (_s.ownsConnection(generation, api)) {
        _s.busyAgent = false;
        _s.touch();
      }
    }
  }

  /// Agent-only slash dispatch. Unknown commands are never sent anywhere;
  /// an unavailable registry is reported honestly with no local execution.
  Future<void> _dispatchSlash(
    KinaiyaClient api,
    int generation,
    String text,
  ) async {
    final name = text.split(RegExp(r'\s+')).first.substring(1);
    final registry = _s.commands;
    if (registry == null) {
      if (!_s.ownsConnection(generation, api)) return;
      _s.agentNotice =
          'Command registry unavailable ($name was not run). Plain instructions still work.';
      _s.touch();
      return;
    }
    final match = registry.where((c) => c.matches(name)).toList();
    if (match.isEmpty || !match.first.available) {
      if (!_s.ownsConnection(generation, api)) return;
      _s.agentNotice = 'Unknown command "$name": not sent anywhere.';
      _s.touch();
      return;
    }
    try {
      SessionInfo? session = _s.agentSession;
      if (session == null) {
        try {
          session = await _s.ensureSession('agent', name: 'work');
        } catch (_) {
          if (!_s.ownsConnection(generation, api)) return;
          rethrow;
        }
      }
      // Recheck after acquisition and re-validate against the live
      // registry: a stale decision must not execute on a new connection.
      if (!mounted || !_s.ownsConnection(generation, api) || !_s.connected) {
        return;
      }
      final liveRegistry = _s.commands;
      final liveMatch =
          liveRegistry?.where((c) => c.matches(name)).toList() ?? [];
      if (liveMatch.isEmpty || !liveMatch.first.available) {
        _s.agentNotice = 'Unknown command "$name": not sent anywhere.';
        _s.touch();
        return;
      }
      final liveApi = _s.client;
      if (liveApi == null || !identical(liveApi, api)) return;
      final out = await liveApi.executeCommand(
        sessionId: session.sessionId,
        threadId: session.threadId,
        epoch: session.epoch,
        input: text,
        idempotencyKey: newIdempotencyKey(),
      );
      // Already-sent commands cannot be recalled, but the stale result
      // must not clear or append into the new connection.
      if (!mounted || !_s.ownsConnection(generation, api)) return;
      if (_input.text.trim() == text) {
        _input.clear();
        if (_s.agentDraft.trim() == text) _s.setDraft(AppView.agent, '');
      }
      if (out?['status'] == 'ok' && out?['result'] is Map) {
        final result = (out!['result'] as Map).cast<String, Object?>();
        if (result['clear_view'] == true) _s.agentHistory.clear();
        if (out['command'] == 'new' || out['command'] == 'resume') {
          _s.agentSession = SessionInfo.fromJson(result);
          _s.agentHistory.clear();
          _s.agentCursor = 0;
        }
      }
      _s.agentHistory.add(
        ChatMsg(
          role: 'kinaiya',
          text: 'Command $name: ${out?['status'] ?? 'no result'}',
          note: out?['result'] != null
              ? const JsonEncoder.withIndent('  ').convert(out!['result'])
              : out?['error']?.toString(),
        ),
      );
      _s.touch();
      if (_s.ownsConnection(generation, api)) await _refreshTasks();
      if (name == 'resources' && _s.ownsConnection(generation, api)) {
        await _s.refreshResources(force: true);
      }
    } on ApiException {
      if (!_s.ownsConnection(generation, api)) return;
      _s.agentNotice = "Couldn't run that command. Check the connection.";
      _s.touch();
    }
  }

  void _showCommands() {
    showModalBottomSheet<void>(
      context: context,
      builder: (context) => SafeArea(
        child: ListView(
          shrinkWrap: true,
          children: [
            for (final command in _s.commands ?? <CommandDef>[])
              ListTile(
                title: Text(
                  command.syntax.isEmpty ? '/${command.name}' : command.syntax,
                ),
                subtitle: Text('${command.description}\n${command.origin}'),
                enabled: command.available,
                onTap: () {
                  final text = '/${command.name} ';
                  _input.text = text;
                  _s.setDraft(AppView.agent, text);
                  Navigator.of(context).pop();
                },
              ),
          ],
        ),
      ),
    );
  }

  Future<void> _steerTask(AgentTask task) async {
    final api = _s.client;
    final generation = _s.connectionGeneration;
    final instruction = _steer[task.taskId]?.text.trim() ?? '';
    if (api == null || instruction.isEmpty) return;
    if (!_s.ownsConnection(generation, api)) return;
    try {
      await api.steerTask(
        taskId: task.taskId,
        instruction: instruction,
        expectedObjectiveRevision: task.objectiveRevision,
        idempotencyKey: newIdempotencyKey(),
      );
      // Already-sent steers cannot be recalled; only the originating
      // connection may consume the result.
      if (!mounted || !_s.ownsConnection(generation, api)) return;
      _steer[task.taskId]?.clear();
      await _refreshTasks();
    } on ApiException catch (e) {
      if (!_s.ownsConnection(generation, api)) return;
      _s.agentNotice = e.status == 409
          ? 'That task moved on; refresh before steering again.'
          : "Couldn't steer that task. Check the connection.";
      _s.touch();
    }
  }

  Future<void> _cancelTask(AgentTask task) async {
    final api = _s.client;
    final generation = _s.connectionGeneration;
    if (api == null) return;
    if (!_s.ownsConnection(generation, api)) return;
    try {
      await api.interruptTask(
        taskId: task.taskId,
        expectedObjectiveRevision: task.objectiveRevision,
        idempotencyKey: newIdempotencyKey(),
      );
      if (!mounted || !_s.ownsConnection(generation, api)) return;
      await _refreshTasks();
    } on ApiException {
      if (!_s.ownsConnection(generation, api)) return;
      _s.agentNotice = "Couldn't stop that task. Check the connection.";
      _s.touch();
    }
  }

  Future<void> _resolve(
    ApprovalRequest approval,
    AgentTask? task,
    bool ok,
  ) async {
    final api = _s.client;
    final generation = _s.connectionGeneration;
    if (api == null) return;
    if (!_s.ownsConnection(generation, api)) return;
    if (approval.isExpired) {
      _s.agentNotice = 'That approval expired and cannot be reused.';
      _s.removeApproval(approval.approvalId);
      return;
    }
    if (task != null && task.objectiveRevision != approval.objectiveRevision) {
      _s.agentNotice =
          'That task moved to a newer revision; this approval cannot apply.';
      _s.removeApproval(approval.approvalId);
      return;
    }
    try {
      await api.resolveApproval(
        approvalId: approval.approvalId,
        approved: ok,
        actionDigest: approval.actionDigest,
        objectiveRevision: approval.objectiveRevision,
      );
      // A stale resolve must not remove the new connection's approvals.
      if (!mounted || !_s.ownsConnection(generation, api)) return;
      _s.removeApproval(approval.approvalId);
      await _refreshTasks();
    } on ApiException {
      if (!_s.ownsConnection(generation, api)) return;
      _s.agentNotice = "Couldn't record that decision. Check the connection.";
      _s.touch();
    }
  }

  @override
  Widget build(BuildContext context) {
    final tasksById = {for (final t in _s.tasks) t.taskId: t};
    return Column(
      children: [
        if (!_s.commandsAvailable)
          const MaterialBanner(
            content: Text(
              'Command registry unavailable; plain instructions still work.',
            ),
            actions: [SizedBox.shrink()],
          ),
        if (_s.agentNotice.isNotEmpty)
          MaterialBanner(
            content: Text(_s.agentNotice),
            actions: [
              TextButton(
                onPressed: () {
                  _s.agentNotice = '';
                  _s.touch();
                },
                child: const Text('DISMISS'),
              ),
            ],
          ),
        for (final a in _s.approvals) _approvalCard(a, tasksById[a.taskId]),
        Expanded(
          child: ListView(
            padding: const EdgeInsets.symmetric(vertical: 8),
            children: [
              for (final m in _s.agentHistory)
                MessageBubble(role: m.role, text: m.text, note: m.note),
              if (_s.tasks.isNotEmpty)
                const Padding(
                  padding: EdgeInsets.symmetric(horizontal: 12, vertical: 8),
                  child: Text('Worker tasks across sessions · newest first'),
                ),
              for (final t in _s.tasks.reversed) _taskCard(t),
              if (_s.tasks.isEmpty && _s.agentHistory.isEmpty)
                const Padding(
                  padding: EdgeInsets.all(24),
                  child: Text(
                    'Describe the work to do. Plain words become tasks; /commands need the registry.',
                    textAlign: TextAlign.center,
                  ),
                ),
            ],
          ),
        ),
        SafeArea(
          child: Padding(
            padding: const EdgeInsets.all(8),
            child: Row(
              children: [
                IconButton(
                  tooltip: 'Agent commands',
                  onPressed: _s.commandsAvailable ? _showCommands : null,
                  icon: const Icon(Icons.terminal),
                ),
                Expanded(
                  child: TextField(
                    controller: _input,
                    decoration: const InputDecoration(
                      labelText: 'Work for the agent, or /command',
                      border: OutlineInputBorder(),
                    ),
                    minLines: 1,
                    maxLines: 4,
                    textInputAction: TextInputAction.send,
                    onChanged: (v) => _s.setDraft(AppView.agent, v),
                    onSubmitted: (_) => _send(),
                  ),
                ),
                const SizedBox(width: 8),
                SendMascotButton(
                  label: 'Send task',
                  enabled:
                      _s.connected &&
                      !_s.busyAgent &&
                      _input.text.trim().isNotEmpty,
                  onPressed: _send,
                ),
              ],
            ),
          ),
        ),
      ],
    );
  }

  Widget _taskCard(AgentTask task) {
    final grounded = task.isGroundedSuccess;
    final tone = grounded
        ? Colors.green
        : task.state == 'failed'
        ? Colors.red
        : Colors.amber;
    _steer.putIfAbsent(task.taskId, TextEditingController.new);
    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Semantics(
                    label: 'Task objective: ${task.objective}',
                    child: Text(
                      task.objective,
                      style: Theme.of(context).textTheme.titleSmall,
                    ),
                  ),
                ),
                StatusChip(text: task.state, tone: tone),
              ],
            ),
            if (grounded)
              const Text(
                'Succeeded with evidence below.',
                style: TextStyle(color: Colors.green),
              ),
            if (task.state == 'succeeded' && !grounded)
              Text(
                task.hasUnverifiedExternalOnly
                    ? 'Finished with an unverified external receipt only; '
                          'response received, side effects not proven.'
                    : 'Finished without independent completion evidence.',
                style: const TextStyle(color: Colors.orange),
              ),
            if (task.summary.isNotEmpty) Text(task.summary),
            for (final e in task.evidence)
              Padding(
                padding: const EdgeInsets.only(top: 4),
                child: Text(
                  '${e.kind}: ${e.summary}'
                  '${e.kind == 'external' ? ' (unverified receipt)' : ''}'
                  '${e.path != null ? ' @ ${e.path}' : ''}'
                  '${e.sha256 != null ? ' sha256:${e.sha256!.substring(0, e.sha256!.length.clamp(0, 12))}' : ''}'
                  '${e.exitCode != null ? ' exit:${e.exitCode}' : ''}',
                  style: Theme.of(context).textTheme.bodySmall,
                ),
              ),
            if (task.remainingRequiredWork.isNotEmpty)
              Text(
                'Remaining: ${task.remainingRequiredWork.join('; ')}',
                style: Theme.of(context).textTheme.bodySmall,
              ),
            if (task.error != null && task.error!.isNotEmpty)
              Text(
                'Error: ${task.error}',
                style: const TextStyle(color: Colors.red),
              ),
            if (!task.isTerminal)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Row(
                  children: [
                    Expanded(
                      child: TextField(
                        controller: _steer[task.taskId],
                        decoration: const InputDecoration(
                          labelText: 'Steer this task',
                          border: OutlineInputBorder(),
                        ),
                      ),
                    ),
                    TextButton(
                      onPressed: () => _steerTask(task),
                      child: const Text('Steer'),
                    ),
                    TextButton(
                      onPressed: () => _cancelTask(task),
                      child: const Text('Stop'),
                    ),
                  ],
                ),
              ),
          ],
        ),
      ),
    );
  }

  Widget _approvalCard(ApprovalRequest approval, AgentTask? task) {
    final args = approval.arguments.entries
        .map((e) => '${e.key}=${e.value}')
        .join(' ');
    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      color: Colors.amber.shade50,
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Semantics(
              label:
                  'Approval requested for ${approval.tool} on task ${approval.taskId}',
              child: Text(
                'Approval: ${approval.tool}',
                style: Theme.of(context).textTheme.titleSmall,
              ),
            ),
            SelectableText(
              'Action: $args\ndigest:${approval.actionDigest} '
              'revision:${approval.objectiveRevision}'
              '${approval.expiresAt != null ? ' expires:${approval.expiresAt}' : ''}',
            ),
            if (approval.isExpired)
              const Text(
                'Expired: this approval cannot be reused.',
                style: TextStyle(color: Colors.red),
              ),
            Row(
              children: [
                FilledButton(
                  onPressed: approval.isExpired
                      ? null
                      : () => _resolve(approval, task, true),
                  child: const Text('Approve'),
                ),
                const SizedBox(width: 8),
                OutlinedButton(
                  onPressed: () => _resolve(approval, task, false),
                  child: const Text('Deny'),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}
