/// Design tokens shared by every surface. See `docs/ui-ux-system.md`.
///
/// The token *names* are the contract: `AppSpacing.xs..xxl`,
/// `AppRadius.sm..full`, `AppType.xs..xxl`, `AppDurations.fast/base/slow`,
/// `AppElevation.level1..3`. Values are the mobile ones (dp/sp).
///
/// Colours are referenced by **role**, never by hex, in a component. That is
/// what [AppPalette] is for: it is a [ThemeExtension] registered in both the
/// light and the dark theme, so a widget that says `AppColors.of(context).danger`
/// is correct in both themes by construction, and a new hex literal inside a
/// screen is a review failure rather than a style choice.
///
/// The historical names [AppRadii] and [AppMotion] are kept as aliases so the
/// screens that predate this contract keep compiling while they are converted.
library;

import 'package:flutter/material.dart';

/// Spacing scale (4pt grid).
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
  static const SizedBox gapXs = SizedBox(height: xs);
  static const SizedBox gapSm = SizedBox(height: sm);
  static const SizedBox gapMd = SizedBox(height: md);
  static const SizedBox gapLg = SizedBox(height: lg);
  static const SizedBox gapXl = SizedBox(height: xl);
}

/// Corner radius scale: `sm` 8, `md` 12, `lg` 18, `full` pill.
class AppRadius {
  const AppRadius._();

  static const double sm = 8;
  static const double md = 12;
  static const double lg = 18;
  static const double full = 999;

  static const BorderRadius allSm = BorderRadius.all(Radius.circular(sm));
  static const BorderRadius allMd = BorderRadius.all(Radius.circular(md));
  static const BorderRadius allLg = BorderRadius.all(Radius.circular(lg));
  static const BorderRadius allFull = BorderRadius.all(Radius.circular(full));
}

/// Type scale: 12 / 13 / 14 / 16 / 20 / 24 / 32 sp.
///
/// Screens should prefer `Theme.of(context).textTheme`, which is built from the
/// same numbers; these constants exist for the places that must name a size
/// (dense badges, monospace ids) instead of inventing one.
class AppType {
  const AppType._();

  static const double xs = 12;
  static const double sm = 13;
  static const double md = 14;
  static const double lg = 16;
  static const double xl = 20;
  static const double xxl = 24;
  static const double display = 32;

  static const TextStyle xsMuted = TextStyle(fontSize: xs);
  static const TextStyle label = TextStyle(
    fontSize: xs,
    fontWeight: FontWeight.w600,
    letterSpacing: 0.3,
  );
  static const TextStyle mono = TextStyle(
    fontSize: xs,
    fontFamily: 'monospace',
  );
}

/// Motion tokens: hover/press, expand, page change.
class AppDurations {
  const AppDurations._();

  static const Duration fast = Duration(milliseconds: 120);
  static const Duration base = Duration(milliseconds: 200);
  static const Duration slow = Duration(milliseconds: 320);

  /// Decelerating curve: starts quickly and settles, which reads as responsive.
  static const Curve ease = Cubic(0.22, 1, 0.36, 1);
}

/// Elevation ladder: card (1), popover (2), modal (3).
class AppElevation {
  const AppElevation._();

  static const double level1 = 1;
  static const double level2 = 3;
  static const double level3 = 8;

  /// Shadow presets, theme-aware: dark surfaces need a deeper glow than light
  /// ones need a grey shadow, and a hardcoded `0x33000000` in a screen is how
  /// those two drift apart.
  static List<BoxShadow> shadows(BuildContext context, {int level = 1}) {
    final dark = Theme.of(context).brightness == Brightness.dark;
    final alpha = dark
        ? const [0x33, 0x44, 0x55][level - 1]
        : const [0x12, 0x1A, 0x24][level - 1];
    final blur = const [18.0, 26.0, 34.0][level - 1];
    final dy = const [8.0, 12.0, 16.0][level - 1];
    return [
      BoxShadow(
        color: Color((alpha << 24) | 0x000000),
        blurRadius: blur,
        offset: Offset(0, dy),
      ),
    ];
  }
}

/// Colour roles. One instance per theme, so both themes answer every role.
@immutable
class AppPalette extends ThemeExtension<AppPalette> {
  const AppPalette({
    required this.surface,
    required this.surface2,
    required this.surface3,
    required this.border,
    required this.text,
    required this.textMuted,
    required this.primary,
    required this.primaryHover,
    required this.onPrimary,
    required this.success,
    required this.warning,
    required this.danger,
    required this.info,
    required this.neutral,
  });

  /// Page background.
  final Color surface;

  /// Raised panel / card.
  final Color surface2;

  /// Nested panel inside a card (route visualiser, code block).
  final Color surface3;

  /// Hairline border and dividers.
  final Color border;

  /// Body text.
  final Color text;

  /// Secondary text, captions, timestamps.
  final Color textMuted;

  final Color primary;
  final Color primaryHover;
  final Color onPrimary;
  final Color success;
  final Color warning;
  final Color danger;
  final Color info;

  /// Disabled / unknown state.
  final Color neutral;

