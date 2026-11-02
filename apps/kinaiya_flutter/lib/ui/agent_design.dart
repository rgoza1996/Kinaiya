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
  static const double detailsMinWidth = 220;
  static const double sidebarMinWidth = 224;
  static const double sidebarCollapsedWidth = 48;
  static const double sidebarCollapseThreshold = 96;
  static const double sidebarExpandThreshold = 128;
  static const double conversationMinWidth = 440;
  static const double sidebarResizeWidth = 24;
  static const double wideBreakpoint = 1080;
  static const double detailsBreakpoint = 1360;
  static const double composerBreakpoint = 620;
}

/// Semantic state variant for an [AgentTask] based strictly on owner-confirmed
/// state and grounding evidence.
///
/// Ungrounded `succeeded` and unknown states never receive [SemanticStateVariant.completed].
SemanticStateVariant agentSemanticStateVariant(AgentTask task) {
  if (task.isGroundedSuccess) return SemanticStateVariant.completed;
  if (task.state == 'failed' || task.state == 'blocked') {
    return SemanticStateVariant.failedOrBlocked;
  }
  if (task.state == 'awaiting_approval' || task.state == 'succeeded') {
    return SemanticStateVariant.needsAttention;
  }
  if (task.state == 'running' || task.state == 'verifying') {
    return SemanticStateVariant.active;
  }
  if (task.state == 'queued' || task.state == 'accepted') {
    return SemanticStateVariant.pending;
  }
  return SemanticStateVariant.unavailableOrUnknown;
}

/// Theme-aware tone for the actual task state.
///
/// Grounded success uses the theme primary, failure uses the theme error,
/// cancellation uses the theme outline, and every other real state
/// (running, accepted, awaiting approval, ungrounded success) uses theme
/// secondary/tertiary accents. The chip label itself stays on the theme
/// surface ink; the tone only tints the chip fill and border.
Color agentStatusTone(BuildContext context, AgentTask task) =>
    KinaiyaDesign.semanticState(context, agentSemanticStateVariant(task)).tone;

/// Actual-state chip for a real task; pairs the literal task state with its
/// non-color semantic meaning and icon.
Widget agentStatusChip(BuildContext context, AgentTask task) {
  final variant = agentSemanticStateVariant(task);
  return StatusChip(
    text: task.state,
    tone: KinaiyaDesign.semanticState(context, variant).tone,
    variant: variant,
  );
}

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
BoxDecoration agentWorkspaceDecoration(
  BuildContext context, {
  bool showBorder = true,
}) {
  final scheme = Theme.of(context).colorScheme;
  return BoxDecoration(
    color: scheme.surfaceContainerLow,
    border: showBorder
        ? Border(right: BorderSide(color: scheme.outlineVariant))
        : null,
  );
}

/// Right details panel: theme surface with a single outline border.
BoxDecoration agentDetailsDecoration(
  BuildContext context, {
  bool showBorder = true,
}) {
  final scheme = Theme.of(context).colorScheme;
  return BoxDecoration(
    color: scheme.surfaceContainerLow,
    border: showBorder
        ? Border(left: BorderSide(color: scheme.outlineVariant))
        : null,
  );
}

/// Composer surface: a complete outline keeps its edges visible in wide chat.
BoxDecoration agentComposerDecoration(BuildContext context) {
  final scheme = Theme.of(context).colorScheme;
  return BoxDecoration(
    color: scheme.surfaceContainerLow,
    border: Border.all(color: scheme.outlineVariant),
    borderRadius: BorderRadius.circular(KinaiyaDesign.radius),
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
