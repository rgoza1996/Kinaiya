import 'package:flutter/material.dart';

// Kinaiya brand ink. Presentation only; never gates speech, runtime, or evidence.
const Color kinaiyaIndigo = Color(0xFF141091);
const Color kinaiyaWhite = Color(0xFFFFFFFF);
const Color kinaiyaNavy = Color(0xFF07172D);
const Color kinaiyaMist = Color(0xFFF0F3FF);

// Global Material3 theme for light and dark surfaces.
// Light primary is exactly kinaiyaIndigo with white onPrimary.
// Dark keeps indigo/white identity with light ink on navy surfaces.
// No fixed widths and no tiny text are hardcoded here.
ThemeData kinaiyaTheme({Brightness brightness = Brightness.light}) {
  final bool isDark = brightness == Brightness.dark;

  final ColorScheme scheme = isDark
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

  final TextTheme baseText = isDark
      ? ThemeData.dark().textTheme
      : ThemeData.light().textTheme;
  final TextTheme textTheme = baseText.apply(
    bodyColor: scheme.onSurface,
    displayColor: scheme.onSurface,
  );

  const Size minTap = Size(48, 48);
  final RoundedRectangleBorder buttonShape = RoundedRectangleBorder(
    borderRadius: BorderRadius.circular(12),
  );
  final RoundedRectangleBorder cardShape = RoundedRectangleBorder(
    borderRadius: BorderRadius.circular(16),
    side: BorderSide(color: scheme.outlineVariant),
  );

  final Color inputFill = isDark
      ? kinaiyaWhite.withValues(alpha: 0.06)
      : kinaiyaMist.withValues(alpha: 0.55);
  final OutlineInputBorder inputBorder = OutlineInputBorder(
    borderRadius: BorderRadius.circular(12),
    borderSide: BorderSide(color: scheme.outlineVariant),
  );

  return ThemeData(
    useMaterial3: true,
    brightness: brightness,
    colorScheme: scheme,
    scaffoldBackgroundColor: scheme.surface,
    canvasColor: scheme.surface,
    focusColor: scheme.primary.withValues(alpha: 0.14),
    materialTapTargetSize: MaterialTapTargetSize.padded,
    visualDensity: VisualDensity.standard,
    textTheme: textTheme,
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
        borderRadius: BorderRadius.circular(20),
        side: BorderSide(color: scheme.outlineVariant),
      ),
    ),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: inputFill,
      contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 16),
      hintStyle: textTheme.bodyLarge?.copyWith(
        color: scheme.onSurface.withValues(alpha: 0.62),
      ),
      labelStyle: textTheme.bodyLarge?.copyWith(color: scheme.onSurface),
      border: inputBorder,
      enabledBorder: inputBorder,
      focusedBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(12),
        borderSide: BorderSide(color: scheme.primary, width: 2),
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
        minimumSize: minTap,
        shape: buttonShape,
        textStyle: const TextStyle(fontSize: 16, fontWeight: FontWeight.w600),
        disabledBackgroundColor: scheme.onSurface.withValues(alpha: 0.12),
        disabledForegroundColor: scheme.onSurface.withValues(alpha: 0.55),
      ),
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        minimumSize: minTap,
        shape: buttonShape,
        textStyle: const TextStyle(fontSize: 16, fontWeight: FontWeight.w600),
        disabledBackgroundColor: scheme.onSurface.withValues(alpha: 0.12),
        disabledForegroundColor: scheme.onSurface.withValues(alpha: 0.55),
      ),
    ),
    outlinedButtonTheme: OutlinedButtonThemeData(
      style: OutlinedButton.styleFrom(
        minimumSize: minTap,
        shape: buttonShape,
        textStyle: const TextStyle(fontSize: 16, fontWeight: FontWeight.w600),
        disabledForegroundColor: scheme.onSurface.withValues(alpha: 0.55),
      ),
    ),
    textButtonTheme: TextButtonThemeData(
      style: TextButton.styleFrom(
        minimumSize: minTap,
        shape: buttonShape,
        textStyle: const TextStyle(fontSize: 16, fontWeight: FontWeight.w600),
        disabledForegroundColor: scheme.onSurface.withValues(alpha: 0.55),
      ),
    ),
    iconButtonTheme: IconButtonThemeData(
      style: ButtonStyle(
        minimumSize: const WidgetStatePropertyAll<Size>(minTap),
        shape: WidgetStatePropertyAll<OutlinedBorder>(buttonShape),
      ),
    ),
    dividerTheme: DividerThemeData(color: scheme.outlineVariant, thickness: 1),
    snackBarTheme: SnackBarThemeData(
      backgroundColor: isDark ? kinaiyaWhite : kinaiyaNavy,
      contentTextStyle: textTheme.bodyLarge?.copyWith(
        color: isDark ? kinaiyaNavy : kinaiyaWhite,
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
