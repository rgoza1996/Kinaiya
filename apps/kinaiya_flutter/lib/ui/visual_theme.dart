import 'package:flutter/material.dart';

import 'design_tokens.dart';

// Kinaiya brand ink. Presentation only; never gates speech, runtime, or evidence.
const Color kinaiyaIndigo = Color(0xFF141091);
const Color kinaiyaWhite = Color(0xFFFFFFFF);
const Color kinaiyaNavy = Color(0xFF07172D);
const Color kinaiyaMist = Color(0xFFF0F3FF);

// Global Material3 theme for light and dark surfaces.
// Light primary is exactly kinaiyaIndigo with white onPrimary.
// Dark keeps indigo/white identity with light ink on navy surfaces.
// No fixed widths and no tiny text are hardcoded here.
ThemeData kinaiyaTheme({
  Brightness brightness = Brightness.light,
  Color? primary,
  Color? background,
  bool designSystem = false,
}) {
  final bool isDark = brightness == Brightness.dark;

  final ColorScheme legacy = isDark
      ? const ColorScheme.dark(
          primary: kinaiyaWhite,
          onPrimary: kinaiyaIndigo,
          primaryContainer: kinaiyaIndigo,
          onPrimaryContainer: kinaiyaWhite,
          secondary: Color(0xFFC2C8F5),
          onSecondary: kinaiyaNavy,
          secondaryContainer: Color(0xFF26236E),
          onSecondaryContainer: kinaiyaMist,
          surface: kinaiyaNavy,
          onSurface: kinaiyaWhite,
          error: Color(0xFFFFB4AB),
          onError: Color(0xFF690005),
          outline: Color(0xFF8E99C4),
          outlineVariant: Color(0xFF2A3A5E),
          onSurfaceVariant: Color(0xFFBDC8E1),
          surfaceContainerLowest: Color(0xFF041020),
          surfaceContainerLow: Color(0xFF0A1C33),
          surfaceContainer: Color(0xFF0B2039),
          surfaceContainerHigh: Color(0xFF102B48),
          tertiaryContainer: Color(0xFF3A311C),
          onTertiaryContainer: Color(0xFFFFEAC2),
        )
      : const ColorScheme.light(
          primary: kinaiyaIndigo,
          onPrimary: kinaiyaWhite,
          primaryContainer: kinaiyaMist,
          onPrimaryContainer: kinaiyaIndigo,
          secondary: Color(0xFF3D3FC4),
          onSecondary: kinaiyaWhite,
          secondaryContainer: kinaiyaMist,
          onSecondaryContainer: kinaiyaNavy,
          surface: kinaiyaWhite,
          onSurface: kinaiyaNavy,
          error: Color(0xFFB3261E),
          onError: kinaiyaWhite,
          outline: Color(0xFF7C86B3),
          outlineVariant: Color(0xFFD4DBF5),
          onSurfaceVariant: Color(0xFF465675),
          surfaceContainerLowest: kinaiyaWhite,
          surfaceContainerLow: Color(0xFFF7F8FF),
          surfaceContainer: kinaiyaMist,
          surfaceContainerHigh: Color(0xFFE7ECFC),
          tertiaryContainer: Color(0xFFFFF2CC),
          onTertiaryContainer: kinaiyaNavy,
        );

  final defaults = designSystem
      ? (isDark
            ? legacy.copyWith(
                primary: KinaiyaDesign.blue,
                onPrimary: kinaiyaWhite,
                primaryContainer: const Color(0xFF123B7D),
                onPrimaryContainer: KinaiyaDesign.text,
                secondary: KinaiyaDesign.cyan,
                surface: KinaiyaDesign.background,
                onSurface: KinaiyaDesign.text,
                onSurfaceVariant: KinaiyaDesign.muted,
                outlineVariant: KinaiyaDesign.stroke,
                surfaceContainerLowest: KinaiyaDesign.background,
                surfaceContainerLow: KinaiyaDesign.panel,
                surfaceContainer: KinaiyaDesign.raised,
                surfaceContainerHigh: const Color(0xFF16324F),
                surfaceContainerHighest: const Color(0xFF1D3D60),
              )
            : legacy.copyWith(
                primary: KinaiyaDesign.blue,
                onPrimary: kinaiyaWhite,
                primaryContainer: const Color(0xFFE9F0FF),
                onPrimaryContainer: const Color(0xFF0B285F),
                secondary: const Color(0xFF0750D7),
                surface: kinaiyaWhite,
                onSurface: KinaiyaDesign.background,
                outlineVariant: const Color(0xFFCBD9F0),
                surfaceContainerLow: const Color(0xFFF6F8FF),
                surfaceContainer: const Color(0xFFE9F0FF),
                surfaceContainerHigh: const Color(0xFFDFE9FC),
              ))
      : legacy;

  // Explicit palette choices keep their fills and receive readable foregrounds.
  final custom = designSystem
      ? primary != null || background != null
      : (primary != null && primary != kinaiyaIndigo) ||
            (background != null && background != kinaiyaWhite);
  final scheme = custom
      ? _customScheme(
          defaults,
          primary ?? defaults.primary,
          background ?? defaults.surface,
        )
      : defaults;
  final actionInk = custom || designSystem
      ? _readableActionInk(scheme)
      : scheme.primary;

  final TextTheme baseText = isDark
      ? ThemeData.dark().textTheme
      : ThemeData.light().textTheme;
  final TextTheme textTheme = baseText.apply(
    fontFamily: designSystem ? KinaiyaDesign.fontFamily : null,
    bodyColor: scheme.onSurface,
    displayColor: scheme.onSurface,
  );

  const Size minTap = Size(48, 48);
  final RoundedRectangleBorder buttonShape = RoundedRectangleBorder(
    borderRadius: BorderRadius.circular(12),
  );
  final RoundedRectangleBorder cardShape = RoundedRectangleBorder(
    borderRadius: BorderRadius.circular(designSystem ? 12 : 16),
    side: BorderSide(color: scheme.outlineVariant),
  );

  final Color inputFill = custom
      ? scheme.surfaceContainerHigh
      : isDark
      ? kinaiyaWhite.withValues(alpha: 0.06)
      : kinaiyaMist.withValues(alpha: 0.55);
  final OutlineInputBorder inputBorder = OutlineInputBorder(
    borderRadius: BorderRadius.circular(12),
    borderSide: BorderSide(color: scheme.outlineVariant),
  );

  return ThemeData(
    useMaterial3: true,
    fontFamily: designSystem ? KinaiyaDesign.fontFamily : null,
    brightness: brightness,
    colorScheme: scheme,
    scaffoldBackgroundColor: scheme.surface,
    canvasColor: scheme.surface,
    focusColor: actionInk.withValues(alpha: 0.14),
    materialTapTargetSize: MaterialTapTargetSize.padded,
    visualDensity: VisualDensity.standard,
    textTheme: textTheme,
    textSelectionTheme: custom
        ? TextSelectionThemeData(
            cursorColor: actionInk,
            selectionColor: actionInk.withValues(alpha: 0.24),
            selectionHandleColor: actionInk,
          )
        : null,
    appBarTheme: AppBarTheme(
      backgroundColor: scheme.surface,
      foregroundColor: scheme.onSurface,
      elevation: 0,
      centerTitle: false,
    ),
    cardTheme: CardThemeData(
      color: scheme.surface,
      surfaceTintColor: Colors.transparent,
      elevation: 0,
      shape: cardShape,
    ),
    dialogTheme: DialogThemeData(
      backgroundColor: scheme.surface,
      surfaceTintColor: Colors.transparent,
      elevation: 0,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(designSystem ? 12 : 20),
        side: BorderSide(color: scheme.outlineVariant),
      ),
    ),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: inputFill,
      contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 16),
      hintStyle: textTheme.bodyLarge?.copyWith(
        color: custom
            ? scheme.onSurface
            : scheme.onSurface.withValues(alpha: 0.62),
      ),
      labelStyle: textTheme.bodyLarge?.copyWith(color: scheme.onSurface),
      border: inputBorder,
      enabledBorder: inputBorder,
      focusedBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(12),
        borderSide: BorderSide(color: actionInk, width: 2),
      ),
      errorBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(12),
        borderSide: BorderSide(color: scheme.error),
      ),
      focusedErrorBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(12),
        borderSide: BorderSide(color: scheme.error, width: 2),
      ),
    ),
    elevatedButtonTheme: ElevatedButtonThemeData(
      style: ElevatedButton.styleFrom(
        foregroundColor: custom || designSystem ? actionInk : null,
        minimumSize: minTap,
        shape: buttonShape,
        textStyle: const TextStyle(fontSize: 16, fontWeight: FontWeight.w600),
        disabledBackgroundColor: scheme.onSurface.withValues(alpha: 0.12),
        disabledForegroundColor: scheme.onSurface.withValues(alpha: 0.55),
      ),
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: kinaiyaPrimaryButtonStyle(scheme, actionInk: actionInk),
    ),
    outlinedButtonTheme: OutlinedButtonThemeData(
      style: OutlinedButton.styleFrom(
        foregroundColor: actionInk,
        minimumSize: minTap,
        shape: buttonShape,
        textStyle: const TextStyle(fontSize: 16, fontWeight: FontWeight.w600),
        disabledForegroundColor: scheme.onSurface.withValues(alpha: 0.55),
      ),
    ),
    textButtonTheme: TextButtonThemeData(
      style: TextButton.styleFrom(
        foregroundColor: actionInk,
        minimumSize: minTap,
        shape: buttonShape,
        textStyle: const TextStyle(fontSize: 16, fontWeight: FontWeight.w600),
        disabledForegroundColor: scheme.onSurface.withValues(alpha: 0.55),
      ),
    ),
    iconButtonTheme: IconButtonThemeData(
      style: ButtonStyle(
        foregroundColor: custom || designSystem
            ? WidgetStatePropertyAll(actionInk)
            : null,
        minimumSize: const WidgetStatePropertyAll<Size>(minTap),
        shape: WidgetStatePropertyAll<OutlinedBorder>(buttonShape),
      ),
    ),
    dividerTheme: DividerThemeData(color: scheme.outlineVariant, thickness: 1),
    snackBarTheme: SnackBarThemeData(
      backgroundColor: custom
          ? scheme.onSurface
          : isDark
          ? kinaiyaWhite
          : kinaiyaNavy,
      contentTextStyle: textTheme.bodyLarge?.copyWith(
        color: custom
            ? scheme.surface
            : isDark
            ? kinaiyaNavy
            : kinaiyaWhite,
      ),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
    ),
    chipTheme: ChipThemeData(
      backgroundColor: scheme.surface,
      disabledColor: scheme.onSurface.withValues(alpha: 0.12),
      labelStyle: textTheme.bodyLarge,
      side: BorderSide(color: scheme.outlineVariant),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
    ),
  );
}

