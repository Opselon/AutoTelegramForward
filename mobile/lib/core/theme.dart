/// Accessible, brand-aligned Material 3 theme and design tokens.
///
/// Palette matches the React Web Dashboard:
///   * Primary brand: emerald (`#059669` / `#10B981`)
///   * Surfaces: neutral slate (`#0F172A` dark, `#F8FAFC` light)
///   * Accents: cyan (`#06B6D4`), amber warnings (`#F59E0B`), red errors (`#EF4444`)
///
/// The token classes ([AppSpacing], [AppRadii], [AppGradients], [AppShadows])
/// exist so screens never hardcode a magic number: spacing, corner radius,
/// gradients and elevation all resolve through one place, which is what keeps
/// the dashboard, rules, logs and settings screens visually consistent as the
/// app grows. A hardcoded `EdgeInsets.all(13)` in one screen and `16` in the
/// next is the usual way that consistency rots.
library;

import 'package:flutter/material.dart';

/// Spacing scale (4pt grid). Use these instead of raw numbers.
class AppSpacing {
  const AppSpacing._();

  static const double xs = 4;
  static const double sm = 8;
  static const double md = 12;
  static const double lg = 16;
  static const double xl = 24;
  static const double xxl = 32;

  /// Screen-level padding used by every top-level page body.
  static const EdgeInsets screen = EdgeInsets.all(lg);

  /// Standard gap between stacked cards / sections.
  static const SizedBox gapSm = SizedBox(height: sm);
  static const SizedBox gapMd = SizedBox(height: md);
  static const SizedBox gapLg = SizedBox(height: lg);
  static const SizedBox gapXl = SizedBox(height: xl);
}

/// Corner radius scale.
class AppRadii {
  const AppRadii._();

  static const double sm = 8;
  static const double md = 12;
  static const double lg = 18;
  static const double pill = 999;

  static const BorderRadius allSm = BorderRadius.all(Radius.circular(sm));
  static const BorderRadius allMd = BorderRadius.all(Radius.circular(md));
  static const BorderRadius allLg = BorderRadius.all(Radius.circular(lg));
  static const BorderRadius allPill = BorderRadius.all(Radius.circular(pill));
}

/// Brand gradients shared by the header, stat tiles and primary actions.
class AppGradients {
  const AppGradients._();

  static const LinearGradient brand = LinearGradient(
    begin: Alignment.topLeft,
    end: Alignment.bottomRight,
    colors: [Color(0xFF10B981), Color(0xFF06B6D4)],
  );

  static const LinearGradient success = LinearGradient(
    begin: Alignment.topLeft,
    end: Alignment.bottomRight,
    colors: [Color(0xFF10B981), Color(0xFF059669)],
  );

