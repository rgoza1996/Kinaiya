import 'package:flutter/material.dart';
import 'anchored_moonlit_scene.dart';

/// Owner-interface visual roles. These values never carry runtime authority.
abstract final class KinaiyaDesign {
  static const background = Color(0xFF061220);
  static const panel = Color(0xFF0B1B2E);
  static const raised = Color(0xFF102742);
  static const stroke = Color(0xFF213C5A);
  static const text = Color(0xFFF3F6FF);
  static const muted = Color(0xFFA4B9D8);
  static const blue = Color(0xFF075BFF);
  static const cyan = Color(0xFF53AEFF);
  static const green = Color(0xFF12D58A);
  static const rail = Color(0xFF0C2250);
  static const radius = 12.0;
  static const fontFamily = 'Inter';
  static const moonlitBackground =
      'assets/design/kinaiya-moonlit-background-v1.png';
  static const moonlitDesktop = MoonlitArtwork(
    asset: 'assets/design/kinaiya-moonlit-desktop-v2.png',
    sourceSize: Size(1536, 1024),
    surfaceAnchor: Offset(.5, 676 / 1024),
  );
  static const moonlitPhone = MoonlitArtwork(
    asset: 'assets/design/kinaiya-moonlit-phone-v2.png',
    sourceSize: Size(1024, 1536),
    surfaceAnchor: Offset(.5, 950 / 1536),
  );

  /// Bounded text hierarchy roles for owner configuration and inspection pages.
  static OwnerTextRoles textRoles(BuildContext context) =>
      OwnerTextRoles.of(context);

  /// Resolves a contrast-safe semantic state style with non-color meaning.
  static SemanticStateStyle semanticState(
    BuildContext context,
    SemanticStateVariant variant,
  ) => SemanticStateStyle.of(context, variant);
}

/// Explicit semantic state variants with non-color textual meaning and icons.
///
/// Unknown or uncertain states always map to [unavailableOrUnknown] and never
/// receive [completed] or [active] styling.
enum SemanticStateVariant {
  active('Active', Icons.play_circle_outline_rounded),
  pending('Pending', Icons.schedule_rounded),
  needsAttention('Needs attention', Icons.warning_amber_rounded),
  failedOrBlocked('Failed or blocked', Icons.error_outline_rounded),
  completed('Completed', Icons.check_circle_outline_rounded),
  unavailableOrUnknown('Unavailable or unknown', Icons.help_outline_rounded);

  const SemanticStateVariant(this.meaning, this.icon);
  final String meaning;
  final IconData icon;
}

/// Theme-aware visual and accessible contract for a [SemanticStateVariant].
class SemanticStateStyle {
  const SemanticStateStyle({
    required this.variant,
    required this.tone,
    required this.background,
    required this.foreground,
    required this.border,
    required this.icon,
    required this.meaning,
  });

  factory SemanticStateStyle.of(
    BuildContext context,
    SemanticStateVariant variant,
  ) {
    final scheme = Theme.of(context).colorScheme;
    final tone = switch (variant) {
      SemanticStateVariant.completed => scheme.primary,
      SemanticStateVariant.failedOrBlocked => scheme.error,
      SemanticStateVariant.needsAttention => scheme.tertiary,
      SemanticStateVariant.active ||
      SemanticStateVariant.pending => scheme.secondary,
      SemanticStateVariant.unavailableOrUnknown => scheme.outline,
    };
    final background = tone.withValues(alpha: 0.15);
    final foreground = scheme.onSurface;
    return SemanticStateStyle(
      variant: variant,
      tone: tone,
      background: background,
      foreground: foreground,
      border: tone,
      icon: variant.icon,
      meaning: variant.meaning,
    );
  }

  final SemanticStateVariant variant;
  final Color tone;
  final Color background;
  final Color foreground;
  final Color border;
  final IconData icon;
  final String meaning;

  String accessibleLabel(String rawLabel) => 'Status: $rawLabel · $meaning';
}