// A selected color remains the actual fill. Foregrounds and subtle surfaces are
// derived independently, so even near-white or near-black user colors work.
ColorScheme _customScheme(ColorScheme base, Color primary, Color surface) {
  primary = primary.withValues(alpha: 1);
  surface = surface.withValues(alpha: 1);
  final ink = _contrastingInk(surface);
  Color raised(double amount) {
    final mixed = Color.lerp(surface, ink, amount)!;
    if (_contrast(mixed, ink) >= 4.5) return mixed;
    return Color.lerp(
      surface,
      ink == Colors.black ? Colors.white : Colors.black,
      amount,
    )!;
  }

  final container = Color.lerp(surface, primary, 0.16)!;
  final secondary = _readableAccent(primary, surface);
  final error = _readableAccent(base.error, surface);
  return base.copyWith(
    primary: primary,
    onPrimary: _contrastingInk(primary),
    primaryContainer: container,
    onPrimaryContainer: _contrastingInk(container),
    secondary: secondary,
    onSecondary: _contrastingInk(secondary),
    secondaryContainer: container,
    onSecondaryContainer: _contrastingInk(container),
    tertiary: secondary,
    onTertiary: _contrastingInk(secondary),
    tertiaryContainer: raised(0.12),
    onTertiaryContainer: _contrastingInk(raised(0.12)),
    surface: surface,
    onSurface: ink,
    onSurfaceVariant: ink,
    surfaceContainerLowest: surface,
    surfaceContainerLow: raised(0.04),
    surfaceContainer: raised(0.07),
    surfaceContainerHigh: raised(0.10),
    surfaceContainerHighest: raised(0.13),
    surfaceDim: raised(0.08),
    surfaceBright: surface,
    outline: _readableAccent(raised(0.45), surface),
    outlineVariant: raised(0.22),
    error: error,
    onError: _contrastingInk(error),
    errorContainer: Color.lerp(surface, error, 0.14),
    onErrorContainer: _contrastingInk(Color.lerp(surface, error, 0.14)!),
    inverseSurface: ink,
    onInverseSurface: surface,
    inversePrimary: _readableAccent(primary, ink),
    surfaceTint: primary,
  );
}

