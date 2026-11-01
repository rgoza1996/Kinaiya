import 'package:flutter/material.dart';

import 'agent_design.dart';

/// Immutable description of one registered harness for presentation only.
///
/// Selection is inspection, not promotion: tapping an entry (even when
/// [selectable] is false) only notifies the owner via `onSelect` so the
/// owner can inspect eligibility. This widget never activates anything.
@immutable
class HarnessSidebarEntry {
  const HarnessSidebarEntry({
    required this.id,
    required this.name,
    required this.kind,
    required this.description,
    required this.active,
    required this.selectable,
    this.eligibility,
  });

  final String id;
  final String name;
  final String kind;
  final String description;
  final bool active;
  final bool selectable;
  final String? eligibility;
}

/// Left sidebar: real registered custom/generated harnesses plus the
/// protected Kinaiya Core card.
///
/// Root owns width/wrapping and supplies real [entries], selection and
/// callbacks. Search text is local-only state; external state stays immutable.
class AgentHarnessSidebar extends StatefulWidget {
  const AgentHarnessSidebar({
    super.key,
    required this.entries,
    required this.selectedId,
    required this.collapsed,
    required this.loading,
    required this.error,
    required this.onToggle,
    required this.onRefresh,
    required this.onSelect,
    this.showBorder = true,
  });

  final List<HarnessSidebarEntry> entries;
  final String? selectedId;
  final bool collapsed;
  final bool loading;
  final String? error;
  final VoidCallback onToggle;
  final VoidCallback onRefresh;
  final ValueChanged<String> onSelect;
  final bool showBorder;

  @override
  State<AgentHarnessSidebar> createState() => _AgentHarnessSidebarState();
}

class _AgentHarnessSidebarState extends State<AgentHarnessSidebar> {
  String _query = '';
  final TextEditingController _search = TextEditingController();

  @override
  void dispose() {
    _search.dispose();
    super.dispose();
  }

  List<HarnessSidebarEntry> get _filtered {
    final q = _query.trim().toLowerCase();
    if (q.isEmpty) return widget.entries;
    return widget.entries.where((e) {
      return e.name.toLowerCase().contains(q) ||
          e.id.toLowerCase().contains(q) ||
          e.kind.toLowerCase().contains(q) ||
          e.description.toLowerCase().contains(q);
    }).toList();
  }

  @override
  Widget build(BuildContext context) {
    final width = widget.collapsed ? 48.0 : 246.0;
    return SizedBox(
      width: width,
      child: Container(
        key: const Key('agent-harness-sidebar'),
        decoration: agentWorkspaceDecoration(
          context,
          showBorder: widget.showBorder,
        ),
        child: widget.collapsed
            ? _buildCollapsed(context)
            : _buildExpanded(context),
      ),
    );
  }

