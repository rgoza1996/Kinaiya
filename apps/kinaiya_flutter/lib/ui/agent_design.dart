import 'package:flutter/material.dart';

import '../api/models.dart';
import 'design_tokens.dart';
import 'widgets.dart';

/// Presentation-only Agent workspace styling.
///
/// These helpers carry no runtime authority: task states, evidence,
/// approvals and drafts come from [AgentTask] and the owning [AppState].
/// All fills, inks and borders resolve from the active [Theme] so explicit
/// user overrides and imported character palettes keep working. Corner
/// geometry follows [KinaiyaDesign.radius]; no UI images are baked in.
abstract final class AgentDesign {
  static const double workspaceWidth = 260;
  static const double detailsWidth = 300;
  static const double conversationMaxWidth = 1120;
  static const double wideBreakpoint = 1080;
  static const double detailsBreakpoint = 1360;
  static const double composerBreakpoint = 620;
}

/// Theme-aware tone for the actual task state.
///
/// Grounded success uses the theme primary, failure uses the theme error,
/// cancellation uses the theme outline, and every other real state
/// (running, accepted, awaiting approval, ungrounded success) uses theme
/// secondary/tertiary accents. The chip label itself stays on the theme
/// surface ink; the tone only tints the chip fill and border.
Color agentStatusTone(BuildContext context, AgentTask task) {
  final scheme = Theme.of(context).colorScheme;
  if (task.isGroundedSuccess) return scheme.primary;
  if (task.state == 'failed') return scheme.error;
  if (task.state == 'cancelled') return scheme.outline;
  if (task.state == 'awaiting_approval') return scheme.tertiary;
  return scheme.secondary;
}

/// Actual-state chip for a real task; the text is always the task state.
Widget agentStatusChip(BuildContext context, AgentTask task) =>
    StatusChip(text: task.state, tone: agentStatusTone(context, task));

/// Leading icon for a real task: terminal tasks show a completed mark,
/// active tasks show a pending mark. No project or harness iconography.
IconData agentTaskIcon(AgentTask task) =>
    task.isTerminal ? Icons.task_alt_rounded : Icons.pending_outlined;

/// Muted copy style derived from the active theme.
TextStyle? agentMutedStyle(BuildContext context, [TextStyle? base]) {
  final theme = Theme.of(context);
  final scheme = theme.colorScheme;
  final resolved = base ?? theme.textTheme.bodyMedium;
  return resolved?.copyWith(color: scheme.onSurfaceVariant);
}

/// Left workspace panel: theme surface with a single outline border.
BoxDecoration agentWorkspaceDecoration(BuildContext context) {
  final scheme = Theme.of(context).colorScheme;
  return BoxDecoration(
    color: scheme.surfaceContainerLow,
    border: Border(right: BorderSide(color: scheme.outlineVariant)),
  );
}

/// Right details panel: theme surface with a single outline border.
BoxDecoration agentDetailsDecoration(BuildContext context) {
  final scheme = Theme.of(context).colorScheme;
  return BoxDecoration(
    color: scheme.surfaceContainerLow,
    border: Border(left: BorderSide(color: scheme.outlineVariant)),
  );
}

/// Composer strip: theme surface with a top outline border.
BoxDecoration agentComposerDecoration(BuildContext context) {
  final scheme = Theme.of(context).colorScheme;
  return BoxDecoration(
    color: scheme.surfaceContainerLow,
    border: Border(top: BorderSide(color: scheme.outlineVariant)),
  );
}

/// 12px card shape with a theme outline border for task/approval cards.
RoundedRectangleBorder agentCardShape(BuildContext context) {
  final scheme = Theme.of(context).colorScheme;
  return RoundedRectangleBorder(
    borderRadius: BorderRadius.circular(KinaiyaDesign.radius),
    side: BorderSide(color: scheme.outlineVariant),
  );
}

/// Rounded 12px tile shape for workspace selection emphasis.
RoundedRectangleBorder agentTileShape() => RoundedRectangleBorder(
  borderRadius: BorderRadius.circular(KinaiyaDesign.radius),
);
