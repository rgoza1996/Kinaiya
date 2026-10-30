import 'package:flutter/material.dart';

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