/// Bounded text hierarchy roles for owner configuration and inspection screens.
///
/// Each role inherits colors and baseline metrics from the active [ThemeData]
/// (`colorScheme.onSurface` and `colorScheme.onSurfaceVariant`) so light, dark,
/// and custom character palettes remain contrast-safe and scale with the
/// system text scaler without hard-coded tiny text.
///
/// Roles:
/// - [pageTitle]: Primary heading of an owner page or dialog (`22px`, `w700`).
/// - [sectionTitle]: Group or card heading (`16px`, `w600`).
/// - [primaryContent]: Actionable owner guidance and primary labels (`15px`, `w500`).
/// - [supportingText]: Secondary explanations and context (`14px`, `w400`).
/// - [technicalMetadata]: Revisions, endpoints, transport IDs, and hashes (`13px`, `w500`, monospace).
class OwnerTextRoles {
  const OwnerTextRoles._({
    required this.pageTitle,
    required this.sectionTitle,
    required this.primaryContent,
    required this.supportingText,
    required this.technicalMetadata,
  });

  factory OwnerTextRoles.of(BuildContext context) {
    final theme = Theme.of(context);
    final scheme = theme.colorScheme;
    final text = theme.textTheme;
    final primaryInk = scheme.onSurface;
    final supportingInk = scheme.onSurfaceVariant;
    return OwnerTextRoles._(
      pageTitle: (text.titleLarge ?? const TextStyle()).copyWith(
        fontSize: 22,
        fontWeight: FontWeight.w700,
        height: 1.25,
        color: primaryInk,
      ),
      sectionTitle: (text.titleMedium ?? const TextStyle()).copyWith(
        fontSize: 16,
        fontWeight: FontWeight.w600,
        height: 1.3,
        color: primaryInk,
      ),
      primaryContent: (text.bodyLarge ?? const TextStyle()).copyWith(
        fontSize: 15,
        fontWeight: FontWeight.w500,
        height: 1.4,
        color: primaryInk,
      ),
      supportingText: (text.bodyMedium ?? const TextStyle()).copyWith(
        fontSize: 14,
        fontWeight: FontWeight.w400,
        height: 1.4,
        color: supportingInk,
      ),
      technicalMetadata: (text.bodySmall ?? const TextStyle()).copyWith(
        fontSize: 13,
        fontWeight: FontWeight.w500,
        height: 1.35,
        fontFamily: 'monospace',
        color: supportingInk,
      ),
    );
  }

  final TextStyle pageTitle;
  final TextStyle sectionTitle;
  final TextStyle primaryContent;
  final TextStyle supportingText;
  final TextStyle technicalMetadata;
}

/// Reusable surface; palette overrides still come from the active theme.
class DesignPanel extends StatelessWidget {
  const DesignPanel({
    super.key,
    required this.child,
    this.padding = const EdgeInsets.all(24),
  });
  final Widget child;
  final EdgeInsetsGeometry padding;

  @override
  Widget build(BuildContext context) => Container(
    padding: padding,
    decoration: BoxDecoration(
      color: Theme.of(context).colorScheme.surfaceContainerLow,
      border: Border.all(color: Theme.of(context).colorScheme.outlineVariant),
      borderRadius: BorderRadius.circular(KinaiyaDesign.radius),
    ),
    child: child,
  );
}

/// Frames a multi-pane workspace using the active presentation palette.
class WorkspaceFrame extends StatelessWidget {
  const WorkspaceFrame({super.key, required this.inset, required this.child});

  final bool inset;
  final Widget child;

  @override
  Widget build(BuildContext context) {
    final radius = BorderRadius.circular(inset ? KinaiyaDesign.radius : 0);
    return Padding(
      padding: EdgeInsets.all(inset ? 12 : 0),
      child: DecoratedBox(
        position: DecorationPosition.foreground,
        decoration: BoxDecoration(
          border: Border.all(
            color: Theme.of(context).colorScheme.outlineVariant,
          ),
          borderRadius: radius,
        ),
        child: ClipRRect(borderRadius: radius, child: child),
      ),
    );
  }
}
