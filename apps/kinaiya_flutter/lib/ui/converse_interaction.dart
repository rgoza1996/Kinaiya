import 'package:flutter/material.dart';

import 'design_tokens.dart';

/// Passive Converse composition. The owner supplies all content and actions;
/// this widget never owns a session, microphone, audio adapter or character.
class ConverseInteraction extends StatelessWidget {
  const ConverseInteraction({
    super.key,
    required this.character,
    required this.transcript,
    required this.composer,
    required this.modelSummary,
    required this.status,
    required this.callLabel,
    this.onCall,
    this.onMicrophone,
    required this.microphoneLabel,
    required this.onModelSettings,
    required this.onHistory,
    required this.onVoiceOptions,
    required this.onMemory,
    this.activeVoice = false,
  });

  final Widget character, transcript, composer, modelSummary;
  final String status, callLabel, microphoneLabel;
  final VoidCallback? onCall, onMicrophone;
  final VoidCallback onModelSettings, onHistory, onVoiceOptions, onMemory;
  final bool activeVoice;

  static const _cameraHint = 'Camera input is not supported by this host';

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final textScale = MediaQuery.textScalerOf(context).scale(16) / 16;
    return ColoredBox(
      color: theme.colorScheme.surface,
      child: LayoutBuilder(
        builder: (context, constraints) {
          final wide = constraints.maxWidth >= 980 && textScale <= 1.4;
          final padding = wide ? 32.0 : 16.0;
          final width = constraints.maxWidth - padding * 2;
          final stageWidth = wide ? (width - 22) * (880 / 1362) : width;
          final sideWidth = wide ? width - 22 - stageWidth : width;
          return SingleChildScrollView(
            key: const ValueKey('converse-interaction-scroll'),
            padding: EdgeInsets.all(padding),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                _heading(context),
                const SizedBox(height: 28),
                // Keep the character at the same element location when the
                // direction changes. Its state belongs to the supplied owner.
                Flex(
                  direction: wide ? Axis.horizontal : Axis.vertical,
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    SizedBox(
                      width: stageWidth,
                      child: _stage(context, wide: wide),
                    ),
                    SizedBox(width: wide ? 22 : 0, height: wide ? 0 : 22),
                    SizedBox(
                      width: sideWidth,
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          _conversation(context, wide: wide),
                          const SizedBox(height: 22),
                          DesignPanel(
                            key: const ValueKey('converse-model-summary'),
                            padding: EdgeInsets.all(wide ? 24 : 16),
                            child: modelSummary,
                          ),
                        ],
                      ),
                    ),
                  ],
                ),
              ],
            ),
          );
        },
      ),
    );
  }

  Widget _heading(BuildContext context) {
    final theme = Theme.of(context);
    return Wrap(
      alignment: WrapAlignment.spaceBetween,
      crossAxisAlignment: WrapCrossAlignment.center,
      spacing: 24,
      runSpacing: 16,
      children: [
        Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              'Converse',
              style: theme.textTheme.headlineLarge?.copyWith(
                fontSize: 36,
                fontWeight: FontWeight.w700,
              ),
            ),
            const SizedBox(height: 8),
            Text(
              'Talk with Kinaiya',
              style: theme.textTheme.titleMedium?.copyWith(
                color: theme.colorScheme.onSurfaceVariant,
              ),
            ),
          ],
        ),
        Wrap(
          spacing: 4,
          runSpacing: 4,
          children: [
            TextButton.icon(
              key: const ValueKey('converse-history'),
              onPressed: onHistory,
              icon: const Icon(Icons.history, size: 20),
              label: const Text('History'),
            ),
            TextButton.icon(
              key: const ValueKey('converse-voice-options'),
              onPressed: onVoiceOptions,
              icon: const Icon(Icons.tune, size: 20),
              label: const Text('Voice options'),
            ),
            TextButton.icon(
              key: const ValueKey('converse-memory'),
              onPressed: onMemory,
              icon: const Icon(Icons.bookmark_outline, size: 20),
              label: const Text('Memory'),
            ),
          ],
        ),
      ],
    );
  }

  Widget _stage(BuildContext context, {required bool wide}) {
    final theme = Theme.of(context);
    return DesignPanel(
      key: const ValueKey('converse-character-panel'),
      padding: EdgeInsets.all(wide ? 24 : 16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Wrap(
            crossAxisAlignment: WrapCrossAlignment.center,
            spacing: 24,
            runSpacing: 8,
            children: [
              Text(
                'Kinaiya',
                style: theme.textTheme.titleLarge?.copyWith(
                  fontSize: 24,
                  fontWeight: FontWeight.w600,
                ),
              ),
              Semantics(
                liveRegion: true,
                child: Text(
                  status,
                  key: const ValueKey('converse-interaction-status'),
                  style: theme.textTheme.bodyMedium?.copyWith(
                    color: theme.colorScheme.onSurfaceVariant,
                  ),
                ),
              ),
            ],
          ),
          const SizedBox(height: 12),
          AspectRatio(
            key: const ValueKey('converse-character-canvas'),
            aspectRatio: 832 / 446,
            child: ClipRRect(
              borderRadius: BorderRadius.circular(10),
              child: ColoredBox(
                color: theme.colorScheme.primaryContainer,
                child: SizedBox.expand(child: character),
              ),
            ),
          ),
          SizedBox(
            height: wide ? 130 : 64,
            child: activeVoice
                ? Center(child: _waveform(context, wide: wide))
                : null,
          ),
          _controls(context),
        ],
      ),
    );
  }

  Widget _waveform(BuildContext context, {required bool wide}) {
    const heights = <double>[8, 14, 24, 38, 56, 30, 68, 46, 28, 52, 34, 16, 10];
    return ExcludeSemantics(
      child: SizedBox(
        key: const ValueKey('converse-active-waveform'),
        width: 570,
        height: wide ? 72 : 44,
        child: Row(
          mainAxisAlignment: MainAxisAlignment.spaceEvenly,
          children: [
            for (final height in heights)
              Container(
                width: wide ? 7 : 5,
                height: height * (wide ? 1 : .55),
                decoration: BoxDecoration(
                  color: Theme.of(context).colorScheme.secondary,
                  borderRadius: BorderRadius.circular(3),
                ),
              ),
          ],
        ),
      ),
    );
  }

  Widget _controls(BuildContext context) => LayoutBuilder(
    builder: (context, constraints) {
      final scale = MediaQuery.textScalerOf(context).scale(16) / 16;
      final width = constraints.maxWidth;
      final inline = width >= 720 * scale;
      final paired = width >= 400 * scale;
      final available = inline
          ? width - 42
          : paired
          ? (width - 12) / 2
          : width;
      final weights = <double>[140 / 790, 140 / 790, 280 / 790, 230 / 790];
      final actions = [
        _control(
          context,
          key: const ValueKey('converse-microphone'),
          label: microphoneLabel,
          icon: Icons.mic_none,
          onPressed: onMicrophone,
        ),
        Tooltip(
          message: _cameraHint,
          child: _control(
            context,
            key: const ValueKey('converse-camera'),
            label: 'Camera',
            icon: Icons.videocam_outlined,
            onPressed: null,
          ),
        ),
        _control(
          context,
          key: const ValueKey('converse-call'),
          label: callLabel,
          icon: Icons.call_outlined,
          onPressed: onCall,
          primary: true,
        ),
        _control(
          context,
          key: const ValueKey('converse-model-settings'),
          label: 'Model settings',
          icon: Icons.settings_outlined,
          onPressed: onModelSettings,
        ),
      ];
      return Wrap(
        spacing: inline ? 14 : 12,
        runSpacing: 12,
        children: [
          for (var index = 0; index < actions.length; index++)
            SizedBox(
              width: inline ? available * weights[index] : available,
              child: actions[index],
            ),
        ],
      );
    },
  );

  Widget _control(
    BuildContext context, {
    required Key key,
    required String label,
    required IconData icon,
    required VoidCallback? onPressed,
    bool primary = false,
  }) {
    final colors = Theme.of(context).colorScheme;
    final style = ButtonStyle(
      minimumSize: const WidgetStatePropertyAll(Size(0, 64)),
      padding: const WidgetStatePropertyAll(
        EdgeInsets.symmetric(horizontal: 12, vertical: 16),
      ),
      shape: WidgetStatePropertyAll(
        RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
      ),
    );
    return primary
        ? FilledButton.icon(
            key: key,
            style: style,
            onPressed: onPressed,
            icon: Icon(icon, size: 20),
            label: Text(label, textAlign: TextAlign.center),
          )
        : OutlinedButton.icon(
            key: key,
            style: style.copyWith(
              backgroundColor: WidgetStatePropertyAll(colors.surfaceContainer),
              foregroundColor: WidgetStateProperty.resolveWith(
                (states) => states.contains(WidgetState.disabled)
                    ? colors.onSurface.withValues(alpha: .38)
                    : colors.onSurface,
              ),
              side: WidgetStatePropertyAll(
                BorderSide(color: colors.outlineVariant),
              ),
            ),
            onPressed: onPressed,
            icon: Icon(icon, size: 20),
            label: Text(label, textAlign: TextAlign.center),
          );
  }

  Widget _conversation(BuildContext context, {required bool wide}) {
    final theme = Theme.of(context);
    return DesignPanel(
      key: const ValueKey('converse-conversation-panel'),
      padding: EdgeInsets.all(wide ? 24 : 16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text(
            'Conversation',
            style: theme.textTheme.titleLarge?.copyWith(
              fontSize: 24,
              fontWeight: FontWeight.w600,
            ),
          ),
          const SizedBox(height: 16),
          // The transcript owner supplies its scrollable. The separate composer
          // remains mounted below it and never scrolls with transcript messages.
          SizedBox(
            key: const ValueKey('converse-transcript-viewport'),
            height: wide ? 278 : 260,
            child: transcript,
          ),
          const SizedBox(height: 16),
          composer,
        ],
      ),
    );
  }
}