double _contrast(Color a, Color b) {
  final x = a.computeLuminance(), y = b.computeLuminance();
  return (x > y ? x + 0.05 : y + 0.05) / (x > y ? y + 0.05 : x + 0.05);
}

Color _contrastingInk(Color fill) =>
    _contrast(Colors.black, fill) >= _contrast(Colors.white, fill)
    ? Colors.black
    : Colors.white;

Color _readableAccent(Color accent, Color surface) {
  if (_contrast(accent, surface) >= 4.5) return accent;
  final ink = _contrastingInk(surface);
  for (var step = 1; step <= 20; step++) {
    final adjusted = Color.lerp(accent, ink, step / 20)!;
    if (_contrast(adjusted, surface) >= 4.5) return adjusted;
  }
  return ink;
}

Color _readableActionInk(ColorScheme scheme) {
  final surfaces = [
    scheme.surface,
    scheme.surfaceContainerLow,
    scheme.surfaceContainer,
    scheme.surfaceContainerHigh,
    scheme.surfaceContainerHighest,
  ];
  for (var step = 0; step <= 20; step++) {
    final adjusted = Color.lerp(scheme.primary, scheme.onSurface, step / 20)!;
    if (surfaces.every((surface) => _contrast(adjusted, surface) >= 4.5)) {
      return adjusted;
    }
  }
  return scheme.onSurface;
}

