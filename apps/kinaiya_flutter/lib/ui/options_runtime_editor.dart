import 'package:flutter/material.dart';

import '../api/agent_runtime_settings.dart';
import '../config/options_navigation.dart';
import 'design_tokens.dart';

/// A draft belongs to the observed native state, never to an unobserved refresh.
class OptionsRuntimeDraft {
  OptionsRuntimeDraft(this.base)
    : fileWrites = base.settings.fileWrites,
      shellCommands = base.settings.shellCommands,
      toolNetwork = base.settings.toolNetwork,
      yolo = base.yolo,
      steps = '${base.settings.maxSteps}',
      timeout = '${base.settings.shellTimeout}',
      directory = base.settings.workingDirectory;
  final AgentRuntimeSnapshot base;
  String fileWrites, shellCommands, steps, timeout, directory;
  bool toolNetwork, yolo;

  Map<String, Object?>? get settings {
    final maxSteps = int.tryParse(steps), cap = int.tryParse(timeout);
    if (maxSteps == null ||
        maxSteps < 1 ||
        maxSteps > 2000 ||
        cap == null ||
        cap < 1 ||
        cap > 120 ||
        directory.isEmpty ||
        directory.length > 512 ||
        directory.startsWith('/') ||
        directory.split('/').contains('..') ||
        directory.runes.any((x) => x < 32 || x == 127)) {
      return null;
    }
    // Match the Linux backend's relative Path normalization for exact receipts.
    final parts = directory.split('/').where((x) => x.isNotEmpty && x != '.');
    return {
      'file_writes': fileWrites,
      'shell_commands': shellCommands,
      'tool_network': toolNetwork,
      'max_steps': maxSteps,
      'shell_timeout_s': cap,
      'working_directory': parts.isEmpty ? '.' : parts.join('/'),
    };
  }

  bool get dirty {
    final value = settings;
    return value == null ||
        !AgentExecutionSettings.fromJson(value).matches(base.settings) ||
        yolo != base.yolo;
  }
}

class OptionsRuntimeEditor extends StatefulWidget {
  const OptionsRuntimeEditor({
    super.key,
    required this.snapshot,
    required this.draft,
    required this.page,
    required this.saving,
    required this.onSave,
    required this.onReset,
    this.workspaceDirectoryOnly = false,
  });
  final AgentRuntimeSnapshot snapshot;
  final OptionsRuntimeDraft draft;
  final OptionsPage page;
  final bool saving;
  final Future<void> Function(OptionsRuntimeDraft draft) onSave;
  final VoidCallback onReset;
  final bool workspaceDirectoryOnly;
  @override
  State<OptionsRuntimeEditor> createState() => _OptionsRuntimeEditorState();
}

class _OptionsRuntimeEditorState extends State<OptionsRuntimeEditor> {
  late final _directory = TextEditingController(text: widget.draft.directory);
  late final _steps = TextEditingController(text: widget.draft.steps);
  late final _timeout = TextEditingController(text: widget.draft.timeout);

  @override
  void dispose() {
    _directory.dispose();
    _steps.dispose();
    _timeout.dispose();
    super.dispose();
  }

