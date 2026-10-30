import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../config/options_catalog.dart';
import 'design_tokens.dart';

/// Searchable Options page on the existing owner Settings route.
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

  IconData _entryIcon(SettingsAction action) {
    return switch (action) {
      SettingsAction.models => Icons.tune_rounded,
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
    final trimmed = _query.trim();
    final List<SettingsEntry> filtered = trimmed.isEmpty
        ? settingsEntries
        : settingsEntries.where((entry) => entry.matches(trimmed)).toList();

    final media = MediaQuery.of(context);
    // One scroll owner keeps every destination reachable at enlarged text.
    // Close stays outside it, including when the keyboard reduces the route.
    return Dialog(
      insetPadding: const EdgeInsets.all(16),
      backgroundColor: theme.scaffoldBackgroundColor,
      surfaceTintColor: Colors.transparent,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(KinaiyaDesign.radius),
        side: BorderSide(color: colors.outlineVariant),
      ),
      child: ConstrainedBox(
        constraints: BoxConstraints(
          maxWidth: 1400,
          maxHeight: media.size.height - 32,
        ),
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
                      child: LayoutBuilder(
                        builder: (context, constraints) {
                          final wide =
                              constraints.maxWidth >= 960 &&
                              media.textScaler.scale(16) <= 24;
                          final padding = constraints.maxWidth < 600
                              ? 12.0
                              : 28.0;
                          final compactHeader =
                              media.size.height < 600 &&
                              media.textScaler.scale(16) > 24;
                          final preferences = _preferences(
                            theme,
                            filtered,
                            roomy: wide,
                          );
                          final summary = _summary(theme, trimmed.isEmpty);
                          return Padding(
                            padding: EdgeInsets.all(padding),
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.stretch,
                              mainAxisSize: MainAxisSize.min,
                              children: <Widget>[
                                if (media.viewInsets.bottom == 0) ...<Widget>[
                                  Semantics(
                                    header: true,
                                    child: Text(
                                      'Options',
                                      style:
                                          (compactHeader
                                                  ? theme.textTheme.titleLarge
                                                  : theme
                                                        .textTheme
                                                        .headlineLarge)
                                              ?.copyWith(
                                                fontWeight: FontWeight.w700,
                                              ),
                                    ),
                                  ),
                                  if (!compactHeader) ...<Widget>[
                                    const SizedBox(height: 8),
                                    Text(
                                      'Tune your workspace, voice and privacy.',
                                      style: theme.textTheme.bodyMedium
                                          ?.copyWith(
                                            color: colors.onSurfaceVariant,
                                          ),
                                    ),
                                  ],
                                  SizedBox(height: compactHeader ? 16 : 24),
                                ],
                                TextField(
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
                                    border: OutlineInputBorder(
                                      borderRadius: BorderRadius.circular(
                                        KinaiyaDesign.radius,
                                      ),
                                    ),
                                  ),
                                ),
                                const SizedBox(height: 24),
                                if (wide)
                                  Row(
                                    crossAxisAlignment:
                                        CrossAxisAlignment.start,
                                    children: <Widget>[
                                      Expanded(child: preferences),
                                      const SizedBox(width: 24),
                                      SizedBox(width: 320, child: summary),
                                    ],
                                  )
                                else ...<Widget>[
                                  preferences,
                                  const SizedBox(height: 16),
                                  summary,
                                ],
                              ],
                            ),
                          );
                        },
                      ),
                    ),
                  ),
                  const Divider(height: 1),
                  Padding(
                    padding: const EdgeInsets.fromLTRB(16, 8, 16, 8),
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

  Widget _preferences(
    ThemeData theme,
    List<SettingsEntry> filtered, {
    required bool roomy,
  }) => DesignPanel(
    key: const Key('options-preferences'),
    padding: EdgeInsets.all(roomy ? 24 : 12),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: <Widget>[
        Semantics(
          header: true,
          child: Text(
            'Preferences',
            style: theme.textTheme.titleLarge?.copyWith(
              fontWeight: FontWeight.w700,
            ),
          ),
        ),
        const SizedBox(height: 8),
        Text(
          'Settings for Kinaiya on this device',
          style: theme.textTheme.bodySmall?.copyWith(
            color: theme.colorScheme.onSurfaceVariant,
          ),
        ),
        if (filtered.isEmpty)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 24),
            child: Semantics(
              liveRegion: true,
              child: Text(
                'No settings match your search.',
                style: theme.textTheme.bodyMedium,
              ),
            ),
          )
        else
          for (final section in SettingsSection.values)
            if (filtered.any((entry) => entry.section == section)) ...<Widget>[
              Padding(
                padding: const EdgeInsets.only(top: 24, bottom: 4),
                child: Semantics(
                  header: true,
                  child: Text(
                    section.label,
                    style: theme.textTheme.labelLarge?.copyWith(
                      color: theme.colorScheme.onSurfaceVariant,
                    ),
                  ),
                ),
              ),
              for (final entry in filtered.where(
                (entry) => entry.section == section,
              )) ...<Widget>[
                _HubEntryTile(
                  key: Key('settings-entry-${entry.action.name}'),
                  entry: entry,
                  icon: _entryIcon(entry.action),
                  enabled: widget.availability.allows(entry.action),
                  reason: widget.availability.unavailableReason(entry.action),
                  roomy: roomy,
                ),
                const Divider(height: 1),
              ],
            ],
      ],
    ),
  );

  Widget _summary(ThemeData theme, bool showQuickSettings) => Column(
    key: const Key('options-summary'),
    crossAxisAlignment: CrossAxisAlignment.stretch,
    children: <Widget>[
      if (showQuickSettings) ...<Widget>[
        DesignPanel(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: <Widget>[
              Semantics(
                header: true,
                child: Text(
                  'Quick settings',
                  style: theme.textTheme.titleLarge?.copyWith(
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ),
              const SizedBox(height: 8),
              Text(
                'Open the controls you use most.',
                style: theme.textTheme.bodySmall?.copyWith(
                  color: theme.colorScheme.onSurfaceVariant,
                ),
              ),
              const SizedBox(height: 24),
              _quickLink(
                theme,
                SettingsAction.voice,
                title: 'Voice & audio',
                description: 'Capture mode and conversation timing.',
                label: 'Open voice options',
              ),
              const Divider(height: 32),
              _quickLink(
                theme,
                SettingsAction.appearance,
                title: 'Character & motion',
                description: 'Colors, captions and reduced motion.',
                label: 'Open appearance',
              ),
            ],
          ),
        ),
        const SizedBox(height: 16),
      ],
      DesignPanel(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: <Widget>[
            Semantics(
              header: true,
              child: Text(
                'About',
                style: theme.textTheme.titleLarge?.copyWith(
                  fontWeight: FontWeight.w700,
                ),
              ),
            ),
            const SizedBox(height: 12),
            SelectableText(
              widget.buildInfo.label,
              semanticsLabel: widget.buildInfo.label,
              style: theme.textTheme.bodyMedium,
            ),
            if (widget.buildInfo.revision.isNotEmpty) ...<Widget>[
              const SizedBox(height: 8),
              SelectableText(
                widget.buildInfo.revision,
                semanticsLabel: widget.buildInfo.revision,
                style: theme.textTheme.bodySmall?.copyWith(
                  color: theme.colorScheme.onSurfaceVariant,
                ),
              ),
            ],
          ],
        ),
      ),
    ],
  );

  Widget _quickLink(
    ThemeData theme,
    SettingsAction action, {
    required String title,
    required String description,
    required String label,
  }) => Column(
    crossAxisAlignment: CrossAxisAlignment.start,
    children: <Widget>[
      Text(title, style: theme.textTheme.titleSmall),
      const SizedBox(height: 8),
      Text(
        description,
        style: theme.textTheme.bodySmall?.copyWith(
          color: theme.colorScheme.onSurfaceVariant,
        ),
      ),
      const SizedBox(height: 8),
      Material(
        type: MaterialType.transparency,
        child: TextButton(
          key: Key('options-quick-${action.name}'),
          onPressed: widget.availability.allows(action)
              ? () => Navigator.of(context).pop(action)
              : null,
          child: Text(label),
        ),
      ),
    ],
  );
}

class _HubEntryTile extends StatelessWidget {
  const _HubEntryTile({
    super.key,
    required this.entry,
    required this.icon,
    required this.enabled,
    required this.reason,
    required this.roomy,
  });

  final SettingsEntry entry;
  final IconData icon;
  final bool enabled;
  final String? reason;
  final bool roomy;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Material(
      type: MaterialType.transparency,
      child: ListTile(
        contentPadding: const EdgeInsets.symmetric(vertical: 12, horizontal: 4),
        leading: Icon(icon),
        trailing: roomy
            ? Text(
                enabled ? 'Open' : 'Unavailable',
                style: theme.textTheme.labelLarge?.copyWith(
                  color: enabled
                      ? theme.colorScheme.onSurface
                      : theme.colorScheme.onSurfaceVariant,
                ),
              )
            : null,
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
      ),
    );
  }
}