/// Reusable primary action [ButtonStyle] with unified keyboard focus ring,
/// hover/pressed overlay states, unambiguous disabled styling, and a minimum
/// 48x48 interactive touch target.
///
/// Pattern documentation:
/// - **Keyboard focus (`WidgetState.focused`)**: Renders a 2px high-contrast
///   border (`onSurface` or `actionInk`) plus a subtle `onPrimary` focus
///   overlay so keyboard position is unmistakable on light, dark, and custom
///   user palettes.
/// - **Hover (`WidgetState.hovered`) & Pressed (`WidgetState.pressed`)**:
///   Applies distinct `onPrimary` overlays and border emphasis without
///   relying on hover-only guidance.
/// - **Disabled (`WidgetState.disabled`)**: Uses muted `onSurface` fill (`12%`)
///   and foreground (`55%`) with transparent overlay.
ButtonStyle kinaiyaPrimaryButtonStyle(ColorScheme scheme, {Color? actionInk}) {
  final focusInk = actionInk ?? _readableActionInk(scheme);
  final focusRing = _contrast(focusInk, scheme.primary) >= 3.0
      ? focusInk
      : scheme.onPrimary;
  return ButtonStyle(
    minimumSize: const WidgetStatePropertyAll<Size>(Size(48, 48)),
    padding: const WidgetStatePropertyAll<EdgeInsetsGeometry>(
      EdgeInsets.symmetric(horizontal: 20, vertical: 12),
    ),
    textStyle: const WidgetStatePropertyAll<TextStyle>(
      TextStyle(fontSize: 16, fontWeight: FontWeight.w600),
    ),
    shape: WidgetStatePropertyAll<OutlinedBorder>(
      RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
    ),
    backgroundColor: WidgetStateProperty.resolveWith<Color?>((states) {
      if (states.contains(WidgetState.disabled)) {
        return scheme.onSurface.withValues(alpha: 0.12);
      }
      return scheme.primary;
    }),
    foregroundColor: WidgetStateProperty.resolveWith<Color?>((states) {
      if (states.contains(WidgetState.disabled)) {
        return scheme.onSurface.withValues(alpha: 0.55);
      }
      return scheme.onPrimary;
    }),
    overlayColor: WidgetStateProperty.resolveWith<Color?>((states) {
      if (states.contains(WidgetState.disabled)) {
        return Colors.transparent;
      }
      if (states.contains(WidgetState.pressed)) {
        return scheme.onPrimary.withValues(alpha: 0.24);
      }
      if (states.contains(WidgetState.focused)) {
        return scheme.onPrimary.withValues(alpha: 0.16);
      }
      if (states.contains(WidgetState.hovered)) {
        return scheme.onPrimary.withValues(alpha: 0.12);
      }
      return null;
    }),
    side: WidgetStateProperty.resolveWith<BorderSide?>((states) {
      if (states.contains(WidgetState.disabled)) {
        return BorderSide(color: scheme.outlineVariant.withValues(alpha: 0.45));
      }
      if (states.contains(WidgetState.focused)) {
        return BorderSide(color: focusRing, width: 2);
      }
      if (states.contains(WidgetState.pressed)) {
        return BorderSide(color: focusRing.withValues(alpha: 0.85), width: 1.5);
      }
      if (states.contains(WidgetState.hovered)) {
        return BorderSide(color: focusRing.withValues(alpha: 0.65), width: 1);
      }
      return BorderSide.none;
    }),
  );
}