  Widget _field(String label, String value) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 8),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(label, style: Theme.of(context).textTheme.labelLarge),
        const SizedBox(height: 6),
        SelectableText(value, semanticsLabel: value),
      ],
    ),
  );

  Widget _paths(String label, ExecutionPathList list) => _field(
    label,
    list.paths.isEmpty
        ? 'None configured'
        : '${list.paths.join('\n')}${list.truncated ? '\nShowing ${list.paths.length} of ${list.total} folders.' : ''}',
  );

  Widget _mode(String label, String value, ValueChanged<String> change) =>
      Padding(
        padding: const EdgeInsets.only(bottom: 20),
        child: DropdownButtonFormField<String>(
          key: ValueKey('$label-$value'),
          initialValue: value,
          isExpanded: true,
          decoration: InputDecoration(
            labelText: label,
            border: const OutlineInputBorder(),
          ),
          items: const [
            DropdownMenuItem(value: 'ask', child: Text('Ask for each action')),
            DropdownMenuItem(value: 'allow', child: Text('Allow in workspace')),
            DropdownMenuItem(value: 'deny', child: Text('Deny')),
          ],
          onChanged: widget.saving
              ? null
              : (value) {
                  if (value != null) setState(() => change(value));
                },
        ),
      );

  @override
  Widget build(BuildContext context) {
    final snapshot = widget.snapshot, draft = widget.draft;
    final execution = snapshot.execution;
    final changed = draft.base.stateToken != snapshot.stateToken;
    final permissions = widget.page == OptionsPage.permissions;
    final workspace = widget.page == OptionsPage.workspace;
    return DesignPanel(
      child: Material(
        type: MaterialType.transparency,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(
              permissions
                  ? 'Service-wide execution controls'
                  : workspace
                  ? widget.workspaceDirectoryOnly
                        ? 'Shell starting directory'
                        : 'Approved workspace'
                  : 'Execution limits and isolation',
              style: Theme.of(context).textTheme.titleLarge,
            ),
            const SizedBox(height: 8),
            Text(
              'Agent service · Revision ${snapshot.revision} · '
              '${snapshot.source == 'owner_saved' ? 'Owner settings' : 'Host configuration'}',
            ),
            const SizedBox(height: 8),
            const Text(
              'Changes apply after all tasks and their cleanup have settled. '
              'Saved settings persist on this host; YOLO lasts for this service instance.',
            ),
            if (!snapshot.activeMatchesSaved) ...[
              const SizedBox(height: 12),
              const Text(
                'Active values differ from the saved settings. Review the active values before saving.',
              ),
            ],
            if (changed) ...[
              const SizedBox(height: 12),
              const Text(
                'The host changed while this draft was open. Your draft is retained. '
                'Use active values to discard it and review the latest settings.',
              ),
            ],
            const SizedBox(height: 24),
            if (permissions) ...[
              _mode(
                'Native file writes',
                draft.fileWrites,
                (x) => draft.fileWrites = x,
              ),
              _mode(
                'Run native commands',
                draft.shellCommands,
                (x) => draft.shellCommands = x,
              ),
              const Text(
                'Ask applies to each native file-write action or command. '
                'An allowed command can change workspace files; choosing Deny '
                'for file writes also mounts the workspace read-only for commands.',
              ),
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                title: const Text('Tool network access'),
                subtitle: const Text(
                  'Controls the native shell network namespace and network-dependent tool access. '
                  'Model provider disclosure is configured separately.',
                ),
                value: draft.toolNetwork,
                onChanged: widget.saving
                    ? null
                    : (x) => setState(() => draft.toolNetwork = x),
              ),
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                title: const Text('YOLO for this service instance'),
                subtitle: const Text(
                  'Bypasses Ask for workspace writes and commands. '
                  'Deny, protected folders and isolation still apply.',
                ),
                value: draft.yolo,
                onChanged: widget.saving
                    ? null
                    : (x) => setState(() => draft.yolo = x),
              ),
              _field(
                'Active write behavior',
                snapshot.effectiveWrite
                    ? 'Allowed within the native workspace policy'
                    : snapshot.settings.fileWrites == 'deny'
                    ? 'Denied, including single-use approvals and YOLO'
                    : 'Requires an exact, single-use action approval',
              ),
              _field(
                'Active command behavior',
                snapshot.effectiveShell
                    ? 'Allowed within the native shell policy'
                    : snapshot.settings.shellCommands == 'deny'
                    ? 'Denied, including single-use approvals and YOLO'
                    : 'Requires an exact, single-use action approval',
              ),
              const Text(
                'Pending action approvals remain on their Agent tasks. '
                'The scoped tool editor is separate. These service-wide settings do not replace per-tool rules or other native access boundaries.',
              ),
            ] else if (workspace) ...[
              if (!widget.workspaceDirectoryOnly) ...[
                _field('Workspace root', execution.workspace),
                _field(
                  'Command access to workspace',
                  execution.shellWorkspaceAccess == 'read_only'
                      ? 'Read-only mount'
                      : execution.shellWorkspaceAccess == 'unavailable'
                      ? 'Unavailable until the starting directory is valid'
                      : 'Read/write mount after command authorization',
                ),
              ],
              _field(
                'Active shell directory',
                execution.directory ??
                    'Unavailable. Choose an existing workspace directory below.',
              ),
              TextField(
                key: const Key('options-shell-directory'),
                controller: _directory,
                enabled: !widget.saving,
                maxLength: 512,
                decoration: const InputDecoration(
                  labelText: 'Shell starting directory',
                  helperText:
                      'Relative to the workspace; . uses its root. Folder must already exist.',
                  helperMaxLines: 3,
                  border: OutlineInputBorder(),
                ),
                onChanged: (x) => setState(() => draft.directory = x),
              ),
              if (!widget.workspaceDirectoryOnly) ...[
                _paths('Read-only folders', execution.readonly),
                _paths('Protected folders', execution.protected),
                const Text(
                  'The host selects the approved root and folder restrictions. '
                  'Changing the starting directory keeps the same workspace boundary. '
                  'Workspace relocation and Git worktree creation are not available here yet.',
                ),
              ] else
                const Text(
                  'This relative directory belongs to Agent execution preferences. Set it to . before changing workspace if the same relative folder will not exist in the new root.',
                ),
            ] else ...[
              TextField(
                key: const Key('options-max-steps'),
                controller: _steps,
                enabled: !widget.saving,
                maxLength: 4,
                keyboardType: TextInputType.number,
                decoration: const InputDecoration(
                  labelText: 'Maximum steps per task',
                  helperText: '1–2000 steps',
                  border: OutlineInputBorder(),
                ),
                onChanged: (x) => setState(() => draft.steps = x),
              ),
              const SizedBox(height: 16),
              TextField(
                key: const Key('options-shell-timeout'),
                controller: _timeout,
                enabled: !widget.saving,
                maxLength: 3,
                keyboardType: TextInputType.number,
                decoration: const InputDecoration(
                  labelText: 'Maximum time per native command',
                  helperText:
                      '1–120 whole seconds. A command may request less time.',
                  helperMaxLines: 3,
                  border: OutlineInputBorder(),
                ),
                onChanged: (x) => setState(() => draft.timeout = x),
              ),
              _field('Sandbox', 'Bubblewrap · Required'),
              _field(
                'Sandbox executable',
                execution.binaryDetected
                    ? 'Detected on host'
                    : 'Not detected. Native commands cannot start.',
              ),
              _field(
                'Isolation status',
                execution.isolationVerified
                    ? 'Verified by host'
                    : 'Not verified by this inspection. Execution must establish isolation.',
              ),
              _field(
                'Configured network namespace',
                execution.namespace == 'isolated'
                    ? 'Isolated'
                    : 'Shared host network',
              ),
              _field(
                'Command environment',
                'Host environment cleared\nPATH=/usr/bin:/bin\nHOME=/tmp',
              ),
              const Text(
                'These are a step limit and a per-command timeout. '
                'Whole-task deadlines, concurrency and hardware budgets need separate native controls.',
              ),
            ],
            const SizedBox(height: 24),
            if (snapshot.busy)
              const Padding(
                padding: EdgeInsets.only(bottom: 12),
                child: Text(
                  'Finish, stop or retire pending Agent work before saving. Refresh after cleanup completes.',
                ),
              ),
            if (draft.settings == null)
              const Padding(
                padding: EdgeInsets.only(bottom: 12),
                child: Text(
                  'Check the directory and execution limits before saving.',
                ),
              ),
            Wrap(
              spacing: 12,
              runSpacing: 8,
              children: [
                FilledButton(
                  key: const Key('options-save-runtime'),
                  onPressed:
                      widget.saving ||
                          snapshot.busy ||
                          changed ||
                          !draft.dirty ||
                          draft.settings == null
                      ? null
                      : () => widget.onSave(draft),
                  child: const Text('Save Agent settings'),
                ),
                TextButton(
                  onPressed: widget.saving || !draft.dirty && !changed
                      ? null
                      : widget.onReset,
                  child: const Text('Use active values'),
                ),
              ],
            ),
            const SizedBox(height: 8),
            Text(
              widget.workspaceDirectoryOnly
                  ? 'Save Agent settings includes execution-preference drafts across Permissions and Execution, plus this starting directory. The approved workspace and read-only overlays use their own Save above.'
                  : 'Save includes your draft changes across Permissions, Workspace and Execution.',
            ),
          ],
        ),
      ),
    );
  }
}