  Widget _buildCollapsed(BuildContext context) {
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        const SizedBox(height: 8),
        IconButton(
          tooltip: 'Expand harnesses',
          onPressed: widget.onToggle,
          icon: const Icon(Icons.chevron_right),
        ),
      ],
    );
  }

  Widget _buildExpanded(BuildContext context) {
    final theme = Theme.of(context);
    final filtered = _filtered;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(12, 12, 4, 4),
          child: Row(
            children: [
              Expanded(
                child: Tooltip(
                  message: 'Harnesses',
                  child: Text(
                    'Harnesses',
                    style: theme.textTheme.titleSmall,
                    softWrap: true,
                  ),
                ),
              ),
              IconButton(
                tooltip: 'Collapse harnesses',
                onPressed: widget.onToggle,
                icon: const Icon(Icons.chevron_left),
              ),
            ],
          ),
        ),
        Padding(
          padding: const EdgeInsets.fromLTRB(12, 0, 12, 8),
          child: Row(
            children: [
              Expanded(
                child: Semantics(
                  label: 'Search harnesses',
                  child: TextField(
                    controller: _search,
                    decoration: const InputDecoration(
                      hintText: 'Search',
                      prefixIcon: Icon(Icons.search),
                      isDense: true,
                      border: OutlineInputBorder(),
                    ),
                    onChanged: (v) => setState(() => _query = v),
                  ),
                ),
              ),
              const SizedBox(width: 4),
              IconButton(
                tooltip: 'Refresh harnesses',
                onPressed: widget.onRefresh,
                icon: const Icon(Icons.refresh),
              ),
            ],
          ),
        ),
        Expanded(child: _buildBody(context, filtered)),
      ],
    );
  }

  Widget _buildBody(BuildContext context, List<HarnessSidebarEntry> filtered) {
    final theme = Theme.of(context);
    if (widget.loading) {
      return const Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            CircularProgressIndicator(),
            SizedBox(height: 8),
            Text('Loading harnesses…', softWrap: true),
          ],
        ),
      );
    }
    final error = widget.error;
    if (error != null) {
      return SingleChildScrollView(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Tooltip(
              message: error,
              child: Text(
                error,
                style: theme.textTheme.bodyMedium,
                softWrap: true,
              ),
            ),
            const SizedBox(height: 8),
            OutlinedButton(
              onPressed: widget.onRefresh,
              child: const Text('Retry'),
            ),
          ],
        ),
      );
    }
    return ListView(
      padding: const EdgeInsets.fromLTRB(8, 0, 8, 12),
      children: [
        _coreCard(context),
        const SizedBox(height: 8),
        if (filtered.isEmpty)
          Padding(
            padding: const EdgeInsets.all(12),
            child: Tooltip(
              message: 'No harnesses found',
              child: Text(
                _query.trim().isEmpty
                    ? 'No harnesses registered.'
                    : 'No harnesses found.',
                style: agentMutedStyle(context),
                softWrap: true,
              ),
            ),
          )
        else
          for (final entry in filtered) _entryTile(context, entry),
      ],
    );
  }

  Widget _coreCard(BuildContext context) {
    final theme = Theme.of(context);
    // Protected descriptive card: no activation callback is attached.
    return Card(
      shape: agentCardShape(context),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Tooltip(
              message: 'Kinaiya Core',
              child: Text(
                'Kinaiya Core',
                style: theme.textTheme.titleSmall,
                softWrap: true,
              ),
            ),
            const SizedBox(height: 4),
            Text(
              'Protected core. Descriptive only; it cannot be activated from here.',
              style: agentMutedStyle(context),
              softWrap: true,
            ),
          ],
        ),
      ),
    );
  }

  Widget _entryTile(BuildContext context, HarnessSidebarEntry entry) {
    final theme = Theme.of(context);
    final selected = widget.selectedId == entry.id;
    final status = entry.active ? 'Active' : 'Inactive';
    final eligibility = entry.eligibility;
    return Semantics(
      selected: selected,
      label: entry.name,
      button: true,
      child: Card(
        key: Key('harness-${entry.id}'),
        shape: agentTileShape(),
        color: selected ? theme.colorScheme.primaryContainer : null,
        child: InkWell(
          borderRadius: BorderRadius.circular(12),
          onTap: () => widget.onSelect(entry.id),
          child: Padding(
            padding: const EdgeInsets.all(12),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Tooltip(
                  message: entry.name,
                  child: Text(
                    entry.name,
                    style: theme.textTheme.bodyMedium?.copyWith(
                      fontWeight: selected ? FontWeight.w600 : null,
                    ),
                    softWrap: true,
                  ),
                ),
                const SizedBox(height: 2),
                Tooltip(
                  message: entry.kind,
                  child: Text(
                    entry.kind,
                    style: agentMutedStyle(context, theme.textTheme.bodySmall),
                    softWrap: true,
                  ),
                ),
                const SizedBox(height: 2),
                Tooltip(
                  message: entry.description,
                  child: Text(
                    entry.description,
                    style: agentMutedStyle(context, theme.textTheme.bodySmall),
                    softWrap: true,
                  ),
                ),
                const SizedBox(height: 4),
                Wrap(
                  spacing: 6,
                  runSpacing: 4,
                  children: [
                    Tooltip(
                      message: status,
                      child: Text(
                        status,
                        style: theme.textTheme.labelSmall,
                        softWrap: true,
                      ),
                    ),
                    if (!entry.selectable)
                      Tooltip(
                        message: eligibility ?? 'Not selectable',
                        child: Text(
                          eligibility?.isNotEmpty == true
                              ? eligibility!
                              : 'Not selectable',
                          style: theme.textTheme.labelSmall,
                          softWrap: true,
                        ),
                      )
                    else if (eligibility?.isNotEmpty == true)
                      Tooltip(
                        message: eligibility!,
                        child: Text(
                          eligibility,
                          style: theme.textTheme.labelSmall,
                          softWrap: true,
                        ),
                      ),
                    if (selected)
                      Text(
                        'Selected',
                        style: theme.textTheme.labelSmall,
                        softWrap: true,
                      ),
                  ],
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

/// Immutable description of one real harness session for presentation only.
@immutable
class SessionSidebarEntry {
  const SessionSidebarEntry({
    required this.id,
    required this.label,
    required this.selected,
    this.harnessVersion,
    this.subtitle,
  });

  final String id;
  final String label;
  final bool selected;
  final String? harnessVersion;
  final String? subtitle;
}

/// Second sidebar: sessions for the selected harness plus New session and
/// owner-supplied controls.
///
/// Root supplies real sessions and selection flags. Search text (if used) is
/// local-only. [controls] is rendered below the session list.
class AgentSessionSidebar extends StatefulWidget {
  const AgentSessionSidebar({
    super.key,
    required this.entries,
    required this.harnessLabel,
    required this.collapsed,
    required this.loading,
    required this.error,
    required this.canCreate,
    required this.hasMore,
    required this.onToggle,
    required this.onRefresh,
    required this.onCreate,
    required this.onLoadMore,
    required this.onSelect,
    required this.controls,
    this.showBorder = true,
  });

  final List<SessionSidebarEntry> entries;
  final String harnessLabel;
  final bool collapsed;
  final bool loading;
  final String? error;
  final bool canCreate;
  final bool hasMore;
  final VoidCallback onToggle;
  final VoidCallback onRefresh;
  final VoidCallback onCreate;
  final VoidCallback onLoadMore;
  final ValueChanged<String> onSelect;
  final Widget controls;
  final bool showBorder;

  @override
  State<AgentSessionSidebar> createState() => _AgentSessionSidebarState();
}

class _AgentSessionSidebarState extends State<AgentSessionSidebar> {
  final String _query = '';

  List<SessionSidebarEntry> get _filtered {
    final q = _query.trim().toLowerCase();
    if (q.isEmpty) return widget.entries;
    return widget.entries.where((e) {
      return e.label.toLowerCase().contains(q) ||
          e.id.toLowerCase().contains(q) ||
          (e.subtitle?.toLowerCase().contains(q) ?? false) ||
          (e.harnessVersion?.toLowerCase().contains(q) ?? false);
    }).toList();
  }

  @override
  Widget build(BuildContext context) {
    final width = widget.collapsed ? 48.0 : 260.0;
    return SizedBox(
      width: width,
      child: Container(
        key: const Key('agent-session-sidebar'),
        decoration: agentWorkspaceDecoration(
          context,
          showBorder: widget.showBorder,
        ),
        child: widget.collapsed ? _buildCollapsed() : _buildExpanded(context),
      ),
    );
  }

  Widget _buildCollapsed() {
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        const SizedBox(height: 8),
        IconButton(
          tooltip: 'Expand sessions',
          onPressed: widget.onToggle,
          icon: const Icon(Icons.chevron_right),
        ),
      ],
    );
  }

  Widget _buildExpanded(BuildContext context) {
    final theme = Theme.of(context);
    return LayoutBuilder(
      builder: (context, box) {
        final minimum = MediaQuery.textScalerOf(context).scale(180) + 220;
        return SingleChildScrollView(
          child: SizedBox(
            height: box.maxHeight < minimum ? minimum : box.maxHeight,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Padding(
                  padding: const EdgeInsets.fromLTRB(12, 12, 4, 0),
                  child: Row(
                    children: [
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Tooltip(
                              message: 'Workspace',
                              child: Text(
                                'Workspace',
                                style: theme.textTheme.titleSmall,
                                softWrap: true,
                              ),
                            ),
                            Tooltip(
                              message: widget.harnessLabel,
                              child: Text(
                                widget.harnessLabel,
                                maxLines: 2,
                                overflow: TextOverflow.ellipsis,
                                style: agentMutedStyle(
                                  context,
                                  theme.textTheme.bodySmall,
                                ),
                                softWrap: true,
                              ),
                            ),
                          ],
                        ),
                      ),
                      IconButton(
                        tooltip: 'Collapse sessions',
                        onPressed: widget.onToggle,
                        icon: const Icon(Icons.chevron_left),
                      ),
                    ],
                  ),
                ),
                Padding(
                  padding: const EdgeInsets.fromLTRB(12, 8, 12, 4),
                  child: Row(
                    children: [
                      Expanded(
                        child: Tooltip(
                          message: 'Sessions',
                          child: Text(
                            'Sessions',
                            style: theme.textTheme.labelLarge,
                            softWrap: true,
                          ),
                        ),
                      ),
                      IconButton(
                        tooltip: 'Refresh sessions',
                        onPressed: widget.onRefresh,
                        icon: const Icon(Icons.refresh),
                      ),
                    ],
                  ),
                ),
                Padding(
                  padding: const EdgeInsets.fromLTRB(12, 0, 12, 8),
                  child: OutlinedButton.icon(
                    onPressed: widget.canCreate ? widget.onCreate : null,
                    icon: const Icon(Icons.add),
                    label: const Text('New session', softWrap: true),
                  ),
                ),
                Expanded(child: _buildList(context)),
                const Divider(height: 1),
                Flexible(
                  child: SingleChildScrollView(
                    padding: const EdgeInsets.all(8),
                    child: widget.controls,
                  ),
                ),
              ],
            ),
          ),
        );
      },
    );
  }

  Widget _buildList(BuildContext context) {
    final theme = Theme.of(context);
    if (widget.loading) {
      return const Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            CircularProgressIndicator(),
            SizedBox(height: 8),
            Text('Loading sessions…', softWrap: true),
          ],
        ),
      );
    }
    final error = widget.error;
    if (error != null) {
      return SingleChildScrollView(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Tooltip(
              message: error,
              child: Text(
                error,
                style: theme.textTheme.bodyMedium,
                softWrap: true,
              ),
            ),
            const SizedBox(height: 8),
            OutlinedButton(
              onPressed: widget.onRefresh,
              child: const Text('Retry'),
            ),
          ],
        ),
      );
    }
    final filtered = _filtered;
    if (filtered.isEmpty) {
      return SingleChildScrollView(
        padding: const EdgeInsets.all(12),
        child: Tooltip(
          message: 'No sessions yet',
          child: Text(
            'No sessions yet.',
            style: agentMutedStyle(context),
            softWrap: true,
          ),
        ),
      );
    }
    return ListView(
      padding: const EdgeInsets.fromLTRB(8, 0, 8, 12),
      children: [
        for (final entry in filtered) _sessionTile(context, entry),
        if (widget.hasMore)
          Padding(
            padding: const EdgeInsets.only(top: 4),
            child: OutlinedButton(
              onPressed: widget.onLoadMore,
              child: const Text('Load more', softWrap: true),
            ),
          ),
      ],
    );
  }

  Widget _sessionTile(BuildContext context, SessionSidebarEntry entry) {
    final theme = Theme.of(context);
    return Semantics(
      selected: entry.selected,
      label: entry.label,
      button: true,
      child: Card(
        key: Key('session-${entry.id}'),
        shape: agentTileShape(),
        color: entry.selected ? theme.colorScheme.primaryContainer : null,
        child: InkWell(
          borderRadius: BorderRadius.circular(12),
          onTap: () => widget.onSelect(entry.id),
          child: Padding(
            padding: const EdgeInsets.all(12),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Tooltip(
                  message: entry.label,
                  child: Text(
                    entry.label,
                    style: theme.textTheme.bodyMedium?.copyWith(
                      fontWeight: entry.selected ? FontWeight.w600 : null,
                    ),
                    softWrap: true,
                  ),
                ),
                if (entry.harnessVersion?.isNotEmpty == true) ...[
                  const SizedBox(height: 2),
                  Tooltip(
                    message: entry.harnessVersion!,
                    child: Text(
                      entry.harnessVersion!,
                      style: agentMutedStyle(
                        context,
                        theme.textTheme.bodySmall,
                      ),
                      softWrap: true,
                    ),
                  ),
                ],
                if (entry.subtitle?.isNotEmpty == true) ...[
                  const SizedBox(height: 2),
                  Tooltip(
                    message: entry.subtitle!,
                    child: Text(
                      entry.subtitle!,
                      style: agentMutedStyle(
                        context,
                        theme.textTheme.bodySmall,
                      ),
                      softWrap: true,
                    ),
                  ),
                ],
                if (entry.selected)
                  Padding(
                    padding: const EdgeInsets.only(top: 4),
                    child: Text(
                      'Selected',
                      style: theme.textTheme.labelSmall,
                      softWrap: true,
                    ),
                  ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
