/// Accessible, brand-aligned Material 3 theme, built on the shared tokens.
///
/// Palette matches the React Web Dashboard **and** the `atf` CLI: one product,
/// one set of hexes — and those hexes live here (or in `design_tokens.dart`),
/// never in a screen.
///   * Brand: indigo (`#6366F1`) on a sky -> indigo -> violet gradient
///   * Success/healthy: emerald (`#10B981`), kept for status semantics only
///   * Surfaces: neutral slate (`#0F172A` dark, `#F8FAFC` light)
///   * Accents: sky (`#38BDF8`), violet (`#A855F7`), amber warnings
///     (`#F59E0B`), red errors (`#EF4444`)
///
/// [AppPalette] publishes those colours by role so a component asks for
/// `surface2` or `danger` and is correct in both themes; [AppRadius],
/// [AppSpacing], [AppType] and [AppDurations] are re-exported from
/// `design_tokens.dart` so `import '../core/theme.dart'` is still the one import
/// a screen needs.
library;

import 'package:flutter/material.dart';

import 'design_tokens.dart';

export 'design_tokens.dart';

/// Legacy alias for the radius scale. New code should use [AppRadius].
class AppRadii {
  const AppRadii._();

  static const double sm = AppRadius.sm;
  static const double md = AppRadius.md;
  static const double lg = AppRadius.lg;
  static const double pill = AppRadius.full;

  static const BorderRadius allSm = AppRadius.allSm;
  static const BorderRadius allMd = AppRadius.allMd;
  static const BorderRadius allLg = AppRadius.allLg;
  static const BorderRadius allPill = AppRadius.allFull;
}

/// Legacy alias for the motion tokens. New code should use [AppDurations].
class AppMotion {
  const AppMotion._();

  static const Duration fast = AppDurations.fast;
  static const Duration base = AppDurations.base;
  static const Duration slow = AppDurations.slow;

  static const Curve ease = AppDurations.ease;
}

/// Brand gradients shared by the header, stat tiles and primary actions.
class AppGradients {
  const AppGradients._();

  static const LinearGradient brand = LinearGradient(
    begin: Alignment.topLeft,
    end: Alignment.bottomRight,
    colors: [Color(0xFF38BDF8), Color(0xFF6366F1), Color(0xFFA855F7)],
  );

  static const LinearGradient success = LinearGradient(
    begin: Alignment.topLeft,
    end: Alignment.bottomRight,
    colors: [Color(0xFF10B981), Color(0xFF14B8A6)],
  );

  static const LinearGradient cyan = LinearGradient(
    begin: Alignment.topLeft,
    end: Alignment.bottomRight,
    colors: [Color(0xFF38BDF8), Color(0xFF06B6D4)],
  );

  static const LinearGradient violet = LinearGradient(
    begin: Alignment.topLeft,
    end: Alignment.bottomRight,
    colors: [Color(0xFFA855F7), Color(0xFF6366F1)],
  );

  static const LinearGradient danger = LinearGradient(
    begin: Alignment.topLeft,
    end: Alignment.bottomRight,
    colors: [Color(0xFFEF4444), Color(0xFFB91C1C)],
  );

  static const LinearGradient gold = LinearGradient(
    begin: Alignment.topLeft,
    end: Alignment.bottomRight,
    colors: [Color(0xFFF59E0B), Color(0xFFD97706)],
  );

  /// Subtle top-lit sheen applied over dark surfaces.
  static LinearGradient surfaceSheen(Color base) => LinearGradient(
    begin: Alignment.topCenter,
    end: Alignment.bottomCenter,
    colors: [
      Color.alphaBlend(Colors.white.withValues(alpha: 0.04), base),
      base,
    ],
  );
}

/// Frosted-glass surface treatment for panels that must sit *over* the page
/// background (hero cards, stat tiles) rather than next to it.
class AppGlass {
  const AppGlass._();

  static const double opacity = 0.06;

