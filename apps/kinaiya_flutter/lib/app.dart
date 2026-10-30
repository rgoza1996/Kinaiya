import 'dart:async';

import 'package:flutter/material.dart';

import 'audio/speech_audio.dart';
import 'config/options_catalog.dart';
import 'state/app_state.dart';
import 'presentation/controller.dart';
import 'ui/presentation_settings.dart';
import 'ui/agent_chat.dart';
import 'ui/character_view.dart';
import 'ui/setup_dialog.dart';
import 'ui/settings_controls.dart';
import 'ui/settings_hub.dart';
import 'ui/model_setup.dart';
import 'ui/converse_memory.dart';
import 'ui/mcp_settings.dart';
import 'ui/visual_theme.dart';
import 'ui/design_tokens.dart';

export 'ui/visual_theme.dart' show kinaiyaIndigo, kinaiyaWhite;

Color? _presentationColor(String? hex) => hex == null
    ? null
    : Color(0xFF000000 | int.parse(hex.substring(1), radix: 16));

class KinaiyaApp extends StatefulWidget {
  const KinaiyaApp({
    super.key,
    required this.state,
    this.audio,
    this.presentation,
  });
  final AppState state;
  final SpeechAudio? audio;
  final PresentationController? presentation;

  @override
  State<KinaiyaApp> createState() => _KinaiyaAppState();
}

class _KinaiyaAppState extends State<KinaiyaApp> {
  late PresentationController _presentation;
  late bool _ownsPresentation;
  Color? _themePrimary, _themeBackground;
  ThemeData? _lightTheme, _darkTheme;

  void _adoptPresentation() {
    _ownsPresentation = widget.presentation == null;
    _presentation = widget.presentation ?? PresentationController();
    unawaited(_presentation.initialize());
  }

  @override
  void initState() {
    super.initState();
    _adoptPresentation();
  }

  @override
  void didUpdateWidget(covariant KinaiyaApp oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.presentation, widget.presentation)) {
      if (_ownsPresentation) _presentation.dispose();
      _adoptPresentation();
    }
  }

  @override
  void dispose() {
    if (_ownsPresentation) _presentation.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: Listenable.merge([widget.state, _presentation]),
    builder: (context, _) {
      final preferences = _presentation.preferences;
      // Bundled character metadata preserves its legacy brand contract; only
      // an imported package supplies an optional interface palette.
      final imported = _presentation.selected?.packageJson != null;
      final pack = preferences.usePackPalette && imported
          ? _presentation.selected?.pack
          : null;
      final primary = _presentationColor(
        preferences.primaryOverride ??
            (pack?.themePrimary.isNotEmpty == true ? pack!.themePrimary : null),
      );
      final background = _presentationColor(
        preferences.backgroundOverride ??
            (pack?.themeBackground.isNotEmpty == true
                ? pack!.themeBackground
                : null),
      );
      if (_lightTheme == null ||
          primary != _themePrimary ||
          background != _themeBackground) {
        _themePrimary = primary;
        _themeBackground = background;
        _lightTheme = kinaiyaTheme(
          primary: primary,
          background: background,
          designSystem: true,
        );
        _darkTheme = kinaiyaTheme(
          designSystem: true,
          brightness: Brightness.dark,
          primary: primary,
          background: background,
        );
      }
      final light = _lightTheme!;
      final dark = _darkTheme!;
      return MaterialApp(
        title: 'Kinaiya',
        theme: light,
        darkTheme: dark,
        themeAnimationDuration: Duration.zero,
        themeMode: widget.state.view == AppView.agent
            ? ThemeMode.dark
            : ThemeMode.light,
        builder: (context, child) {
          final media = MediaQuery.of(context);
          return MediaQuery(
            data: media.copyWith(
              disableAnimations:
                  media.disableAnimations || preferences.reducedMotion,
            ),
            child: child!,
          );
        },
        home: HomeShell(
          key: ObjectKey(widget.state),
          state: widget.state,
          audio: widget.audio,
          presentation: _presentation,
          lightTheme: light,
          darkTheme: dark,
          accent: primary ?? KinaiyaDesign.blue,
          stageBackground: background ?? kinaiyaWhite,
        ),
      );
    },
  );
}

