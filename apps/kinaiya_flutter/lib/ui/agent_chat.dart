import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';

import '../api/client.dart';
import '../api/models.dart';
import '../config/options_navigation.dart';
import '../state/app_state.dart';
import 'agent_design.dart';
import 'agent_workspace_sidebars.dart';
import 'mcp_settings.dart';
import 'settings_controls.dart';
import 'widgets.dart';

/// Direct Agent Chat: real worker surface with tools, evidence, approvals
/// and cancel. Slash commands are Agent-only and registry-driven; Converse
/// text is never routed here.
class AgentChat extends StatefulWidget {
  const AgentChat({
    super.key,
    required this.state,
    this.controls,
    this.onOpenOptions,
  });
  final AppState state;
  final AgentViewControls? controls;
  final ValueChanged<OptionsPage>? onOpenOptions;

  @override
  State<AgentChat> createState() => _AgentChatState();
}

class _AgentChatState extends State<AgentChat> {
  AgentViewControls? _registeredControls;
  void Function()? _registeredCommands;

  void _detachSettingsControls() {
    final controls = _registeredControls;
    if (controls != null &&
        identical(controls.openCommands, _registeredCommands)) {
      controls.openCommands = null;
    }
    _registeredControls = null;
    _registeredCommands = null;
  }

  void _bindSettingsControls() {
    _detachSettingsControls();
    final controls = widget.controls;
    if (controls == null) return;
    final owner = widget.state;
    late final void Function() open;
    open = () {
      if (!mounted ||
          !identical(widget.state, owner) ||
          !identical(widget.controls, controls) ||
          !identical(controls.openCommands, open) ||
          !owner.commandsAvailable) {
        return;
      }
      _showCommands();
    };
    _registeredControls = controls;
    _registeredCommands = open;
    controls.openCommands = open;
  }

  late final TextEditingController _input;
  bool _detailsInline = false;
  bool _harnessCollapsed = false,
      _sessionsCollapsed = false,
      _showDetails = true;
  int _lastSelection = 0;
  String? _lastSessionScope;
  String? _selectedTaskId;
  final Map<String, TextEditingController> _steer = {};
  final Map<String, ContentScope> _steerScopes = {};
  Timer? _poll;
  KinaiyaClient? _lastClient;
  int _lastGeneration = 0;
  bool _lastWorkspaceAuth = false;
  int _taskRead = 0;

  @override
  void initState() {
    super.initState();
    _bindSettingsControls();
    _input = TextEditingController(text: widget.state.agentDraft);
    _lastClient = _s.client;
    _lastGeneration = _s.connectionGeneration;
    _lastWorkspaceAuth = _s.sessionAuthenticated;
    _s.addListener(_handleStateChange);
    _loadRegistry();
    unawaited(_refreshTasks());
    _scheduleWorkspaceRefresh();
    _poll = Timer.periodic(const Duration(seconds: 4), (_) => _refreshTasks());
  }

