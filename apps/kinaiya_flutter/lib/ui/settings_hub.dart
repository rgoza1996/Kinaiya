import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../config/options_catalog.dart';

/// Searchable owner Settings hub.
///
/// Pure navigation: lists the destinations in [settingsEntries] grouped by
/// [SettingsSection] and returns the chosen [SettingsAction] with
/// `Navigator.pop(context, entry.action)`. Opening, searching or closing the
/// hub changes no setting, credential, permission, session or audio state and
/// performs no network or inference work. Unavailable destinations show
/// [SettingsAvailability.unavailableReason] and cannot be selected.
class SettingsHub extends StatefulWidget {
  const SettingsHub({
    super.key,
    required this.availability,
    this.buildInfo = const SettingsBuildInfo.fromEnvironment(),
  });

  final SettingsAvailability availability;
  final SettingsBuildInfo buildInfo;

  @override
  State<SettingsHub> createState() => _SettingsHubState();
}

class _SettingsHubState extends State<SettingsHub> {
  late final TextEditingController _searchController;
  late final FocusNode _searchFocus;
  String _query = '';

  @override
  void initState() {
    super.initState();
    _searchController = TextEditingController();
    _searchFocus = FocusNode();
    _searchController.addListener(_onSearchChanged);
  }

  void _onSearchChanged() {
    final next = _searchController.text;
    if (next == _query) return;
    setState(() => _query = next);
  }

  @override
  void dispose() {
    _searchController.removeListener(_onSearchChanged);
    _searchController.dispose();
    _searchFocus.dispose();
    super.dispose();
  }

  void _close() {
    Navigator.of(context).maybePop();
  }

  void _clearSearch() {
    _searchController.clear();
    _searchFocus.requestFocus();
  }

  IconData _sectionIcon(SettingsSection section) {
    return switch (section) {
      SettingsSection.connection => Icons.link_outlined,
      SettingsSection.appearance => Icons.palette_outlined,
      SettingsSection.converse => Icons.chat_bubble_outline,
      SettingsSection.agent => Icons.smart_toy_outlined,
    };
  }