  /// Soft tint of a role, for backgrounds behind that role's text.
  Color soft(Color role, {double alpha = 0.14}) =>
      role.withValues(alpha: alpha);

  /// Foreground that reaches 4.5:1 on [background] — white or the slate ink,
  /// whichever measures higher. Filled surfaces (buttons, toasts, avatars) use
  /// this instead of assuming white, which fails on every light role.
  Color readableOn(Color background) =>
      contrastRatio(background, const Color(0xFFFFFFFF)) >=
          contrastRatio(background, const Color(0xFF0F172A))
      ? const Color(0xFFFFFFFF)
      : const Color(0xFF0F172A);

  /// WCAG 2.1 relative-luminance contrast ratio, 1..21. Public because the
  /// palette contract is verified by a test, not by eye.
  static double contrastRatio(Color a, Color b) {
    final la = a.computeLuminance();
    final lb = b.computeLuminance();
    final hi = la > lb ? la : lb;
    final lo = la > lb ? lb : la;
    return (hi + 0.05) / (lo + 0.05);
  }

  /// Dark roles are the *light* end of each hue on purpose: a mid-tone
  /// (indigo 500, emerald 500) reads at 2.5–4.0:1 on a slate background, under
  /// the 4.5:1 floor. Each value here is measured against [surface] and
  /// [surface2] by `test/core/palette_contrast_test.dart`.
  static const AppPalette dark = AppPalette(
    surface: Color(0xFF0F172A),
    surface2: Color(0xFF1E293B),
    surface3: Color(0xFF111C31),
    border: Color(0xFF334155),
    text: Color(0xFFE2E8F0),
    textMuted: Color(0xFF94A3B8),
    primary: Color(0xFFA5B4FC),
    primaryHover: Color(0xFF818CF8),
    onPrimary: Color(0xFF0F172A),
    success: Color(0xFF34D399),
    warning: Color(0xFFFBBF24),
    danger: Color(0xFFFCA5A5),
    info: Color(0xFF60A5FA),
    neutral: Color(0xFF94A3B8),
  );

  static const AppPalette light = AppPalette(
    surface: Color(0xFFF8FAFC),
    surface2: Color(0xFFFFFFFF),
    surface3: Color(0xFFF1F5F9),
    border: Color(0xFFE2E8F0),
    text: Color(0xFF0F172A),
    textMuted: Color(0xFF475569),
    primary: Color(0xFF4F46E5),
    primaryHover: Color(0xFF4338CA),
    onPrimary: Color(0xFFFFFFFF),
    success: Color(0xFF065F46),
    warning: Color(0xFF92400E),
    danger: Color(0xFFB91C1C),
    info: Color(0xFF1D4ED8),
    neutral: Color(0xFF64748B),
  );

  @override
  AppPalette copyWith({
    Color? surface,
    Color? surface2,
    Color? surface3,
    Color? border,
    Color? text,
    Color? textMuted,
    Color? primary,
    Color? primaryHover,
    Color? onPrimary,
    Color? success,
    Color? warning,
    Color? danger,
    Color? info,
    Color? neutral,
  }) {
    return AppPalette(
      surface: surface ?? this.surface,
      surface2: surface2 ?? this.surface2,
      surface3: surface3 ?? this.surface3,
      border: border ?? this.border,
      text: text ?? this.text,
      textMuted: textMuted ?? this.textMuted,
      primary: primary ?? this.primary,
      primaryHover: primaryHover ?? this.primaryHover,
      onPrimary: onPrimary ?? this.onPrimary,
      success: success ?? this.success,
      warning: warning ?? this.warning,
      danger: danger ?? this.danger,
      info: info ?? this.info,
      neutral: neutral ?? this.neutral,
    );
  }

  @override
  AppPalette lerp(ThemeExtension<AppPalette>? other, double t) {
    if (other is! AppPalette) return this;
    return AppPalette(
      surface: Color.lerp(surface, other.surface, t)!,
      surface2: Color.lerp(surface2, other.surface2, t)!,
      surface3: Color.lerp(surface3, other.surface3, t)!,
      border: Color.lerp(border, other.border, t)!,
      text: Color.lerp(text, other.text, t)!,
      textMuted: Color.lerp(textMuted, other.textMuted, t)!,
      primary: Color.lerp(primary, other.primary, t)!,
      primaryHover: Color.lerp(primaryHover, other.primaryHover, t)!,
      onPrimary: Color.lerp(onPrimary, other.onPrimary, t)!,
      success: Color.lerp(success, other.success, t)!,
      warning: Color.lerp(warning, other.warning, t)!,
      danger: Color.lerp(danger, other.danger, t)!,
      info: Color.lerp(info, other.info, t)!,
      neutral: Color.lerp(neutral, other.neutral, t)!,
    );
  }
}

/// Role lookup that works on any widget's context, including a context that
/// has no [Theme] above it yet (tests mount widgets in a bare `MaterialApp`).
class AppColors {
  const AppColors._();

  static AppPalette of(BuildContext context) {
    final theme = Theme.of(context);
    return theme.extension<AppPalette>() ??
        (theme.brightness == Brightness.dark
            ? AppPalette.dark
            : AppPalette.light);
  }
}