  @override
  void didUpdateWidget(covariant AgentChat oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.controls, widget.controls) ||
        !identical(oldWidget.state, widget.state)) {
      _bindSettingsControls();
    }
    if (!identical(oldWidget.state, widget.state)) {
      oldWidget.state.removeListener(_handleStateChange);
      _lastClient = _s.client;
      _lastGeneration = _s.connectionGeneration;
      _lastWorkspaceAuth = _s.sessionAuthenticated;
      _syncConnectionDraft();
      _s.addListener(_handleStateChange);
      _loadRegistry();
      _scheduleWorkspaceRefresh();
    }
  }

  void _scheduleWorkspaceRefresh() {
    final owner = _s;
    final generation = owner.connectionGeneration;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted ||
          !identical(widget.state, owner) ||
          owner.connectionGeneration != generation ||
          owner.agentHarnessesLoading ||
          owner.agentHarnesses.isNotEmpty ||
          owner.agentHarnessError != null) {
        return;
      }
      unawaited(owner.refreshAgentHarnesses());
    });
  }

  @override
  void dispose() {
    _detachSettingsControls();
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
    final connectionChanged =
        !identical(_lastClient, _s.client) ||
        _lastGeneration != _s.connectionGeneration;
    final becameAuthenticated = _s.sessionAuthenticated && !_lastWorkspaceAuth;
    // Record auth before callbacks notify synchronously. A reconnect can also
    // authenticate in the same notification and needs only one catalog refresh.
    _lastWorkspaceAuth = _s.sessionAuthenticated;
    if (connectionChanged) {
      _lastClient = _s.client;
      _lastGeneration = _s.connectionGeneration;
      _syncConnectionDraft();
      _loadRegistry();
      unawaited(_refreshTasks());
    }
    if (connectionChanged || becameAuthenticated) {
      unawaited(_s.refreshAgentHarnesses());
    }
    final session = _s.agentSession;
    final scope = session == null
        ? null
        : '${session.sessionId}/${session.threadId}/${session.epoch}';
    if (_lastSelection != _s.agentSelectionGeneration ||
        _lastSessionScope != scope) {
      _lastSelection = _s.agentSelectionGeneration;
      _lastSessionScope = scope;
      _syncConnectionDraft();
    }
  }

  void _syncConnectionDraft() {
    _steerScopes.clear();
    _selectedTaskId = null;
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
    final read = ++_taskRead;
    bool ownsRead() =>
        mounted && read == _taskRead && _s.ownsConnection(generation, api);
    final session = _s.agentSession;
    if (api == null || !mounted) return;
    if (!_s.ownsConnection(generation, api)) return;
    try {
      final tasks = await api.listTasks();
      if (!ownsRead()) return;
      _s.setTasks(tasks);
    } catch (_) {
      if (!ownsRead()) return;
      _s.markTasksUnavailable();
    }
    try {
      var page = await api.pendingApprovals();
      if (!ownsRead()) return;
      if (page != null) {
        final found = <ApprovalRequest>[];
        final cursors = <String>{};
        final maxPages = _s.tasks.length ~/ 100 + 2;
        for (var index = 0; index < maxPages; index++) {
          for (final approval in page!.approvals) {
            if (!approval.isExpired &&
                _s.tasks.any(
                  (task) =>
                      task.taskId == approval.taskId &&
                      task.state == 'awaiting_approval' &&
                      task.objectiveRevision == approval.objectiveRevision &&
                      task.sessionId == approval.sessionId &&
                      task.threadId == approval.threadId &&
                      task.epoch == approval.epoch,
                )) {
              found.add(approval);
            }
          }
          final after = page.nextAfter;
          if (after == null) break;
          if (!cursors.add(after)) {
            throw const FormatException('Repeated approval cursor');
          }
          page = await api.pendingApprovals(after: after);
          if (!ownsRead()) return;
          if (page == null) {
            throw const FormatException(
              'Approval endpoint changed during read',
            );
          }
        }
        _s.recordApprovals(found);
        return;
      }
    } catch (_) {
      if (!ownsRead()) return;
      _s.recordApprovals([]);
      return;
    }
    // Older hosts expose requests only through their session journal.
    if (session == null) return;
    if (!ownsRead()) return;
    // The approval scan belongs to the session captured before the await;
    // a reconnect installing a new session drops this stale read.
    if (_s.agentSession?.sessionId != session.sessionId ||
        _s.agentSession?.threadId != session.threadId ||
        _s.agentSession?.epoch != session.epoch) {
      return;
    }
    try {
      final entries = await api.recent(sessionId: session.sessionId);
      if (!ownsRead()) return;
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

  bool _isSlash(String text) =>
      !text.contains('\n') &&
      !text.contains('\r') &&
      RegExp(r'^/[a-z][a-z0-9_-]*(?:\s|$)').hasMatch(text);

  /// Submit gate for the Send button: deterministic owner controls stay
  /// enabled without inference; natural text and skill invocations require
  /// Agent inference. Unknown registry state fails closed to inference.
  bool _canSubmitText(String text) {
    if (text.trim().isEmpty || _s.busyAgent || _s.agentIdentitySelecting) {
      return false;
    }
    if (!_s.sessionAuthenticated || _s.agentSession == null) return false;
    if (!_isSlash(text)) {
      return _s.agentSession != null &&
          _s.agentCanInfer &&
          !_s.agentHistoryLoading &&
          _s.agentHistoryError == null;
    }
    final name = text.trim().split(RegExp(r'\s+')).first.substring(1);
    final registry = _s.commands;
    if (registry == null) return _s.agentCanInfer;
    final match = registry.where((c) => c.matches(name)).toList();
    if (match.isEmpty || !match.first.available) return false;
    if (match.first.requiresInference) {
      return _s.agentCanInfer &&
          !_s.agentHistoryLoading &&
          _s.agentHistoryError == null;
    }
    return true;
  }

  Future<void> _send() async {
    final text = _input.text.trim();
    final api = _s.client;
    final generation = _s.connectionGeneration;
    final scope = _s.draftScope(AppView.agent);
    if (text.isEmpty ||
        api == null ||
        _s.busyAgent ||
        _s.agentIdentitySelecting) {
      return;
    }
    if (!_s.ownsConnection(generation, api)) return;
    if (!_s.sessionAuthenticated) return;
    final slash = _isSlash(text);
    if (!slash && (_s.agentHistoryLoading || _s.agentHistoryError != null)) {
      return;
    }
    if (!slash && !_s.agentCanDisclose(scope)) {
      if (!_s.ownsConnection(generation, api)) return;
      _s.agentNotice = _s.agentCanInfer
          ? 'This draft retains its original disclosure scope and cannot be sent to this provider. Clear and re-enter it to choose a new scope.'
          : 'Agent inference is not available for the current provider and disclosure consent. Deterministic controls (/status, /stop, /help) still work.';
      _s.touch();
      return;
    }
    if (!slash && _s.agentSession == null) return;
    if (slash && !_s.sessionAuthenticated) return;
    _s.busyAgent = true;
    _s.touch();
    try {
      if (slash) {
        await _dispatchSlash(api, generation, text, scope);
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
      // a side effect submitted from a stale session/client. Inference may
      // also have been revoked (for example by a /model switch).
      if (!mounted ||
          !_s.ownsConnection(generation, api) ||
          !_s.agentCanDisclose(scope)) {
        return;
      }
      final selection = _s.agentSelectionGeneration;
      if (!_s.ownsAgentSelection(selection, session)) return;
      final liveApi = _s.client;
      if (liveApi == null || !identical(liveApi, api)) return;
      final task = await liveApi.createTask(
        sessionId: session.sessionId,
        threadId: session.threadId,
        epoch: session.epoch,
        objective: text.startsWith(r'\/') ? text.substring(1) : text,
        contentScope: scope,
        idempotencyKey: newIdempotencyKey(),
      );
      // Already-sent requests cannot be recalled, but a stale completion
      // must not mutate the new connection's draft, history or busy state.
      if (!mounted ||
          !_s.ownsConnection(generation, api) ||
          !_s.ownsAgentSelection(selection, session)) {
        return;
      }
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
    } catch (_) {
      if (!_s.ownsConnection(generation, api)) return;
      _s.agentNotice =
          'The Agent request was not confirmed. Inspect current sessions and tasks before trying again.';
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
    ContentScope scope,
  ) async {
    final name = text.split(RegExp(r'\s+')).first.substring(1);
    if (!_s.sessionAuthenticated) return;
    final registry = _s.commands;
    if (registry == null) {
      if (!_s.ownsConnection(generation, api)) return;
      _s.agentNotice = _s.agentCanInfer
          ? 'Command registry unavailable ($name was not run). Plain instructions still work.'
          : 'Command registry unavailable ($name was not run). Agent inference is blocked for the current provider and consent.';
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
    // Skill invocations and unknown classes start inference; deterministic
    // runtime controls run without inference. Never invoke a skill just to
    // discover it was forbidden.
    if (match.first.requiresInference &&
        (_s.agentHistoryLoading || _s.agentHistoryError != null)) {
      return;
    }
    if (match.first.requiresInference && !_s.agentCanDisclose(scope)) {
      if (!_s.ownsConnection(generation, api)) return;
      _s.agentNotice = _s.agentCanInfer
          ? 'This draft retains its original disclosure scope and cannot be sent to this provider. Clear and re-enter it to choose a new scope.'
          : 'Agent inference is not available for the current provider and disclosure consent. Deterministic controls (/status, /stop, /help) still work.';
      _s.touch();
      return;
    }
    try {
      // A command never acquires an arbitrary destination while discovery is pending.
      if (_s.agentSession == null) {
        _s.agentNotice =
            'Choose an existing session or create one from Workspace first.';
        _s.touch();
        return;
      }
      // No session is created for blocked inference (checked above).
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
      // Inference may also have been revoked while awaiting the session.
      if (!mounted ||
          !_s.ownsConnection(generation, api) ||
          !_s.sessionAuthenticated) {
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
      if (liveMatch.first.requiresInference && !_s.agentCanDisclose(scope)) {
        _s.agentNotice =
            'Agent inference is not available for the current provider and disclosure consent. Deterministic controls (/status, /stop, /help) still work.';
        _s.touch();
        return;
      }
      final selection = _s.agentSelectionGeneration;
      if (!_s.ownsAgentSelection(selection, session)) return;
      final liveApi = _s.client;
      if (liveApi == null || !identical(liveApi, api)) return;
      final hasModelArgs =
          liveMatch.first.name == 'model' &&
          text.trim().split(RegExp(r'\s+')).length > 1;
      // A lost acknowledgment is not proof the old provider remains selected.
      if (hasModelArgs) _s.invalidateAgentInference();
      final out = await liveApi.executeCommand(
        sessionId: session.sessionId,
        threadId: session.threadId,
        epoch: session.epoch,
        input: text,
        contentScope: scope,
        idempotencyKey: newIdempotencyKey(),
      );
      // Already-sent commands cannot be recalled, but the stale result
      // must not clear or append into the new connection.
      if (!mounted ||
          !_s.ownsConnection(generation, api) ||
          !_s.ownsAgentSelection(selection, session)) {
        return;
      }
      if (_input.text.trim() == text) {
        _input.clear();
        if (_s.agentDraft.trim() == text) _s.setDraft(AppView.agent, '');
      }
      if (out?['status'] == 'ok' && out?['result'] is Map) {
        final result = (out!['result'] as Map).cast<String, Object?>();
        if (result['clear_view'] == true) _s.agentHistory.clear();
        if (out['command'] == 'new' || out['command'] == 'resume') {
          final next = SessionInfo.fromJson(result);
          if (next.channel != 'agent' || next.sessionId.isEmpty) {
            throw const FormatException('Invalid Agent command session');
          }
          await _s.selectAgentSession(next.sessionId);
          if (!_s.ownsConnection(generation, api) ||
              _s.agentSession?.sessionId != next.sessionId ||
              _s.agentSession?.threadId != next.threadId ||
              _s.agentSession?.epoch != next.epoch) {
            return;
          }
          unawaited(_s.refreshAgentSessions());
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
      if (name == 'voice' &&
          out?['status'] == 'ok' &&
          _s.ownsConnection(generation, api) &&
          _s.ownsAgentSelection(selection, session) &&
          mounted) {
        final result = out?['result'];
        if (result is Map && result['capture_changed'] == false) {
          final page = switch (result['open_options']) {
            'audio' => OptionsPage.audio,
            'voice' => OptionsPage.voice,
            _ => null,
          };
          if (page != null) widget.onOpenOptions?.call(page);
        }
      }
      if (name == 'resources' && _s.ownsConnection(generation, api)) {
        await _s.refreshResources(force: true);
      }
      // `/model` stays deterministic, but a successful profile switch
      // changes the Agent provider: clear and reverify Agent inference
      // before accepting more input.
      if (hasModelArgs &&
          out?['status'] == 'ok' &&
          _s.ownsConnection(generation, api)) {
        await _s.revalidateAgentInference();
      }
    } on ApiException {
      if (!_s.ownsConnection(generation, api)) return;
      _s.agentNotice = "Couldn't run that command. Check the connection.";
      _s.touch();
    } catch (_) {
      if (!_s.ownsConnection(generation, api)) return;
      _s.agentNotice =
          'The command result was not confirmed. Inspect the selected session before trying again.';
      _s.touch();
    }
  }

  void _showCommands() {
    final owner = widget.state;
    final generation = owner.connectionGeneration;
    if (!owner.commandsAvailable) return;
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
                  if (!mounted ||
                      !identical(widget.state, owner) ||
                      owner.connectionGeneration != generation ||
                      !owner.commandsAvailable) {
                    return;
                  }
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
    final scope = _steerScopes[task.taskId] ?? ContentScope.localOnly;
    if (api == null || instruction.isEmpty) return;
    if (!_s.ownsConnection(generation, api)) return;
    // Steering text starts inference; deterministic stop/approval controls
    // below deliberately do not require it.
    if (!_s.agentCanDisclose(scope)) {
      _s.agentNotice = _s.agentCanInfer
          ? 'This steering input retains its original disclosure scope. Clear and re-enter it to choose a new scope.'
          : 'Agent inference is not available for the current provider and disclosure consent. Steering is blocked; /status and /stop still work.';
      _s.touch();
      return;
    }
    try {
      await api.steerTask(
        taskId: task.taskId,
        instruction: instruction,
        contentScope: scope,
        expectedObjectiveRevision: task.objectiveRevision,
        idempotencyKey: newIdempotencyKey(),
      );
      // Already-sent steers cannot be recalled; only the originating
      // connection may consume the result.
      if (!mounted || !_s.ownsConnection(generation, api)) return;
      _steer[task.taskId]?.clear();
      _steerScopes.remove(task.taskId);
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
    // Deterministic owner stop: works on authenticated sessions even when
    // inference is blocked.
    if (!_s.sessionAuthenticated) return;
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
    // Deterministic owner approval: works without inference.
    if (!_s.sessionAuthenticated) return;
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

  void _showMcp() => showDialog<void>(
    context: context,
    builder: (_) => McpSettings(state: _s),
  );

  Widget _tools() => Wrap(
    spacing: 4,
    children: [
      IconButton(
        tooltip: 'Agent commands',
        onPressed: _s.commandsAvailable ? _showCommands : null,
        icon: const Icon(Icons.terminal_rounded),
      ),
      IconButton(
        tooltip: 'MCP connections',
        onPressed: _s.client == null ? null : _showMcp,
        icon: const Icon(Icons.hub_outlined),
      ),
    ],
  );

  Widget _composer() => Container(
    decoration: agentComposerDecoration(context),
    child: SafeArea(
      top: false,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 12),
        child: LayoutBuilder(
          builder: (context, box) {
            final input = TextField(
              controller: _input,
              key: const ValueKey('agent-input'),
              enabled: !_s.agentIdentitySelecting,
              decoration: const InputDecoration(
                labelText: 'Work for the agent, or /command',
              ),
              minLines: 1,
              maxLines: 4,
              textInputAction: TextInputAction.send,
              onChanged: (v) => _s.setDraft(AppView.agent, v),
              onSubmitted: (_) => _send(),
            );
            final send = SendMascotButton(
              label: 'Send task',
              enabled: _canSubmitText(_input.text),
              onPressed: _send,
            );
            if (box.maxWidth /
                    MediaQuery.textScalerOf(context).scale(1).clamp(1, 2) <
                AgentDesign.composerBreakpoint) {
              return Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  input,
                  const SizedBox(height: 8),
                  Row(
                    children: [
                      _tools(),
                      const SizedBox(width: 8),
                      Expanded(
                        child: Align(
                          alignment: Alignment.centerRight,
                          child: send,
                        ),
                      ),
                    ],
                  ),
                ],
              );
            }
            return Row(
              children: [
                _tools(),
                const SizedBox(width: 8),
                Expanded(child: input),
                const SizedBox(width: 12),
                send,
              ],
            );
          },
        ),
      ),
    ),
  );

  Widget _harnessSidebar({bool sheet = false}) => AgentHarnessSidebar(
    entries: [
      for (final item in _s.agentHarnesses)
        HarnessSidebarEntry(
          id: item.versionId,
          name: item.name,
          kind: item.kind,
          description: item.description,
          active: item.active,
          selectable: item.selectable,
          eligibility: item.eligibility,
        ),
      const HarnessSidebarEntry(
        id: 'unassociated',
        name: 'Unassociated historical sessions',
        kind: 'historical_sessions',
        description: 'No harness association was recorded for these sessions.',
        active: false,
        selectable: false,
        eligibility: 'historical',
      ),
      if (_s.selectedAgentHarness != null &&
          _s.selectedAgentHarness != 'unassociated' &&
          _s.selectedAgentHarnessInfo == null)
        HarnessSidebarEntry(
          id: _s.selectedAgentHarness!,
          name: _s.selectedAgentHarness!,
          kind: 'historical_sessions',
          description:
              'Recorded session harness; unavailable in the current catalog.',
          active: false,
          selectable: false,
          eligibility: 'unavailable',
        ),
    ],
    selectedId: _s.selectedAgentHarness,
    collapsed: !sheet && _harnessCollapsed,
    loading: _s.agentHarnessesLoading,
    error: _s.agentHarnessError,
    onToggle: () => sheet
        ? Navigator.of(context).pop()
        : setState(() => _harnessCollapsed = !_harnessCollapsed),
    onRefresh: () => unawaited(_s.refreshAgentHarnesses()),
    onSelect: (id) => unawaited(_s.selectAgentHarness(id)),
  );

  Widget _sessionSidebar({bool sheet = false}) => AgentSessionSidebar(
    entries: [
      for (final session in _s.agentWorkspaceSessions)
        SessionSidebarEntry(
          id: session.sessionId,
          label: session.name.isEmpty ? session.sessionId : session.name,
          harnessVersion: session.harnessVersion,
          selected: _s.agentSession?.sessionId == session.sessionId,
          subtitle: session.harnessVersion == null
              ? 'Historical harness unknown'
              : session.sessionId,
        ),
    ],
    harnessLabel:
        _s.selectedAgentHarnessInfo?.name ??
        (_s.selectedAgentHarness == 'unassociated'
            ? 'Historical harness unknown'
            : _s.selectedAgentHarness ?? 'Select a harness'),
    collapsed: !sheet && _sessionsCollapsed,
    loading: _s.agentSessionsLoading || _s.agentSessionCreating,
    error: _s.agentSessionsError,
    canCreate: _s.canCreateAgentSession,
    hasMore: _s.agentSessionsHaveMore,
    onToggle: () => sheet
        ? Navigator.of(context).pop()
        : setState(() => _sessionsCollapsed = !_sessionsCollapsed),
    onRefresh: () => unawaited(_s.refreshAgentSessions()),
    onCreate: () => unawaited(_s.createSelectedAgentSession()),
    onLoadMore: () => unawaited(_s.refreshAgentSessions(loadMore: true)),
    onSelect: (id) => unawaited(_s.selectAgentSession(id)),
    controls: Material(
      type: MaterialType.transparency,
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          if (_s.agentCreationUncertain) ...[
            const Padding(
              padding: EdgeInsets.all(12),
              child: Text(
                'Session creation is unconfirmed. Refresh and inspect sessions before creating another.',
              ),
            ),
            TextButton(
              onPressed: _s.canAcknowledgeAgentCreation
                  ? _s.acknowledgeAgentCreationUncertainty
                  : null,
              child: const Text('I checked sessions; allow another creation'),
            ),
          ],
          ListTile(
            leading: const Icon(Icons.terminal_rounded),
            title: const Text('Commands'),
            enabled: _s.commandsAvailable,
            onTap: _s.commandsAvailable ? _showCommands : null,
          ),
          ListTile(
            leading: const Icon(Icons.hub_outlined),
            title: const Text('Connections'),
            enabled: _s.client != null,
            onTap: _s.client == null ? null : _showMcp,
          ),
        ],
      ),
    ),
  );

  void _showSidebar(bool harnesses) {
    final owner = _s;
    showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      builder: (sheetContext) => SafeArea(
        child: SizedBox(
          height: MediaQuery.sizeOf(sheetContext).height * .9,
          child: ListenableBuilder(
            listenable: owner,
            builder: (_, _) => harnesses
                ? _harnessSidebar(sheet: true)
                : _sessionSidebar(sheet: true),
          ),
        ),
      ),
    );
  }

  Widget _detail(String label, String value) {
    final textTheme = Theme.of(context).textTheme;
    final scheme = Theme.of(context).colorScheme;
    return Padding(
      padding: const EdgeInsets.only(bottom: 16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            label,
            style: textTheme.labelMedium?.copyWith(
              color: scheme.onSurfaceVariant,
            ),
          ),
          const SizedBox(height: 4),
          SelectableText(value, style: textTheme.bodyMedium),
        ],
      ),
    );
  }

  Widget _details(AgentTask? task) => Container(
    width: AgentDesign.detailsWidth,
    decoration: agentDetailsDecoration(context),
    child: ListView(
      padding: const EdgeInsets.all(20),
      children: [
        Text('Task details', style: Theme.of(context).textTheme.titleLarge),
        const SizedBox(height: 8),
        Text(
          task == null
              ? 'Select a task on the left or in the conversation.'
              : 'Task inspection; new instructions use the selected session.',
          style: agentMutedStyle(
            context,
            Theme.of(context).textTheme.bodySmall,
          ),
        ),
        const SizedBox(height: 16),
        if (task == null)
          const Text('Choose a task to inspect its status and evidence.')
        else ...[
          _detail('Objective', task.objective),
          _detail('Current state', task.state),
          _detail('Objective revision', '${task.objectiveRevision}'),
          _detail('Task', task.taskId),
          if (task.modelPin case final pin?) ...[
            _detail('Model', '${pin['model']}'),
            _detail('Provider', '${pin['provider']}'),
            _detail('Model profile', '${pin['profile_id']}'),
            _detail('Reasoning', '${pin['reasoning'] ?? 'Profile default'}'),
            _detail('Model settings revision', '${pin['settings_revision']}'),
            if (pin['catalog_binding'] case final Map reference) ...[
              _detail('Saved provider', '${reference['id']}'),
              _detail('Provider version', '${reference['generation']}'),
              _detail('Catalog revision', '${pin['catalog_revision']}'),
              ExpansionTile(
                tilePadding: EdgeInsets.zero,
                title: const Text('Saved provider identity'),
                children: [
                  _detail(
                    'Configuration SHA256',
                    '${reference['configuration_sha256']}',
                  ),
                ],
              ),
            ],
          ],
          _detail('Session', task.sessionId),
          _detail(
            'Recorded harness',
            task.harnessVersion ?? 'Unknown historical harness',
          ),
          _detail(
            'Evidence',
            task.evidence.isEmpty
                ? 'No completion evidence recorded.'
                : '${task.evidence.length} recorded items; inspect the receipts in the task card.',
          ),
          if (task.remainingRequiredWork.isNotEmpty)
            _detail('Remaining work', task.remainingRequiredWork.join('\n')),
        ],
        const Divider(),
        const SizedBox(height: 16),
        _detail(
          'Current Agent provider',
          _s.agentProvider == null
              ? 'Not verified'
              : '${_s.agentProvider!['provider']} · ${_s.agentProvider!['model']}',
        ),
        _detail('Pending approvals', '${_s.approvals.length}'),
        Text(
          'Inspecting a task does not change the recipient of a new instruction. Use its Steer or Stop control to affect that task.',
          style: agentMutedStyle(
            context,
            Theme.of(context).textTheme.bodySmall,
          ),
        ),
      ],
    ),
  );

  Widget _conversation(bool wide, AgentTask? selected) {
    final tasksById = {for (final t in _s.tasks) t.taskId: t};
    final sessionTasks = _s.selectedAgentTasks;
    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(20, 20, 20, 12),
          child: Align(
            alignment: Alignment.centerLeft,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  'Direct Agent Chat',
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                  style: Theme.of(context).textTheme.headlineSmall,
                ),
                const SizedBox(height: 4),
                Text(
                  _s.agentSession?.name.isNotEmpty == true
                      ? 'Session: ${_s.agentSession!.name}'
                      : _s.agentHistoryLoading
                      ? 'Verifying session…'
                      : 'Choose a session or create one for the selected harness.',
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                  style: agentMutedStyle(
                    context,
                    Theme.of(context).textTheme.bodyMedium,
                  ),
                ),
              ],
            ),
          ),
        ),
        Expanded(
          child: ListView(
            padding: const EdgeInsets.symmetric(vertical: 8),
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

              for (final a in _s.approvals)
                _approvalCard(a, tasksById[a.taskId]),
              if (_s.agentHistoryLoading) const LinearProgressIndicator(),
              if (_s.agentHistoryError != null)
                Padding(
                  padding: const EdgeInsets.all(12),
                  child: Text(_s.agentHistoryError!),
                ),
              for (final m in _s.agentHistory)
                MessageBubble(
                  role: m.role,
                  text: m.text,
                  note: m.note,
                  speaker: 'Agent',
                ),
              if (sessionTasks.isNotEmpty)
                Padding(
                  padding: const EdgeInsets.symmetric(
                    horizontal: 16,
                    vertical: 8,
                  ),
                  child: Text(
                    'Tasks in this session · newest first',
                    style: agentMutedStyle(
                      context,
                      Theme.of(context).textTheme.bodySmall,
                    ),
                  ),
                ),
              for (final task in sessionTasks.reversed) _taskCard(task),
              if (_s.agentHistoryHasMore)
                TextButton(
                  onPressed: _s.agentHistoryLoading
                      ? null
                      : () => unawaited(_s.refreshAgentHistory()),
                  child: const Text('Load more recorded history'),
                ),
              if (sessionTasks.isEmpty && _s.agentHistory.isEmpty)
                Padding(
                  padding: const EdgeInsets.symmetric(
                    horizontal: 32,
                    vertical: 40,
                  ),
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Icon(
                        Icons.auto_awesome_outlined,
                        size: 40,
                        color: Theme.of(context).colorScheme.onSurfaceVariant,
                      ),
                      const SizedBox(height: 16),
                      Text(
                        'What would you like to work on?',
                        style: Theme.of(context).textTheme.titleLarge,
                        textAlign: TextAlign.center,
                      ),
                      const SizedBox(height: 12),
                      Text(
                        'Give Agent a task, inspect its work, and review approvals here. Your conversation stays in Converse.',
                        textAlign: TextAlign.center,
                        style: agentMutedStyle(
                          context,
                          Theme.of(context).textTheme.bodyMedium,
                        ),
                      ),
                    ],
                  ),
                ),
            ],
          ),
        ),
        _composer(),
      ],
    );
  }

  @override
  Widget build(BuildContext context) => LayoutBuilder(
    builder: (context, box) {
      final width =
          box.maxWidth / MediaQuery.textScalerOf(context).scale(1).clamp(1, 2);
      final wide = width >= AgentDesign.wideBreakpoint;
      _detailsInline = width >= AgentDesign.detailsBreakpoint && _showDetails;
      final tasks = _s.selectedAgentTasks;
      final matches = tasks.where((t) => t.taskId == _selectedTaskId);
      final selected = matches.firstOrNull ?? tasks.lastOrNull;
      final panes = Column(
        children: [
          if (!wide)
            Wrap(
              spacing: 8,
              children: [
                TextButton.icon(
                  key: const ValueKey('open-agent-harnesses'),
                  onPressed: () => _showSidebar(true),
                  icon: const Icon(Icons.account_tree_outlined),
                  label: const Text('Harnesses'),
                ),
                TextButton.icon(
                  key: const ValueKey('open-agent-workspace'),
                  onPressed: () => _showSidebar(false),
                  icon: const Icon(Icons.view_sidebar_outlined),
                  label: const Text('Workspace'),
                ),
              ],
            ),
          if (wide)
            Align(
              alignment: Alignment.centerRight,
              child: TextButton.icon(
                onPressed: () => setState(() => _showDetails = !_showDetails),
                icon: const Icon(Icons.info_outline),
                label: Text(_showDetails ? 'Hide details' : 'Show details'),
              ),
            ),
          Expanded(
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                if (wide)
                  SizedBox(
                    width: _harnessCollapsed ? 48 : 246,
                    child: _harnessSidebar(),
                  ),
                if (wide)
                  SizedBox(
                    width: _sessionsCollapsed ? 48 : 260,
                    child: _sessionSidebar(),
                  ),
                Expanded(
                  child: Center(
                    child: ConstrainedBox(
                      constraints: const BoxConstraints(
                        maxWidth: AgentDesign.conversationMaxWidth,
                      ),
                      child: _conversation(wide, selected),
                    ),
                  ),
                ),
                if (width >= AgentDesign.detailsBreakpoint && _showDetails)
                  _details(selected),
              ],
            ),
          ),
        ],
      );
      final minHeight = MediaQuery.textScalerOf(context).scale(1) >= 1.5
          ? 760.0
          : 500.0;
      return SingleChildScrollView(
        child: SizedBox(
          height: box.maxHeight < minHeight ? minHeight : box.maxHeight,
          child: panes,
        ),
      );
    },
  );

  void _inspectTask(AgentTask task) {
    setState(() => _selectedTaskId = task.taskId);
    if (!_detailsInline) {
      showModalBottomSheet<void>(
        context: context,
        isScrollControlled: true,
        builder: (sheet) => SafeArea(
          child: SizedBox(
            height: MediaQuery.sizeOf(sheet).height * .85,
            child: _details(task),
          ),
        ),
      );
    }
  }

  Widget _taskCard(AgentTask task) {
    final grounded = task.isGroundedSuccess;
    final scheme = Theme.of(context).colorScheme;
    final textTheme = Theme.of(context).textTheme;
    _steer.putIfAbsent(task.taskId, TextEditingController.new);
    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
      shape: agentCardShape(context),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Semantics(
              label: 'Task objective: ${task.objective}',
              child: Text(task.objective, style: textTheme.titleSmall),
            ),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              crossAxisAlignment: WrapCrossAlignment.center,
              children: [
                agentStatusChip(context, task),
                TextButton.icon(
                  onPressed: () => _inspectTask(task),
                  icon: const Icon(Icons.info_outline),
                  label: const Text('Inspect task'),
                ),
                if (!task.isTerminal)
                  TextButton.icon(
                    onPressed: () => _cancelTask(task),
                    icon: const Icon(Icons.stop_circle_outlined),
                    label: const Text('Stop'),
                  ),
              ],
            ),
            if (grounded)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Text(
                  'Succeeded with evidence below.',
                  style: textTheme.bodyMedium?.copyWith(
                    color: scheme.onSurface,
                  ),
                ),
              ),
            if (task.state == 'succeeded' && !grounded)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Text(
                  task.hasUnverifiedExternalOnly
                      ? 'Finished with a tool or computation receipt only; '
                            'task effects need independent evidence.'
                      : 'Finished without independent completion evidence.',
                  style: textTheme.bodyMedium?.copyWith(
                    color: scheme.secondary,
                  ),
                ),
              ),
            if (task.summary.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Text(task.summary),
              ),
            for (final e in task.evidence)
              Padding(
                padding: const EdgeInsets.only(top: 4),
                child: Text(
                  '${e.kind}: ${e.summary}'
                  '${{'external', 'extension'}.contains(e.kind) ? ' (receipt; effects unverified)' : ''}'
                  '${e.path != null ? ' @ ${e.path}' : ''}'
                  '${e.sha256 != null ? ' sha256:${e.sha256!.substring(0, e.sha256!.length.clamp(0, 12))}' : ''}'
                  '${e.exitCode != null ? ' exit:${e.exitCode}' : ''}',
                  style: textTheme.bodySmall,
                ),
              ),
            if (task.remainingRequiredWork.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(top: 4),
                child: Text(
                  'Remaining: ${task.remainingRequiredWork.join('; ')}',
                  style: textTheme.bodySmall,
                ),
              ),
            if (task.error != null && task.error!.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(top: 4),
                child: Text(
                  'Error: ${task.error}',
                  style: textTheme.bodyMedium?.copyWith(color: scheme.error),
                ),
              ),
            if (!task.isTerminal)
              Padding(
                padding: const EdgeInsets.only(top: 12),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    TextField(
                      controller: _steer[task.taskId],
                      onChanged: (text) {
                        if (text.trim().isEmpty) {
                          _steerScopes.remove(task.taskId);
                        } else {
                          _steerScopes.update(
                            task.taskId,
                            (scope) => scope.combine(_s.selectedContentScope),
                            ifAbsent: () => _s.selectedContentScope,
                          );
                        }
                      },
                      decoration: const InputDecoration(
                        labelText: 'Steer this task',
                        border: OutlineInputBorder(),
                      ),
                    ),
                    const SizedBox(height: 4),
                    Wrap(
                      children: [
                        TextButton(
                          onPressed: () => _steerTask(task),
                          child: const Text('Steer'),
                        ),
                      ],
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
    final scheme = Theme.of(context).colorScheme;
    final textTheme = Theme.of(context).textTheme;
    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
      color: scheme.tertiaryContainer,
      shape: agentCardShape(context),
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
                style: textTheme.titleSmall,
              ),
            ),
            if (approval.reason == 'uncommitted_speech')
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Text(
                  'Live speech is not yet confirmed. Review this exact action.',
                  style: textTheme.bodyMedium,
                ),
              ),
            SelectableText(
              'Action: $args\ndigest:${approval.actionDigest} '
              'revision:${approval.objectiveRevision}'
              '${approval.expiresAt != null ? ' expires:${approval.expiresAt}' : ''}',
            ),
            if (approval.isExpired)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Text(
                  'Expired: this approval cannot be reused.',
                  style: textTheme.bodyMedium?.copyWith(color: scheme.error),
                ),
              ),
            Wrap(
              spacing: 8,
              runSpacing: 8,
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