/// Keep voice/task panes mounted across navigation. Hide only their decorative
/// tickers, focus and semantics; changing views is not a runtime stop request.
class HomeShell extends StatefulWidget {
  const HomeShell({
    super.key,
    required this.state,
    this.audio,
    required this.presentation,
    required this.lightTheme,
    required this.darkTheme,
    required this.accent,
    required this.stageBackground,
  });
  final AppState state;
  final SpeechAudio? audio;
  final PresentationController presentation;
  final ThemeData lightTheme, darkTheme;
  final Color accent, stageBackground;
  @override
  State<HomeShell> createState() => _HomeShellState();
}

enum _ShellPage {
  home('Home', Icons.home_outlined),
  converse('Converse', Icons.graphic_eq_rounded),
  agent('Agent Chat', Icons.chat_bubble_outline_rounded),
  character('Character & motion', Icons.person_outline_rounded),
  projects('Projects', Icons.folder_outlined),
  files('Files', Icons.description_outlined),
  media('Media', Icons.photo_library_outlined);

  const _ShellPage(this.label, this.icon);
  final String label;
  final IconData icon;

  String get heading => this == agent ? 'Agent' : label;
}

class _HomeShellState extends State<HomeShell> {
  final _scaffoldKey = GlobalKey<ScaffoldState>();
  final _voice = ValueNotifier(const VoiceActivity());
  final _converseControls = ConverseViewControls();
  final _agentControls = AgentViewControls();
  bool _settingsOpen = false;
  bool _modelSetupOpen = false;
  bool _converseVisible = true;
  bool _railExtended = false;
  _ShellPage? _workspacePage;
  late AppView _observedView;

  @override
  void initState() {
    super.initState();
    _observedView = widget.state.view;
    _workspacePage = widget.state.view == AppView.agent
        ? null
        : _ShellPage.home;
    _converseVisible = widget.presentation.preferences.showConverseView;
    widget.presentation.addListener(_presentationChanged);
    widget.state.addListener(_viewChanged);
  }

