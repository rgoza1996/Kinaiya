import 'package:flutter/material.dart';

/// Fades presentation only. Runtime/capture owners belong outside this widget.
class UiFadeTransition extends StatelessWidget {
  const UiFadeTransition({
    super.key,
    required this.enabled,
    required this.transitionKey,
    required this.child,
    this.alignment = Alignment.center,
  });

  final bool enabled;
  final Object transitionKey;
  final Widget child;
  final AlignmentGeometry alignment;

  @override
  Widget build(BuildContext context) {
    final animate = enabled && !MediaQuery.disableAnimationsOf(context);
    return AnimatedSwitcher(
      duration: animate ? const Duration(milliseconds: 220) : Duration.zero,
      switchInCurve: Curves.easeInOut,
      switchOutCurve: Curves.easeInOut,
      transitionBuilder: (child, animation) => FadeTransition(
        opacity: animate ? animation : const AlwaysStoppedAnimation(1),
        child: child,
      ),
      layoutBuilder: (current, previous) => Stack(
        alignment: alignment,
        children: [
          for (final outgoing in animate ? previous : <Widget>[])
            ExcludeFocus(
              child: ExcludeSemantics(child: IgnorePointer(child: outgoing)),
            ),
          if (current != null) current,
        ],
      ),
      child: KeyedSubtree(key: ValueKey(transitionKey), child: child),
    );
  }
}