  static BoxDecoration panel({
    required Brightness brightness,
    Gradient? gradient,
    Color? tint,
    double radius = AppRadius.lg,
    bool highlight = true,
  }) {
    final dark = brightness == Brightness.dark;
    final base =
        tint ?? (dark ? AppPalette.dark.surface2 : AppPalette.light.surface2);
    return BoxDecoration(
      gradient: gradient,
      color: gradient == null ? base.withValues(alpha: 0.72) : null,
      borderRadius: BorderRadius.circular(radius),
      border: Border.all(
        color: highlight
            ? Colors.white.withValues(alpha: dark ? 0.10 : 0.6)
            : Colors.transparent,
      ),
      boxShadow: AppShadows.dark(brightness),
    );
  }
}

/// Elevation presets, theme-aware (dark surfaces need glow, light needs shadow).
class AppShadows {
  const AppShadows._();

  static List<BoxShadow> dark(Brightness b) => b == Brightness.dark
      ? const [
          BoxShadow(
            color: Color(0x33000000),
            blurRadius: 18,
            offset: Offset(0, 8),
          ),
        ]
      : const [
          BoxShadow(
            color: Color(0x14000000),
            blurRadius: 10,
            offset: Offset(0, 4),
          ),
        ];
}

class AppTheme {
  const AppTheme._();

  static const Color primary = Color(0xFF6366F1);
  static const Color primaryDark = Color(0xFF4F46E5);
  static const Color accent = Color(0xFFA855F7);
  static const Color sky = Color(0xFF38BDF8);

  /// Reserved for "everything is healthy" - the brand hue means *brand*, not
  /// *success*, so status colours get their own names.
  static const Color success = Color(0xFF10B981);
  static const Color teal = Color(0xFF14B8A6);
  static const Color error = Color(0xFFEF4444);
  static const Color warning = Color(0xFFF59E0B);
  static const Color info = Color(0xFF3B82F6);

  // Dark palette (default)
  static const Color darkBackground = Color(0xFF0F172A);
  static const Color darkSurface = Color(0xFF1E293B);
  static const Color darkCard = Color(0xFF1E293B);
  static const Color darkBorder = Color(0xFF334155);

  // Light palette
  static const Color lightBackground = Color(0xFFF8FAFC);
  static const Color lightSurface = Color(0xFFFFFFFF);
  static const Color lightCard = Color(0xFFFFFFFF);
  static const Color lightBorder = Color(0xFFE2E8F0);

  /// The colour that represents a service/rule health state everywhere.
  static Color statusColor(bool healthy, {bool neutralWhenFalse = false}) {
    if (healthy) return success;
    return neutralWhenFalse ? const Color(0xFF94A3B8) : error;
  }

  /// Maps a finding severity onto the shared severity palette, so the mobile
  /// preflight list and the web diagnostics panel agree on "error is red".
  static Color severityColor(String severity) {
    switch (severity) {
      case 'error':
        return error;
      case 'warning':
        return warning;
      default:
        return info;
    }
  }

  /// A compact type scale: M3 defaults are tuned for phones, but our dense
  /// operational screens need slightly tighter line heights and clearer
  /// weight separation between labels and values.
  ///
  /// Sizes come from [AppType], so the scale is the one the spec publishes:
  /// 12 / 13 / 14 / 16 / 20 / 24 / 32. Both themes share the same shape and
  /// differ only in colour, which is what "dark and light parity" means here.
  static TextTheme textThemeFor(Brightness brightness) {
    final palette = brightness == Brightness.dark
        ? AppPalette.dark
        : AppPalette.light;
    final onSurface = palette.text;
    final muted = palette.textMuted;
    return TextTheme(
      displaySmall: TextStyle(
        fontSize: AppType.display,
        fontWeight: FontWeight.w800,
        letterSpacing: -1,
        height: 1.15,
        color: onSurface,
      ),
      headlineMedium: TextStyle(
        fontSize: AppType.xxl,
        fontWeight: FontWeight.w700,
        letterSpacing: -0.6,
        height: 1.2,
        color: onSurface,
      ),
      headlineSmall: TextStyle(
        fontSize: AppType.xl,
        fontWeight: FontWeight.w700,
        letterSpacing: -0.4,
        height: 1.25,
        color: onSurface,
      ),
      titleLarge: TextStyle(
        fontSize: AppType.xl,
        fontWeight: FontWeight.w700,
        letterSpacing: -0.2,
        height: 1.25,
        color: onSurface,
      ),
      titleMedium: TextStyle(
        fontSize: AppType.lg,
        fontWeight: FontWeight.w600,
        height: 1.3,
        color: onSurface,
      ),
      titleSmall: TextStyle(
        fontSize: AppType.md,
        fontWeight: FontWeight.w600,
        height: 1.3,
        color: onSurface,
      ),
      bodyLarge: TextStyle(fontSize: AppType.lg, height: 1.4, color: onSurface),
      bodyMedium: TextStyle(
        fontSize: AppType.md,
        height: 1.45,
        color: onSurface,
      ),
      bodySmall: TextStyle(fontSize: AppType.xs, height: 1.4, color: muted),
      labelLarge: TextStyle(
        fontSize: AppType.md,
        fontWeight: FontWeight.w600,
        height: 1.3,
        color: onSurface,
      ),
      labelMedium: TextStyle(
        fontSize: AppType.sm,
        fontWeight: FontWeight.w600,
        height: 1.3,
        color: onSurface,
      ),
      labelSmall: TextStyle(
        fontSize: AppType.xs,
        fontWeight: FontWeight.w600,
        letterSpacing: 0.4,
        color: muted,
      ),
    );
  }

