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

  @override
  void initState() {
    super.initState();
    _input = TextEditingController(text: widget.state.agentDraft);
    _loadRegistry();
    _poll = Timer.periodic(const Duration(seconds: 4), (_) => _refreshTasks());
  }

  @override
  void dispose() {
    _input.dispose();
    for (final c in _steer.values) {
      c.dispose();
    }
    _poll?.cancel();
    super.dispose();
  }

  AppState get _s => widget.state;

  Future<void> _loadRegistry() async {
    final api = _s.client;
    if (api == null) return;
    try {
      _s.setCommands(await api.fetchCommands());
    } catch (_) {
      _s.setCommands(null);
    }
  }

  Future<void> _refreshTasks() async {
    final api = _s.client;
    if (api == null || !mounted) return;
    try {
      final tasks = await api.listTasks();
      _s.setTasks(tasks);
    } catch (_) {}
    final session = _s.agentSession;
    if (session == null) return;
    try {
      final entries = await api.recent(sessionId: session.sessionId);
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
    if (text.isEmpty || api == null || !_s.connected || _s.busyAgent) return;
    _s.busyAgent = true;
    _s.touch();
    try {
      if (!text.contains('\n') &&
          !text.contains('\r') &&
          RegExp(r'^/[a-z][a-z0-9_-]*(?:\s|$)').hasMatch(text)) {
        await _dispatchSlash(api, text);
        return;
      }
      final session =
          _s.agentSession ?? await _s.ensureSession('agent', name: 'work');
      final task = await api.createTask(
        sessionId: session.sessionId,
        threadId: session.threadId,
        epoch: session.epoch,
        objective: text.startsWith(r'\/') ? text.substring(1) : text,
        idempotencyKey: newIdempotencyKey(),
      );
      _input.clear();
      _s.setDraft(AppView.agent, '');
      _s.agentHistory.add(
        ChatMsg(
          role: 'user',
          text: text,
          note: 'Task ${task.taskId} ${task.state}',
        ),
      );
      await _refreshTasks();
    } on ApiException catch (e) {
      _s.agentNotice = e.status == 409
          ? 'That request conflicts with current work. Refresh and try again.'
          : "Couldn't reach the Agent service. Check the connection.";
      _s.touch();
    } finally {
      _s.busyAgent = false;
      _s.touch();
    }
  }

  /// Agent-only slash dispatch. Unknown commands are never sent anywhere;
  /// an unavailable registry is reported honestly with no local execution.
  Future<void> _dispatchSlash(KinaiyaClient api, String text) async {
    final name = text.split(RegExp(r'\s+')).first.substring(1);
    final registry = _s.commands;
    if (registry == null) {
      _s.agentNotice =
          'Command registry unavailable ($name was not run). Plain instructions still work.';
      _s.touch();
      return;
    }
    final match = registry.where((c) => c.matches(name)).toList();
    if (match.isEmpty || !match.first.available) {
      _s.agentNotice = 'Unknown command "$name": not sent anywhere.';
      _s.touch();
      return;
    }
    try {
      final session =
          _s.agentSession ?? await _s.ensureSession('agent', name: 'work');
      final out = await api.executeCommand(
        sessionId: session.sessionId,
        threadId: session.threadId,
        epoch: session.epoch,
        input: text,
        idempotencyKey: newIdempotencyKey(),
      );
      _input.clear();
      _s.setDraft(AppView.agent, '');
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
      await _refreshTasks();
    } on ApiException {
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
    final instruction = _steer[task.taskId]?.text.trim() ?? '';
    if (api == null || instruction.isEmpty) return;
    try {
      await api.steerTask(
        taskId: task.taskId,
        instruction: instruction,
        expectedObjectiveRevision: task.objectiveRevision,
        idempotencyKey: newIdempotencyKey(),
      );
      _steer[task.taskId]?.clear();
      await _refreshTasks();
    } on ApiException catch (e) {
      _s.agentNotice = e.status == 409
          ? 'That task moved on; refresh before steering again.'
          : "Couldn't steer that task. Check the connection.";
      _s.touch();
    }
  }

  Future<void> _cancelTask(AgentTask task) async {
    final api = _s.client;
    if (api == null) return;
    try {
      await api.interruptTask(
        taskId: task.taskId,
        expectedObjectiveRevision: task.objectiveRevision,
        idempotencyKey: newIdempotencyKey(),
      );
      await _refreshTasks();
    } on ApiException {
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
    if (api == null) return;
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
      _s.removeApproval(approval.approvalId);
      await _refreshTasks();
    } on ApiException {
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
              const Text(
                'Finished without evidence; not shown as proven success.',
                style: TextStyle(color: Colors.orange),
              ),
            if (task.summary.isNotEmpty) Text(task.summary),
            for (final e in task.evidence)
              Padding(
                padding: const EdgeInsets.only(top: 4),
                child: Text(
                  '${e.kind}: ${e.summary}'
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