  static const LinearGradient cyan = LinearGradient(
    begin: Alignment.topLeft,
    end: Alignment.bottomRight,
    colors: [Color(0xFF06B6D4), Color(0xFF3B82F6)],
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

/// Elevation presets, theme-aware (dark surfaces need glow, light needs shadow).
class AppShadows {
  const AppShadows._();

  static List<BoxShadow> dark(Brightness b) =>
      b == Brightness.dark
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

  static const Color primary = Color(0xFF10B981);
  static const Color primaryDark = Color(0xFF059669);
  static const Color accent = Color(0xFF06B6D4);
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
    if (healthy) return primary;
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
  static TextTheme _textTheme(Brightness brightness) {
    final onSurface =
        brightness == Brightness.dark
            ? const Color(0xFFE2E8F0)
            : const Color(0xFF0F172A);
    final muted =
        brightness == Brightness.dark
            ? const Color(0xFF94A3B8)
            : const Color(0xFF64748B);
    return TextTheme(
      headlineSmall: TextStyle(
        fontWeight: FontWeight.w700,
        letterSpacing: -0.4,
        color: onSurface,
      ),
      titleLarge: TextStyle(
        fontWeight: FontWeight.w700,
        letterSpacing: -0.2,
        color: onSurface,
      ),
      titleMedium: TextStyle(fontWeight: FontWeight.w600, color: onSurface),
      titleSmall: TextStyle(fontWeight: FontWeight.w600, color: onSurface),
      bodyLarge: TextStyle(height: 1.4, color: onSurface),
      bodyMedium: TextStyle(height: 1.45, color: onSurface),
      bodySmall: TextStyle(height: 1.4, color: muted),
      labelLarge: const TextStyle(fontWeight: FontWeight.w600),
      labelSmall: TextStyle(
        fontWeight: FontWeight.w600,
        letterSpacing: 0.4,
        color: muted,
      ),
    );
  }

  static ThemeData dark() {
    final base = ThemeData.dark(useMaterial3: true);
    const surface = darkSurface;
    const border = darkBorder;
    return _build(
      base: base,
      brightness: Brightness.dark,
      background: darkBackground,
      surface: surface,
      border: border,
      seed: primary,
    );
  }

  static ThemeData light() {
    final base = ThemeData.light(useMaterial3: true);
    const surface = lightSurface;
    const border = lightBorder;
    return _build(
      base: base,
      brightness: Brightness.light,
      background: lightBackground,
      surface: surface,
      border: border,
      seed: primaryDark,
    );
  }

  static ThemeData _build({
    required ThemeData base,
    required Brightness brightness,
    required Color background,
    required Color surface,
    required Color border,
    required Color seed,
  }) {
    final isDark = brightness == Brightness.dark;
    final scheme = ColorScheme.fromSeed(
      seedColor: seed,
      brightness: brightness,
      surface: surface,
      error: error,
    );

    return base.copyWith(
      scaffoldBackgroundColor: background,
      colorScheme: scheme,
      textTheme: _textTheme(brightness),
      iconTheme: IconThemeData(color: scheme.onSurface.withValues(alpha: 0.85)),
      cardTheme: CardTheme(
        color: surface,
        elevation: 0,
        margin: EdgeInsets.zero,
        clipBehavior: Clip.antiAlias,
        shape: RoundedRectangleBorder(
          side: BorderSide(color: border),
          borderRadius: AppRadii.allLg,
        ),
      ),
      appBarTheme: AppBarTheme(
        backgroundColor: background,
        surfaceTintColor: Colors.transparent,
        elevation: 0,
        scrolledUnderElevation: 0,
        centerTitle: false,
        titleTextStyle: _textTheme(brightness).titleLarge,
      ),
      navigationBarTheme: NavigationBarThemeData(
        backgroundColor: isDark ? const Color(0xFF111827) : Colors.white,
        surfaceTintColor: Colors.transparent,
        elevation: 0,
        height: 68,
        indicatorColor: primary.withValues(alpha: isDark ? 0.22 : 0.16),
        labelBehavior: NavigationDestinationLabelBehavior.alwaysShow,
        labelTextStyle: WidgetStateProperty.resolveWith((states) {
          final selected = states.contains(WidgetState.selected);
          return TextStyle(
            fontSize: 11,
            fontWeight: selected ? FontWeight.w700 : FontWeight.w500,
            color:
                selected
                    ? scheme.onSurface
                    : scheme.onSurface.withValues(alpha: 0.6),
          );
        }),
        iconTheme: WidgetStateProperty.resolveWith((states) {
          final selected = states.contains(WidgetState.selected);
          return IconThemeData(
            size: 22,
            color: selected ? primary : scheme.onSurface.withValues(alpha: 0.6),
          );
        }),
      ),
      navigationRailTheme: NavigationRailThemeData(
        backgroundColor: isDark ? const Color(0xFF111827) : Colors.white,
        indicatorColor: primary.withValues(alpha: isDark ? 0.22 : 0.16),
        selectedIconTheme: const IconThemeData(color: primary, size: 22),
        unselectedIconTheme: IconThemeData(
          color: scheme.onSurface.withValues(alpha: 0.6),
          size: 22,
        ),
        selectedLabelTextStyle: const TextStyle(
          fontWeight: FontWeight.w700,
          fontSize: 12,
        ),
        unselectedLabelTextStyle: TextStyle(
          color: scheme.onSurface.withValues(alpha: 0.6),
          fontSize: 12,
        ),
      ),
      dividerTheme: DividerThemeData(
        color: border.withValues(alpha: 0.7),
        thickness: 1,
        space: 1,
      ),
      listTileTheme: ListTileThemeData(
        iconColor: scheme.onSurface.withValues(alpha: 0.75),
        contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
        minVerticalPadding: 10,
        shape: RoundedRectangleBorder(borderRadius: AppRadii.allMd),
      ),
      chipTheme: ChipThemeData(
        backgroundColor: primary.withValues(alpha: isDark ? 0.14 : 0.1),
        side: BorderSide(color: primary.withValues(alpha: 0.35)),
        labelStyle: const TextStyle(fontSize: 11, fontWeight: FontWeight.w600),
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
        shape: const StadiumBorder(),
      ),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: isDark ? const Color(0xFF0F172A) : Colors.white,
        hintStyle: TextStyle(color: scheme.onSurface.withValues(alpha: 0.45)),
        labelStyle: TextStyle(color: scheme.onSurface.withValues(alpha: 0.7)),
        border: OutlineInputBorder(
          borderRadius: AppRadii.allMd,
          borderSide: BorderSide(color: border),
        ),
        enabledBorder: OutlineInputBorder(
          borderRadius: AppRadii.allMd,
          borderSide: BorderSide(color: border),
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: AppRadii.allMd,
          borderSide: const BorderSide(color: primary, width: 2),
        ),
        errorBorder: OutlineInputBorder(
          borderRadius: AppRadii.allMd,
          borderSide: const BorderSide(color: error),
        ),
        focusedErrorBorder: OutlineInputBorder(
          borderRadius: AppRadii.allMd,
          borderSide: const BorderSide(color: error, width: 2),
        ),
        contentPadding: const EdgeInsets.symmetric(
          horizontal: 16,
          vertical: 14,
        ),
      ),
      filledButtonTheme: FilledButtonThemeData(
        style: FilledButton.styleFrom(
          backgroundColor: primary,
          foregroundColor: Colors.white,
          minimumSize: const Size(0, 48),
          shape: RoundedRectangleBorder(borderRadius: AppRadii.allMd),
          textStyle: const TextStyle(fontWeight: FontWeight.w600, fontSize: 15),
        ),
      ),
      elevatedButtonTheme: ElevatedButtonThemeData(
        style: ElevatedButton.styleFrom(
          backgroundColor: primary,
          foregroundColor: Colors.white,
          elevation: 0,
          minimumSize: const Size(double.infinity, 48),
          shape: RoundedRectangleBorder(borderRadius: AppRadii.allMd),
          textStyle: const TextStyle(fontWeight: FontWeight.w600, fontSize: 16),
        ),
      ),
      outlinedButtonTheme: OutlinedButtonThemeData(
        style: OutlinedButton.styleFrom(
          foregroundColor: scheme.onSurface,
          minimumSize: const Size(0, 46),
          side: BorderSide(color: border),
          shape: RoundedRectangleBorder(borderRadius: AppRadii.allMd),
          textStyle: const TextStyle(fontWeight: FontWeight.w600),
        ),
      ),
      textButtonTheme: TextButtonThemeData(
        style: TextButton.styleFrom(
          foregroundColor: primary,
          textStyle: const TextStyle(fontWeight: FontWeight.w600),
        ),
      ),
      iconButtonTheme: IconButtonThemeData(
        style: IconButton.styleFrom(
          foregroundColor: scheme.onSurface.withValues(alpha: 0.8),
        ),
      ),
      switchTheme: SwitchThemeData(
        thumbColor: WidgetStateProperty.resolveWith(
          (s) => s.contains(WidgetState.selected) ? Colors.white : null,
        ),
        trackColor: WidgetStateProperty.resolveWith(
          (s) => s.contains(WidgetState.selected) ? primary : null,
        ),
      ),
      segmentedButtonTheme: SegmentedButtonThemeData(
        style: SegmentedButton.styleFrom(
          selectedBackgroundColor: primary.withValues(alpha: 0.2),
          selectedForegroundColor: primary,
          side: BorderSide(color: border),
        ),
      ),
      progressIndicatorTheme: const ProgressIndicatorThemeData(
        color: primary,
        linearMinHeight: 3,
      ),
      tooltipTheme: TooltipThemeData(
        decoration: BoxDecoration(
          color: isDark ? const Color(0xFF0B1220) : const Color(0xFF0F172A),
          borderRadius: AppRadii.allSm,
        ),
        textStyle: const TextStyle(color: Colors.white, fontSize: 12),
      ),
      snackBarTheme: SnackBarThemeData(
        behavior: SnackBarBehavior.floating,
        backgroundColor:
            isDark ? const Color(0xFF0B1220) : const Color(0xFF0F172A),
        contentTextStyle: const TextStyle(color: Colors.white),
        shape: RoundedRectangleBorder(borderRadius: AppRadii.allMd),
      ),
      bottomSheetTheme: BottomSheetThemeData(
        backgroundColor: surface,
        surfaceTintColor: Colors.transparent,
        showDragHandle: true,
        shape: const RoundedRectangleBorder(
          borderRadius: BorderRadius.vertical(
            top: Radius.circular(AppRadii.lg),
          ),
        ),
      ),
      popupMenuTheme: PopupMenuThemeData(
        color: surface,
        surfaceTintColor: Colors.transparent,
        shape: RoundedRectangleBorder(
          side: BorderSide(color: border),
          borderRadius: AppRadii.allMd,
        ),
      ),
      splashFactory: InkSparkle.splashFactory,
    );
  }
}