  IconData _entryIcon(SettingsAction action) {
    return switch (action) {
      SettingsAction.connection => Icons.link_outlined,
      SettingsAction.appearance => Icons.palette_outlined,
      SettingsAction.voice => Icons.mic_outlined,
      SettingsAction.memory => Icons.notes_outlined,
      SettingsAction.agentControls => Icons.tune_outlined,
      SettingsAction.mcp => Icons.extension_outlined,
    };
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final colors = theme.colorScheme;
    final availability = widget.availability;
    final trimmed = _query.trim();
    final List<SettingsEntry> filtered = trimmed.isEmpty
        ? settingsEntries
        : settingsEntries.where((entry) => entry.matches(trimmed)).toList();

    final media = MediaQuery.of(context);
    final double maxHeight = (media.size.height - 32)
        .clamp(280.0, 640.0)
        .toDouble();

    // The heading and search share the scrollable body so an on-screen
    // keyboard or enlarged text cannot push the fixed Close control away.
    return Dialog(
      insetPadding: const EdgeInsets.all(16),
      child: ConstrainedBox(
        constraints: BoxConstraints(maxWidth: 560, maxHeight: maxHeight),
        child: CallbackShortcuts(
          bindings: <ShortcutActivator, VoidCallback>{
            const SingleActivator(LogicalKeyboardKey.escape): _close,
          },
          child: Focus(
            autofocus: true,
            child: Semantics(
              namesRoute: true,
              scopesRoute: true,
              explicitChildNodes: true,
              label: 'Settings',
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: <Widget>[
                  Flexible(
                    child: SingleChildScrollView(
                      padding: const EdgeInsets.fromLTRB(8, 0, 8, 8),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        mainAxisSize: MainAxisSize.min,
                        children: <Widget>[
                          Padding(
                            padding: const EdgeInsets.fromLTRB(20, 20, 20, 4),
                            child: Semantics(
                              header: true,
                              child: Text(
                                'Settings',
                                style: theme.textTheme.titleLarge,
                              ),
                            ),
                          ),
                          Padding(
                            padding: const EdgeInsets.fromLTRB(16, 8, 16, 8),
                            child: TextField(
                              key: const Key('settings-search'),
                              controller: _searchController,
                              focusNode: _searchFocus,
                              maxLength: 128,
                              maxLengthEnforcement:
                                  MaxLengthEnforcement.enforced,
                              textInputAction: TextInputAction.search,
                              buildCounter:
                                  (
                                    context, {
                                    required int currentLength,
                                    required bool isFocused,
                                    int? maxLength,
                                  }) => const SizedBox.shrink(),
                              decoration: InputDecoration(
                                labelText: 'Search settings',
                                hintText: 'Search settings',
                                prefixIcon: const Icon(Icons.search),
                                suffixIcon: _query.isEmpty
                                    ? null
                                    : IconButton(
                                        tooltip: 'Clear search',
                                        onPressed: _clearSearch,
                                        icon: const Icon(Icons.clear),
                                      ),
                              ),
                            ),
                          ),
                          if (filtered.isEmpty)
                            Padding(
                              padding: const EdgeInsets.fromLTRB(
                                16,
                                24,
                                16,
                                24,
                              ),
                              child: Semantics(
                                liveRegion: true,
                                child: Text(
                                  'No settings match your search.',
                                  style: theme.textTheme.bodyMedium,
                                  textAlign: TextAlign.center,
                                ),
                              ),
                            )
                          else
                            for (final section in SettingsSection.values)
                              if (filtered.any(
                                (entry) => entry.section == section,
                              )) ...<Widget>[
                                Padding(
                                  padding: const EdgeInsets.fromLTRB(
                                    12,
                                    12,
                                    12,
                                    4,
                                  ),
                                  child: Semantics(
                                    header: true,
                                    child: Row(
                                      children: <Widget>[
                                        Icon(
                                          _sectionIcon(section),
                                          size: 18,
                                          color: colors.onSurfaceVariant,
                                          semanticLabel: null,
                                        ),
                                        const SizedBox(width: 8),
                                        Expanded(
                                          child: Text(
                                            section.label,
                                            style: theme.textTheme.titleSmall,
                                          ),
                                        ),
                                      ],
                                    ),
                                  ),
                                ),
                                for (final entry in filtered.where(
                                  (entry) => entry.section == section,
                                ))
                                  _HubEntryTile(
                                    key: Key(
                                      'settings-entry-${entry.action.name}',
                                    ),
                                    entry: entry,
                                    icon: _entryIcon(entry.action),
                                    enabled: availability.allows(entry.action),
                                    reason: availability.unavailableReason(
                                      entry.action,
                                    ),
                                  ),
                              ],
                          const Divider(height: 24),
                          Padding(
                            padding: const EdgeInsets.fromLTRB(12, 0, 12, 12),
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              mainAxisSize: MainAxisSize.min,
                              children: <Widget>[
                                Semantics(
                                  header: true,
                                  child: Text(
                                    'About',
                                    style: theme.textTheme.titleSmall,
                                  ),
                                ),
                                const SizedBox(height: 4),
                                SelectableText(
                                  widget.buildInfo.label,
                                  style: theme.textTheme.bodySmall,
                                ),
                                if (widget.buildInfo.revision.isNotEmpty)
                                  Padding(
                                    padding: const EdgeInsets.only(top: 2),
                                    child: SelectableText(
                                      widget.buildInfo.revision,
                                      style: theme.textTheme.bodySmall
                                          ?.copyWith(
                                            color: colors.onSurfaceVariant,
                                          ),
                                    ),
                                  ),
                              ],
                            ),
                          ),
                        ],
                      ),
                    ),
                  ),
                  const Divider(height: 1),
                  Padding(
                    padding: const EdgeInsets.fromLTRB(8, 4, 8, 4),
                    child: Align(
                      alignment: Alignment.centerRight,
                      child: TextButton(
                        key: const Key('settings-close'),
                        onPressed: _close,
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

class _HubEntryTile extends StatelessWidget {
  const _HubEntryTile({
    super.key,
    required this.entry,
    required this.icon,
    required this.enabled,
    required this.reason,
  });

  final SettingsEntry entry;
  final IconData icon;
  final bool enabled;
  final String? reason;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return ListTile(
      leading: Icon(icon),
      title: Text(entry.title),
      subtitle: reason == null
          ? Text(entry.description)
          : Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: <Widget>[
                Text(entry.description),
                const SizedBox(height: 2),
                Text(
                  reason!,
                  style: theme.textTheme.bodySmall?.copyWith(
                    color: theme.colorScheme.onSurfaceVariant,
                  ),
                ),
              ],
            ),
      enabled: enabled,
      onTap: enabled ? () => Navigator.of(context).pop(entry.action) : null,
    );
  }
}
