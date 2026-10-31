import 'package:flutter/material.dart';

/// Existing preference owners can render in Options or their original dialog.
/// Embedding changes only the surrounding surface, never the owner's lifetime
/// or validation rules.
class SettingsSurface extends StatelessWidget {
  const SettingsSurface({
    super.key,
    required this.child,
    required this.embedded,
    this.insetPadding = const EdgeInsets.all(12),
  });
  final Widget child;
  final bool embedded;
  final EdgeInsets insetPadding;

  @override
  Widget build(BuildContext context) => embedded
      ? Align(alignment: Alignment.topLeft, child: child)
      : Dialog(insetPadding: insetPadding, child: child);
}