/// Reusable sidebar navigation destination with unified keyboard focus, hover,
/// pressed, selected, and disabled feedback.
///
/// Preserves:
/// - Minimum 48x48 interactive touch target.
/// - Contrast-safe focus ring, hover fill, and pressed highlight across default
///   and user-configured palettes.
/// - Screen reader labels/states and single-dispatch keyboard activation
///   (`Enter` / `Space`).
class KinaiyaSidebarDestination extends StatefulWidget {
  const KinaiyaSidebarDestination({
    super.key,
    required this.title,
    this.subtitle,
    this.leading,
    this.selected = false,
    this.enabled = true,
    this.onTap,
  });

  final String title;
  final String? subtitle;
  final Widget? leading;
  final bool selected;
  final bool enabled;
  final VoidCallback? onTap;

  @override
  State<KinaiyaSidebarDestination> createState() =>
      _KinaiyaSidebarDestinationState();
}

class _KinaiyaSidebarDestinationState extends State<KinaiyaSidebarDestination> {
  bool _focused = false;
  bool _hovered = false;
  bool _pressed = false;

  bool get _interactive => widget.enabled && widget.onTap != null;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final scheme = theme.colorScheme;
    final actionInk = _readableActionInk(scheme);

    final Color bg;
    if (!_interactive) {
      bg = scheme.onSurface.withValues(alpha: 0.05);
    } else if (_pressed) {
      bg = Color.alphaBlend(
        actionInk.withValues(alpha: 0.20),
        widget.selected ? scheme.primaryContainer : scheme.surfaceContainerLow,
      );
    } else if (_hovered || _focused) {
      bg = Color.alphaBlend(
        actionInk.withValues(alpha: 0.12),
        widget.selected ? scheme.primaryContainer : scheme.surfaceContainerLow,
      );
    } else if (widget.selected) {
      bg = scheme.primaryContainer;
    } else {
      bg = Colors.transparent;
    }

    final Color fg = !_interactive
        ? scheme.onSurface.withValues(alpha: 0.50)
        : widget.selected
        ? scheme.onPrimaryContainer
        : scheme.onSurface;

    final BorderSide borderSide;
    if (!_interactive) {
      borderSide = BorderSide(
        color: scheme.outlineVariant.withValues(alpha: 0.35),
      );
    } else if (_focused) {
      borderSide = BorderSide(color: actionInk, width: 2);
    } else if (_pressed) {
      borderSide = BorderSide(color: actionInk, width: 1.5);
    } else if (_hovered || widget.selected) {
      borderSide = BorderSide(
        color: widget.selected ? actionInk : scheme.outlineVariant,
        width: 1,
      );
    } else {
      borderSide = BorderSide.none;
    }

    return Semantics(
      selected: widget.selected,
      enabled: _interactive,
      button: true,
      child: ConstrainedBox(
        constraints: const BoxConstraints(minWidth: 48, minHeight: 48),
        child: Material(
          color: bg,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(8),
            side: borderSide,
          ),
          clipBehavior: Clip.antiAlias,
          child: InkWell(
            canRequestFocus: _interactive,
            onTap: _interactive ? widget.onTap : null,
            onFocusChange: (value) => setState(() => _focused = value),
            onHover: (value) => setState(() => _hovered = value),
            onHighlightChanged: (value) => setState(() => _pressed = value),
            borderRadius: BorderRadius.circular(8),
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
              child: Row(
                children: [
                  if (widget.leading != null) ...[
                    IconTheme.merge(
                      data: IconThemeData(color: fg, size: 20),
                      child: widget.leading!,
                    ),
                    const SizedBox(width: 12),
                  ],
                  Expanded(
                    child: Column(
                      mainAxisSize: MainAxisSize.min,
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          widget.title,
                          style: theme.textTheme.bodyLarge?.copyWith(
                            color: fg,
                            fontWeight: widget.selected || _focused
                                ? FontWeight.w600
                                : FontWeight.w500,
                          ),
                        ),
                        if (widget.subtitle != null) ...[
                          const SizedBox(height: 2),
                          Text(
                            widget.subtitle!,
                            maxLines: 2,
                            overflow: TextOverflow.ellipsis,
                            style: theme.textTheme.bodySmall?.copyWith(
                              color: fg.withValues(alpha: 0.82),
                            ),
                          ),
                        ],
                      ],
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
}