  static ThemeData dark() => _build(
    brightness: Brightness.dark,
    palette: AppPalette.dark,
    seed: primary,
  );

  static ThemeData light() => _build(
    brightness: Brightness.light,
    palette: AppPalette.light,
    seed: primaryDark,
  );

  static ThemeData _build({
    required Brightness brightness,
    required AppPalette palette,
    required Color seed,
  }) {
    final isDark = brightness == Brightness.dark;
    final base = isDark
        ? ThemeData.dark(useMaterial3: true)
        : ThemeData.light(useMaterial3: true);

    // M3 hands the seeded scheme the container ladder; we pin the surface
    // levels to the brand neutrals so the dashboard, the web app and the bot
    // agree on what "surface" looks like.
    final scheme =
        ColorScheme.fromSeed(
          seedColor: seed,
          brightness: brightness,
          surface: palette.surface2,
          error: palette.danger,
        ).copyWith(
          primary: palette.primary,
          onPrimary: palette.onPrimary,
          surface: palette.surface,
          onSurface: palette.text,
          onSurfaceVariant: palette.textMuted,
          outline: palette.border,
          outlineVariant: palette.border,
          surfaceContainerLowest: palette.surface,
          surfaceContainerLow: palette.surface2,
          surfaceContainer: palette.surface2,
          surfaceContainerHigh: palette.surface3,
          surfaceContainerHighest: palette.surface3,
        );

    final textTheme = textThemeFor(brightness);

    return base.copyWith(
      brightness: brightness,
      scaffoldBackgroundColor: palette.surface,
      canvasColor: palette.surface,
      colorScheme: scheme,
      textTheme: textTheme,
      extensions: <ThemeExtension<dynamic>>[palette],
      iconTheme: IconThemeData(color: palette.text.withValues(alpha: 0.85)),
      cardTheme: CardThemeData(
        color: palette.surface2,
        elevation: AppElevation.level1,
        margin: EdgeInsets.zero,
        clipBehavior: Clip.antiAlias,
        shape: RoundedRectangleBorder(
          side: BorderSide(color: palette.border),
          borderRadius: AppRadius.allMd,
        ),
      ),
      appBarTheme: AppBarTheme(
        backgroundColor: palette.surface,
        surfaceTintColor: Colors.transparent,
        elevation: 0,
        scrolledUnderElevation: 0,
        centerTitle: false,
        titleTextStyle: textTheme.titleLarge,
      ),
      navigationBarTheme: NavigationBarThemeData(
        backgroundColor: isDark ? const Color(0xFF111827) : Colors.white,
        surfaceTintColor: Colors.transparent,
        elevation: 0,
        height: 72,
        indicatorColor: palette.primary.withValues(alpha: isDark ? 0.22 : 0.16),
        labelBehavior: NavigationDestinationLabelBehavior.alwaysShow,
        labelTextStyle: WidgetStateProperty.resolveWith((states) {
          final selected = states.contains(WidgetState.selected);
          return TextStyle(
            fontSize: AppType.xs,
            fontWeight: selected ? FontWeight.w700 : FontWeight.w500,
            color: selected
                ? palette.text
                : palette.text.withValues(alpha: 0.7),
          );
        }),
        iconTheme: WidgetStateProperty.resolveWith((states) {
          final selected = states.contains(WidgetState.selected);
          return IconThemeData(
            size: 24,
            color: selected
                ? palette.primary
                : palette.text.withValues(alpha: 0.7),
          );
        }),
      ),
      navigationRailTheme: NavigationRailThemeData(
        backgroundColor: isDark ? const Color(0xFF111827) : Colors.white,
        indicatorColor: palette.primary.withValues(alpha: isDark ? 0.22 : 0.16),
        selectedIconTheme: IconThemeData(color: palette.primary, size: 22),
        unselectedIconTheme: IconThemeData(
          color: palette.text.withValues(alpha: 0.7),
          size: 22,
        ),
        selectedLabelTextStyle: TextStyle(
          fontWeight: FontWeight.w700,
          fontSize: AppType.xs,
          color: palette.text,
        ),
        unselectedLabelTextStyle: TextStyle(
          color: palette.text.withValues(alpha: 0.7),
          fontSize: AppType.xs,
        ),
      ),
      dividerTheme: DividerThemeData(
        color: palette.border.withValues(alpha: 0.7),
        thickness: 1,
        space: 1,
      ),
      listTileTheme: ListTileThemeData(
        iconColor: palette.text.withValues(alpha: 0.75),
        contentPadding: const EdgeInsets.symmetric(
          horizontal: AppSpacing.lg,
          vertical: AppSpacing.xs,
        ),
        minVerticalPadding: AppSpacing.sm,
        shape: RoundedRectangleBorder(borderRadius: AppRadius.allMd),
      ),
      chipTheme: ChipThemeData(
        backgroundColor: palette.primary.withValues(alpha: isDark ? 0.14 : 0.1),
        side: BorderSide(color: palette.primary.withValues(alpha: 0.35)),
        labelStyle: TextStyle(
          fontSize: AppType.xs,
          fontWeight: FontWeight.w600,
        ),
        padding: const EdgeInsets.symmetric(
          horizontal: AppSpacing.sm,
          vertical: 2,
        ),
        shape: const StadiumBorder(),
      ),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: palette.surface3,
        hintStyle: TextStyle(color: palette.textMuted),
        labelStyle: TextStyle(color: palette.textMuted),
        floatingLabelStyle: TextStyle(color: palette.primary),
        border: OutlineInputBorder(
          borderRadius: AppRadius.allMd,
          borderSide: BorderSide(color: palette.border),
        ),
        enabledBorder: OutlineInputBorder(
          borderRadius: AppRadius.allMd,
          borderSide: BorderSide(color: palette.border),
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: AppRadius.allMd,
          borderSide: BorderSide(color: palette.primary, width: 2),
        ),
        errorBorder: OutlineInputBorder(
          borderRadius: AppRadius.allMd,
          borderSide: BorderSide(color: palette.danger),
        ),
        focusedErrorBorder: OutlineInputBorder(
          borderRadius: AppRadius.allMd,
          borderSide: BorderSide(color: palette.danger, width: 2),
        ),
        contentPadding: const EdgeInsets.symmetric(
          horizontal: AppSpacing.lg,
          vertical: 14,
        ),
      ),
      filledButtonTheme: FilledButtonThemeData(
        style: FilledButton.styleFrom(
          backgroundColor: palette.primary,
          foregroundColor: palette.onPrimary,
          disabledBackgroundColor: palette.primary.withValues(alpha: 0.4),
          minimumSize: const Size(0, 48),
          shape: RoundedRectangleBorder(borderRadius: AppRadius.allMd),
          textStyle: TextStyle(
            fontWeight: FontWeight.w600,
            fontSize: AppType.md,
          ),
        ),
      ),
      elevatedButtonTheme: ElevatedButtonThemeData(
        style: ElevatedButton.styleFrom(
          backgroundColor: palette.primary,
          foregroundColor: palette.onPrimary,
          elevation: 0,
          minimumSize: const Size(0, 48),
          shape: RoundedRectangleBorder(borderRadius: AppRadius.allMd),
          textStyle: TextStyle(
            fontWeight: FontWeight.w600,
            fontSize: AppType.md,
          ),
        ),
      ),
      outlinedButtonTheme: OutlinedButtonThemeData(
        style: OutlinedButton.styleFrom(
          foregroundColor: palette.text,
          minimumSize: const Size(0, 48),
          side: BorderSide(color: palette.border),
          shape: RoundedRectangleBorder(borderRadius: AppRadius.allMd),
          textStyle: TextStyle(
            fontWeight: FontWeight.w600,
            fontSize: AppType.md,
          ),
        ),
      ),
      textButtonTheme: TextButtonThemeData(
        style: TextButton.styleFrom(
          foregroundColor: palette.primary,
          minimumSize: const Size(48, 48),
          textStyle: TextStyle(
            fontWeight: FontWeight.w600,
            fontSize: AppType.md,
          ),
        ),
      ),
      iconButtonTheme: IconButtonThemeData(
        style: IconButton.styleFrom(
          foregroundColor: palette.text.withValues(alpha: 0.8),
          minimumSize: const Size(48, 48),
        ),
      ),
      switchTheme: SwitchThemeData(
        thumbColor: WidgetStateProperty.resolveWith(
          (s) => s.contains(WidgetState.selected) ? Colors.white : null,
        ),
        trackColor: WidgetStateProperty.resolveWith(
          (s) => s.contains(WidgetState.selected) ? palette.primary : null,
        ),
      ),
      segmentedButtonTheme: SegmentedButtonThemeData(
        style: SegmentedButton.styleFrom(
          selectedBackgroundColor: palette.primary.withValues(alpha: 0.2),
          selectedForegroundColor: palette.primary,
          side: BorderSide(color: palette.border),
        ),
      ),
      progressIndicatorTheme: ProgressIndicatorThemeData(
        color: palette.primary,
        linearMinHeight: 3,
      ),
      tooltipTheme: TooltipThemeData(
        decoration: BoxDecoration(
          color: isDark ? const Color(0xFF0B1220) : const Color(0xFF0F172A),
          borderRadius: AppRadius.allSm,
        ),
        textStyle: const TextStyle(color: Colors.white, fontSize: AppType.xs),
      ),
      snackBarTheme: SnackBarThemeData(
        behavior: SnackBarBehavior.floating,
        backgroundColor: isDark
            ? const Color(0xFF0B1220)
            : const Color(0xFF0F172A),
        contentTextStyle: const TextStyle(color: Colors.white),
        shape: RoundedRectangleBorder(borderRadius: AppRadius.allMd),
        insetPadding: const EdgeInsets.all(AppSpacing.lg),
      ),
      bottomSheetTheme: BottomSheetThemeData(
        backgroundColor: palette.surface2,
        surfaceTintColor: Colors.transparent,
        showDragHandle: true,
        shape: const RoundedRectangleBorder(
          borderRadius: BorderRadius.vertical(
            top: Radius.circular(AppRadius.lg),
          ),
        ),
      ),
      popupMenuTheme: PopupMenuThemeData(
        color: palette.surface2,
        surfaceTintColor: Colors.transparent,
        shape: RoundedRectangleBorder(
          side: BorderSide(color: palette.border),
          borderRadius: AppRadius.allMd,
        ),
      ),
      dialogTheme: DialogThemeData(
        backgroundColor: palette.surface2,
        surfaceTintColor: Colors.transparent,
        shape: RoundedRectangleBorder(borderRadius: AppRadius.allLg),
      ),
      // A page change is 200 ms of fade-through, not the platform's zoom:
      // consistent motion on every Android version, and it respects the
      // reduced-motion flag through the framework.
      pageTransitionsTheme: PageTransitionsTheme(
        builders: <TargetPlatform, PageTransitionsBuilder>{
          TargetPlatform.android: const FadeForwardsPageTransitionsBuilder(),
          TargetPlatform.linux: const FadeForwardsPageTransitionsBuilder(),
          TargetPlatform.macOS: const FadeForwardsPageTransitionsBuilder(),
          TargetPlatform.windows: const FadeForwardsPageTransitionsBuilder(),
        },
      ),
      visualDensity: VisualDensity.standard,
      materialTapTargetSize: MaterialTapTargetSize.padded,
      splashFactory: InkSparkle.splashFactory,
    );
  }
}
