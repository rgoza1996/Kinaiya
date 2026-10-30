import 'package:flutter/material.dart';

/// Presentation of the existing two explicit microphone gestures. All capture,
/// readiness, stop and epoch decisions remain in CharacterView.
class ConverseVoiceActions extends StatelessWidget {
  const ConverseVoiceActions({
    super.key,
    required this.callLabel,
    required this.tellLabel,
    required this.callTooltip,
    required this.tellTooltip,
    required this.callIcon,
    required this.tellIcon,
    this.onCall,
    this.onTell,
  });

  final String callLabel, tellLabel, callTooltip, tellTooltip;
  final IconData callIcon, tellIcon;
  final VoidCallback? onCall, onTell;

  @override
  Widget build(BuildContext context) {
    final colors = Theme.of(context).colorScheme;
    return Container(
      key: const ValueKey('converse-voice-group'),
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 16),
      decoration: BoxDecoration(
        color: colors.primaryContainer.withValues(alpha: .72),
        borderRadius: BorderRadius.circular(24),
        border: Border.all(color: colors.outlineVariant),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Expanded(
            child: _action(
              context,
              filled: true,
              label: callLabel,
              tooltip: callTooltip,
              icon: callIcon,
              onPressed: onCall,
              key: const ValueKey('call-kinaiya'),
              description: 'Have a natural conversation',
            ),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: _action(
              context,
              filled: false,
              label: tellLabel,
              tooltip: tellTooltip,
              icon: tellIcon,
              onPressed: onTell,
              key: const ValueKey('tell-kinaiya'),
              description: 'Send a voice message',
            ),
          ),
        ],
      ),
    );
  }

  Widget _action(
    BuildContext context, {
    required Key key,
    required bool filled,
    required String label,
    required String tooltip,
    required IconData icon,
    required String description,
    required VoidCallback? onPressed,
  }) {
    final colors = Theme.of(context).colorScheme;
    final style = ButtonStyle(
      minimumSize: const WidgetStatePropertyAll(Size.square(80)),
      maximumSize: const WidgetStatePropertyAll(Size.square(80)),
      padding: const WidgetStatePropertyAll(EdgeInsets.zero),
      shape: const WidgetStatePropertyAll(CircleBorder()),
    );
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        Tooltip(
          message: tooltip,
          child: Semantics(
            label: label,
            child: filled
                ? FilledButton(
                    key: key,
                    style: style,
                    onPressed: onPressed,
                    child: Icon(icon, size: 32),
                  )
                : OutlinedButton(
                    key: key,
                    style: style.copyWith(
                      backgroundColor: WidgetStatePropertyAll(colors.surface),
                    ),
                    onPressed: onPressed,
                    child: Icon(icon, size: 32),
                  ),
          ),
        ),
        const SizedBox(height: 10),
        Text(
          label,
          textAlign: TextAlign.center,
          style: Theme.of(context).textTheme.titleSmall?.copyWith(
            color: colors.onPrimaryContainer,
            fontWeight: FontWeight.w700,
          ),
        ),
        const SizedBox(height: 4),
        Text(
          description,
          textAlign: TextAlign.center,
          style: Theme.of(
            context,
          ).textTheme.bodySmall?.copyWith(color: colors.onPrimaryContainer),
        ),
      ],
    );
  }
}