  @override
  void didUpdateWidget(covariant HomeShell oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.presentation, widget.presentation)) {
      oldWidget.presentation.removeListener(_presentationChanged);
      widget.presentation.addListener(_presentationChanged);
      _presentationChanged();
    }
    if (!identical(oldWidget.state, widget.state)) {
      oldWidget.state.removeListener(_viewChanged);
      _observedView = widget.state.view;
      _workspacePage = null;
      widget.state.addListener(_viewChanged);
    }
  }

  void _presentationChanged() {
    final visible = widget.presentation.preferences.showConverseView;
    if (mounted && !_closing && _converseVisible != visible) {
      setState(() {
        _converseVisible = visible;
        if (!visible && _workspacePage == _ShellPage.converse) {
          _workspacePage = _ShellPage.home;
        }
      });
    }
  }

  List<_ShellPage> get _destinations => [
    for (final page in _ShellPage.values)
      if (page != _ShellPage.converse ||
          widget.presentation.preferences.showConverseView)
        page,
  ];

  void _viewChanged() {
    if (_observedView == widget.state.view) return;
    _observedView = widget.state.view;
    if (mounted &&
        !_closing &&
        _workspacePage != null &&
        (widget.state.view == AppView.agent ||
            !const {
              _ShellPage.home,
              _ShellPage.converse,
            }.contains(_workspacePage))) {
      setState(() => _workspacePage = null);
    }
  }

  void _selectPage(_ShellPage page) {
    if (_closing) return;
    _scaffoldKey.currentState?.closeDrawer();
    if (page == _ShellPage.character) {
      _appearance();
      return;
    }
    if (page == _ShellPage.home ||
        page == _ShellPage.converse ||
        page == _ShellPage.agent) {
      if (page == _ShellPage.converse &&
          !widget.presentation.preferences.showConverseView) {
        return;
      }
      setState(() => _workspacePage = page == _ShellPage.agent ? null : page);
      widget.state.setView(
        page == _ShellPage.agent ? AppView.agent : AppView.character,
      );
    } else {
      setState(() => _workspacePage = page);
    }
  }

  void _openNavigation() {
    if (!mounted || _closing) return;
    FocusScope.of(context).unfocus();
    _scaffoldKey.currentState?.openDrawer();
  }

  Future<void> _openConverseChat(AppState owner) async {
    if (!mounted || _closing || !identical(widget.state, owner)) return;
    await _converseControls.openTextConversation?.call();
  }

  Future<void> _modelSetup() async {
    if (_closing || _modelSetupOpen) return;
    final owner = widget.state;
    _modelSetupOpen = true;
    try {
      await showDialog<void>(
        context: context,
        builder: (_) => Theme(
          data: widget.lightTheme,
          child: ModelSetupDialog(
            state: owner,
            presentation: widget.presentation,
          ),
        ),
      );
    } finally {
      _modelSetupOpen = false;
    }
  }

  SettingsAvailability get _settingsAvailability => SettingsAvailability(
    voiceOptions: _converseControls.openVoiceOptions != null,
    agentControls:
        widget.state.commandsAvailable && _agentControls.openCommands != null,
    mcpConnections: widget.state.client != null,
  );

  Future<void> _settings() async {
    if (_settingsOpen || _closing) return;
    final owner = widget.state;
    _settingsOpen = true;
    SettingsAction? action;
    try {
      action = await showDialog<SettingsAction>(
        context: context,
        builder: (_) => ListenableBuilder(
          listenable: owner,
          builder: (_, _) => Theme(
            data: widget.darkTheme,
            child: SettingsHub(availability: _settingsAvailability),
          ),
        ),
      );
    } finally {
      _settingsOpen = false;
    }
    if (!mounted ||
        _closing ||
        !identical(widget.state, owner) ||
        action == null) {
      return;
    }
    // The hub is dismissed before any real destination is opened. Availability
    // is read now, never taken from the snapshot that rendered a selected row.
    if (!_settingsAvailability.allows(action)) return;
    switch (action) {
      case SettingsAction.models:
        await _modelSetup();
      case SettingsAction.connection:
        _setup();
      case SettingsAction.appearance:
        _appearance();
      case SettingsAction.memory:
        await showConverseMemoryDialog(context: context, state: owner);
      case SettingsAction.voice:
        await _converseControls.openVoiceOptions?.call();
      case SettingsAction.agentControls:
      case SettingsAction.mcp:
        setState(() => _workspacePage = null);
        owner.setView(AppView.agent);
        await WidgetsBinding.instance.endOfFrame;
        if (!mounted ||
            _closing ||
            !identical(widget.state, owner) ||
            !_settingsAvailability.allows(action)) {
          return;
        }
        if (action == SettingsAction.agentControls) {
          _agentControls.openCommands?.call();
        } else {
          await showDialog<void>(
            context: context,
            builder: (_) => McpSettings(state: owner),
          );
        }
    }
  }

  bool _closing = false;
  @override
  void dispose() {
    _closing = true;
    widget.state.removeListener(_viewChanged);
    widget.presentation.removeListener(_presentationChanged);
    _voice.dispose();
    super.dispose();
  }

  void _setup() => showDialog<void>(
    context: context,
    builder: (_) => SetupDialog(state: widget.state),
  );

  void _appearance() => showDialog<void>(
    context: context,
    builder: (_) => PresentationSettings(controller: widget.presentation),
  );

  @override
  Widget build(BuildContext context) {
    final state = widget.state;
    final page =
        _workspacePage ??
        (state.view == AppView.agent ? _ShellPage.agent : _ShellPage.home);
    final isAgent = page == _ShellPage.agent;
    final isConverse = page == _ShellPage.converse;
    final isVoice = page == _ShellPage.home || isConverse;
    final destinations = _destinations;
    return LayoutBuilder(
      builder: (context, box) {
        final scale = MediaQuery.textScalerOf(context).scale(1);
        final rail = box.maxWidth / scale.clamp(1, 2) >= 900;
        final keyboardOpen = MediaQuery.viewInsetsOf(context).bottom > 0;
        final compressedHeader = keyboardOpen || box.maxHeight < 360;
        return Scaffold(
          key: _scaffoldKey,
          drawer: rail ? null : _navigationDrawer(page),
          body: SafeArea(
            bottom: false,
            child: Row(
              children: [
                if (rail)
                  NavigationRail(
                    extended: _railExtended,
                    scrollable: true,
                    backgroundColor:
                        widget.darkTheme.colorScheme.surfaceContainerLow,
                    indicatorColor: widget.darkTheme.colorScheme.primary,
                    selectedIconTheme: IconThemeData(
                      color: widget.darkTheme.colorScheme.onPrimary,
                    ),
                    unselectedIconTheme: IconThemeData(
                      color: widget.darkTheme.colorScheme.onSurfaceVariant,
                    ),
                    selectedLabelTextStyle: widget
                        .darkTheme
                        .textTheme
                        .labelLarge
                        ?.copyWith(
                          color: widget.darkTheme.colorScheme.onSurface,
                        ),
                    unselectedLabelTextStyle: widget
                        .darkTheme
                        .textTheme
                        .labelLarge
                        ?.copyWith(
                          color: widget.darkTheme.colorScheme.onSurfaceVariant,
                        ),
                    minWidth: 76,
                    minExtendedWidth: 248,
                    groupAlignment: -1,
                    leading: Padding(
                      padding: const EdgeInsets.symmetric(vertical: 20),
                      child: ExcludeSemantics(
                        child: Image.asset(
                          'assets/mascot/kinaiya-mascot.png',
                          width: 48,
                          height: 48,
                        ),
                      ),
                    ),
                    trailingAtBottom: true,
                    trailing: IconButton(
                      key: const ValueKey('navigation-expand'),
                      tooltip: _railExtended
                          ? 'Collapse navigation'
                          : 'Expand navigation',
                      color: widget.darkTheme.colorScheme.onSurface,
                      onPressed: () =>
                          setState(() => _railExtended = !_railExtended),
                      icon: Icon(
                        _railExtended
                            ? Icons.chevron_left
                            : Icons.chevron_right,
                      ),
                    ),
                    selectedIndex: destinations.indexOf(page),
                    onDestinationSelected: (i) {
                      if (i == destinations.length) {
                        _settings();
                      } else {
                        _selectPage(destinations[i]);
                      }
                    },
                    destinations: [
                      for (final destination in _destinations)
                        NavigationRailDestination(
                          icon: Icon(
                            destination.icon,
                            key: ValueKey('navigation-${destination.name}'),
                          ),
                          label: Text(destination.label),
                        ),
                      const NavigationRailDestination(
                        icon: Icon(
                          Icons.settings_outlined,
                          key: ValueKey('navigation-settings'),
                        ),
                        label: Text('Options'),
                      ),
                    ],
                  ),
                Expanded(
                  child: Column(
                    children: [
                      Theme(
                        data: widget.darkTheme,
                        child: Material(
                          color: widget.darkTheme.colorScheme.surface,
                          child: Padding(
                            padding: const EdgeInsets.fromLTRB(20, 8, 12, 8),
                            child: LayoutBuilder(
                              builder: (context, header) {
                                final compact = header.maxWidth / scale >= 420;
                                final title = Column(
                                  crossAxisAlignment: CrossAxisAlignment.start,
                                  mainAxisSize: MainAxisSize.min,
                                  children: [
                                    Text(
                                      'Kinaiya',
                                      style: Theme.of(context)
                                          .textTheme
                                          .titleLarge
                                          ?.copyWith(
                                            fontWeight: FontWeight.w700,
                                          ),
                                    ),
                                    Text(
                                      page.heading,
                                      style: Theme.of(context)
                                          .textTheme
                                          .bodySmall
                                          ?.copyWith(
                                            color: Theme.of(
                                              context,
                                            ).colorScheme.onSurfaceVariant,
                                          ),
                                    ),
                                  ],
                                );
                                final actions = Row(
                                  mainAxisSize: MainAxisSize.min,
                                  children: [
                                    ConnectionDot(state: state),
                                    if (!isAgent)
                                      IconButton(
                                        key: const ValueKey(
                                          'converse-chat-open',
                                        ),
                                        tooltip: 'Converse chat',
                                        icon: const Icon(
                                          Icons.chat_bubble_outline,
                                        ),
                                        onPressed: () =>
                                            _openConverseChat(state),
                                      ),
                                    IconButton(
                                      key: const ValueKey('settings-open'),
                                      tooltip: 'Settings',
                                      icon: const Icon(Icons.settings_outlined),
                                      onPressed: _settings,
                                    ),
                                    IconButton(
                                      tooltip: 'Appearance',
                                      icon: const Icon(Icons.palette_outlined),
                                      onPressed: _appearance,
                                    ),
                                    IconButton(
                                      tooltip: 'Connection setup',
                                      icon: const Icon(Icons.tune_rounded),
                                      onPressed: _setup,
                                    ),
                                  ],
                                );
                                if (compressedHeader) {
                                  return Row(
                                    children: [
                                      if (!rail)
                                        IconButton(
                                          key: const ValueKey(
                                            'navigation-menu',
                                          ),
                                          tooltip: 'Open navigation',
                                          onPressed: _openNavigation,
                                          icon: const Icon(Icons.menu),
                                        ),
                                      Expanded(
                                        child: Text(
                                          page.heading,
                                          maxLines: 1,
                                          overflow: TextOverflow.ellipsis,
                                          style: Theme.of(
                                            context,
                                          ).textTheme.titleMedium,
                                        ),
                                      ),
                                      actions,
                                    ],
                                  );
                                }
                                return compact
                                    ? Row(
                                        children: [
                                          if (!rail)
                                            IconButton(
                                              key: const ValueKey(
                                                'navigation-menu',
                                              ),
                                              tooltip: 'Open navigation',
                                              onPressed: _openNavigation,
                                              icon: const Icon(Icons.menu),
                                            ),
                                          Expanded(child: title),
                                          actions,
                                        ],
                                      )
                                    : Column(
                                        crossAxisAlignment:
                                            CrossAxisAlignment.start,
                                        children: [
                                          Row(
                                            children: [
                                              if (!rail)
                                                IconButton(
                                                  key: const ValueKey(
                                                    'navigation-menu',
                                                  ),
                                                  tooltip: 'Open navigation',
                                                  onPressed: _openNavigation,
                                                  icon: const Icon(Icons.menu),
                                                ),
                                              Expanded(child: title),
                                            ],
                                          ),
                                          Align(
                                            alignment: Alignment.centerRight,
                                            child: actions,
                                          ),
                                        ],
                                      );
                              },
                            ),
                          ),
                        ),
                      ),
                      if (!state.connected && state.connectionError != null)
                        MaterialBanner(
                          content: Text(
                            state.connectionError!,
                            maxLines: 3,
                            overflow: TextOverflow.ellipsis,
                          ),
                          actions: [
                            TextButton(
                              onPressed: _setup,
                              child: const Text('SETUP'),
                            ),
                          ],
                        ),
                      ValueListenableBuilder<VoiceActivity>(
                        valueListenable: _voice,
                        builder: (context, voice, _) => voice.active
                            ? _voiceBar(voice)
                            : const SizedBox.shrink(),
                      ),
                      Expanded(
                        child: Stack(
                          fit: StackFit.expand,
                          children: [
                            Offstage(
                              offstage: !isVoice,
                              child: ExcludeFocus(
                                excluding: !isVoice,
                                child: TickerMode(
                                  enabled: isVoice,
                                  child: Theme(
                                    data: isConverse
                                        ? widget.darkTheme
                                        : widget.lightTheme,
                                    child: Material(
                                      type: MaterialType.transparency,
                                      child: CharacterView(
                                        surface: isConverse
                                            ? CharacterSurface.interaction
                                            : CharacterSurface.home,
                                        onModelSettings: _modelSetup,
                                        showConverseView: widget
                                            .presentation
                                            .preferences
                                            .showConverseView,
                                        voiceOnlyControls: true,
                                        useDesignScene:
                                            widget
                                                    .presentation
                                                    .selected
                                                    ?.packageJson ==
                                                null &&
                                            widget
                                                    .presentation
                                                    .preferences
                                                    .primaryOverride ==
                                                null &&
                                            widget
                                                    .presentation
                                                    .preferences
                                                    .backgroundOverride ==
                                                null,
                                        state: state,
                                        audio: widget.audio,
                                        controls: _converseControls,
                                        character: widget.presentation.selected,
                                        showCharacter: widget
                                            .presentation
                                            .preferences
                                            .showCharacter,
                                        showCaptions: widget
                                            .presentation
                                            .preferences
                                            .showCaptions,
                                        scenePrimary: widget.accent,
                                        sceneBackground: widget.stageBackground,
                                        onActivityChanged: (activity) {
                                          if (!_closing) {
                                            _voice.value = activity;
                                          }
                                        },
                                      ),
                                    ),
                                  ),
                                ),
                              ),
                            ),
                            Offstage(
                              offstage: !isAgent,
                              child: ExcludeFocus(
                                excluding: !isAgent,
                                child: TickerMode(
                                  enabled: isAgent,
                                  child: Theme(
                                    data: widget.darkTheme,
                                    child: AgentChat(
                                      state: state,
                                      controls: _agentControls,
                                    ),
                                  ),
                                ),
                              ),
                            ),
                            if (!isVoice && !isAgent) _workspaceContent(page),
                          ],
                        ),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
        );
      },
    );
  }

  Widget _navigationDrawer(_ShellPage page) => Theme(
    data: widget.darkTheme,
    child: Drawer(
      backgroundColor: widget.darkTheme.colorScheme.surfaceContainerLow,
      child: SafeArea(
        child: Column(
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 8, 8, 8),
              child: Row(
                children: [
                  Expanded(
                    child: Semantics(
                      header: true,
                      child: Text(
                        'Navigation',
                        style: widget.darkTheme.textTheme.titleMedium,
                      ),
                    ),
                  ),
                  IconButton(
                    key: const ValueKey('navigation-close'),
                    tooltip: 'Close navigation',
                    onPressed: () => _scaffoldKey.currentState?.closeDrawer(),
                    icon: const Icon(Icons.close),
                  ),
                ],
              ),
            ),
            const Divider(height: 1),
            Expanded(
              child: ListView(
                padding: const EdgeInsets.symmetric(vertical: 8),
                children: [
                  for (final destination in _destinations)
                    ListTile(
                      key: ValueKey('navigation-${destination.name}'),
                      leading: Icon(destination.icon),
                      title: Text(destination.label),
                      selected: destination == page,
                      onTap: () => _selectPage(destination),
                    ),
                  ListTile(
                    key: const ValueKey('navigation-settings'),
                    leading: const Icon(Icons.settings_outlined),
                    title: const Text('Options'),
                    onTap: () {
                      _scaffoldKey.currentState?.closeDrawer();
                      _settings();
                    },
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    ),
  );

  Widget _workspaceContent(_ShellPage page) {
    final home = page == _ShellPage.home;
    final description = switch (page) {
      _ShellPage.home =>
        'Choose where you want to start. Your conversations stay open when you switch sections.',
      _ShellPage.character =>
        'Character and motion settings are available from Appearance.',
      _ShellPage.files =>
        'Workspace file browsing is planned. Opening this page does not read files or grant Agent access.',
      _ShellPage.projects =>
        'Project management is not connected to this host yet. Existing Agent sessions remain available in Agent.',
      _ShellPage.media =>
        'A generated media library is not connected to this host yet. No example files are shown as your media.',
      _ShellPage.converse || _ShellPage.agent => '',
    };
    return Theme(
      data: widget.darkTheme,
      child: Material(
        color: widget.darkTheme.scaffoldBackgroundColor,
        child: SingleChildScrollView(
          key: ValueKey('workspace-${page.name}'),
          padding: const EdgeInsets.all(24),
          child: Align(
            alignment: Alignment.topCenter,
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 840),
              child: DesignPanel(
                child: Material(
                  type: MaterialType.transparency,
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Icon(
                        page.icon,
                        size: 32,
                        color: widget.darkTheme.colorScheme.primary,
                      ),
                      const SizedBox(height: 16),
                      Semantics(
                        header: true,
                        child: Text(
                          page.label,
                          style: widget.darkTheme.textTheme.headlineMedium,
                        ),
                      ),
                      const SizedBox(height: 12),
                      Text(
                        description,
                        style: widget.darkTheme.textTheme.bodyLarge,
                      ),
                      const SizedBox(height: 24),
                      Wrap(
                        spacing: 12,
                        runSpacing: 12,
                        children: [
                          if (home || page == _ShellPage.character)
                            FilledButton.icon(
                              onPressed: () => _selectPage(_ShellPage.converse),
                              icon: const Icon(Icons.graphic_eq_rounded),
                              label: const Text('Open Converse'),
                            ),
                          if (page != _ShellPage.character)
                            OutlinedButton.icon(
                              onPressed: () => _selectPage(_ShellPage.agent),
                              icon: const Icon(
                                Icons.chat_bubble_outline_rounded,
                              ),
                              label: const Text('Open Agent Chat'),
                            ),
                          if (home)
                            OutlinedButton.icon(
                              onPressed: _settings,
                              icon: const Icon(Icons.settings_outlined),
                              label: const Text('Open Options'),
                            ),
                        ],
                      ),
                    ],
                  ),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }

  Widget _voiceBar(VoiceActivity voice) => Material(
    color: Theme.of(context).colorScheme.surfaceContainerHigh,
    child: Padding(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 2),
      child: Wrap(
        spacing: 12,
        crossAxisAlignment: WrapCrossAlignment.center,
        children: [
          Text(
            voice.stopPending
                ? 'Stopping microphone…'
                : voice.stopUnconfirmed
                ? 'Microphone stop unconfirmed'
                : voice.recording
                ? 'Recording a voice message'
                : voice.listening
                ? 'Live microphone on'
                : 'Converse is speaking',
          ),
          if (voice.listening || voice.recording || voice.stopUnconfirmed)
            TextButton.icon(
              onPressed: voice.stopPending ? null : voice.stopMicrophone,
              icon: const Icon(Icons.mic_off_rounded),
              label: const Text('Stop microphone'),
            ),
          if (voice.speaking)
            TextButton.icon(
              onPressed: voice.stopSpeech,
              icon: const Icon(Icons.volume_off_rounded),
              label: const Text('Stop speech'),
            ),
        ],
      ),
    ),
  );
}

class ConnectionDot extends StatelessWidget {
  const ConnectionDot({super.key, required this.state});
  final AppState state;
  @override
  Widget build(BuildContext context) {
    final label = state.connected ? 'Connection checked' : 'Not connected';
    final detail =
        state.connectionError ??
        (state.connected
            ? 'Services checked.'
            : 'Open Connection setup to enter service URLs and the owner token.');
    return Tooltip(
      message: detail,
      child: Semantics(
        label: 'Connection status: $label. $detail',
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 12),
          child: Icon(
            Icons.circle,
            size: 12,
            color: state.connected
                ? const Color(0xFF63E6B2)
                : const Color(0xFFDAD8FF),
            semanticLabel: label,
          ),
        ),
      ),
    );
  }
}
