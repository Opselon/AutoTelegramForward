/// Button primitive: `primary`, `secondary` (the spec's `ghost` border
/// variant), `ghost` (text), `danger`, `link`.
///
/// Targets: `md` is 48 dp tall, `sm` keeps a 36 dp body inside a 48 dp tap
/// area. The spec's component table lists 44 dp for `md`; the accessibility
/// section requires 48×48 dp on mobile, so the stricter number wins here —
/// reported as a spec conflict rather than silently choosing one.
///
/// Disabled = 40% opacity and no pointer events; loading replaces the label
/// with a spinner while the label keeps its width, so a row of buttons does not
/// jump when one of them starts saving.
library;

import 'package:flutter/material.dart';

import '../core/design_tokens.dart';
import '../core/layout.dart';

enum AppButtonVariant { primary, secondary, ghost, danger, link }

enum AppButtonSize { sm, md }

class AppButton extends StatelessWidget {
  const AppButton({
    super.key,
    required this.label,
    required this.onPressed,
    this.icon,
    this.variant = AppButtonVariant.primary,
    this.size = AppButtonSize.md,
    this.expand = false,
    this.loading = false,
    this.semanticLabel,
  });

  final String label;
  final VoidCallback? onPressed;
  final IconData? icon;
  final AppButtonVariant variant;
  final AppButtonSize size;

  /// Fill the available width (dialogs, forms) instead of hugging the label.
  final bool expand;
  final bool loading;

  /// Overrides the accessible name — only needed when [label] is an ellipsised
  /// abbreviation.
  final String? semanticLabel;

  bool get _enabled => onPressed != null && !loading;

  @override
  Widget build(BuildContext context) {
    final palette = AppColors.of(context);
    final height = size == AppButtonSize.md ? 48.0 : 36.0;
    final textStyle = TextStyle(
      fontSize: size == AppButtonSize.md ? AppType.md : AppType.sm,
      fontWeight: FontWeight.w600,
    );

    final child = _Label(
      label: label,
      icon: icon,
      loading: loading,
      style: textStyle,
    );

    final ButtonStyle style = switch (variant) {
      AppButtonVariant.primary => FilledButton.styleFrom(
        backgroundColor: palette.primary,
        foregroundColor: palette.onPrimary,
        minimumSize: Size(0, height),
        padding: const EdgeInsets.symmetric(horizontal: AppSpacing.lg),
        shape: RoundedRectangleBorder(borderRadius: AppRadius.allMd),
        textStyle: textStyle,
      ),
      AppButtonVariant.secondary => OutlinedButton.styleFrom(
        foregroundColor: palette.text,
        minimumSize: Size(0, height),
        side: BorderSide(color: palette.border),
        padding: const EdgeInsets.symmetric(horizontal: AppSpacing.lg),
        shape: RoundedRectangleBorder(borderRadius: AppRadius.allMd),
        textStyle: textStyle,
      ),
      AppButtonVariant.ghost => TextButton.styleFrom(
        foregroundColor: palette.text,
        minimumSize: Size(0, height),
        padding: const EdgeInsets.symmetric(horizontal: AppSpacing.md),
        shape: RoundedRectangleBorder(borderRadius: AppRadius.allMd),
        textStyle: textStyle,
      ),
      AppButtonVariant.link => TextButton.styleFrom(
        foregroundColor: palette.primary,
        minimumSize: Size(0, height),
        padding: const EdgeInsets.symmetric(horizontal: AppSpacing.sm),
        textStyle: textStyle,
      ),
      AppButtonVariant.danger => FilledButton.styleFrom(
        backgroundColor: palette.danger,
        foregroundColor: palette.onPrimary,
        minimumSize: Size(0, height),
        padding: const EdgeInsets.symmetric(horizontal: AppSpacing.lg),
        shape: RoundedRectangleBorder(borderRadius: AppRadius.allMd),
        textStyle: textStyle,
      ),
    };

    final button = switch (variant) {
      AppButtonVariant.primary || AppButtonVariant.danger => FilledButton(
        style: style,
        onPressed: _enabled ? onPressed : null,
        child: child,
      ),
      AppButtonVariant.secondary => OutlinedButton(
        style: style,
        onPressed: _enabled ? onPressed : null,
        child: child,
      ),
      AppButtonVariant.ghost || AppButtonVariant.link => TextButton(
        style: style,
        onPressed: _enabled ? onPressed : null,
        child: child,
      ),
    };

    // `sm` keeps the 36 dp body but never a smaller-than-48 dp touch area.
    Widget target = size == AppButtonSize.sm
        ? ConstrainedBox(
            constraints: const BoxConstraints(
              minWidth: AppBreakpoints.touchTarget,
              minHeight: AppBreakpoints.touchTarget,
            ),
            child: Center(widthFactor: 1, heightFactor: 1, child: button),
          )
        : button;

    if (expand) {
      target = SizedBox(width: double.infinity, child: target);
    }

    return Semantics(
      button: true,
      enabled: _enabled,
      label: semanticLabel ?? label,
      excludeSemantics: semanticLabel != null,
      child: target,
    );
  }
}

class _Label extends StatelessWidget {
  const _Label({
    required this.label,
    required this.icon,
    required this.loading,
    required this.style,
  });

  final String label;
  final IconData? icon;
  final bool loading;
  final TextStyle style;

  @override
  Widget build(BuildContext context) {
    final row = Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        if (icon != null) ...[
          Icon(icon, size: style.fontSize! + 4),
          const SizedBox(width: AppSpacing.sm),
        ],
        Flexible(
          child: Text(
            label,
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: style,
          ),
        ),
      ],
    );

    if (!loading) return row;

    // Width is locked by the invisible label; the spinner sits on top of it.
    return Stack(
      alignment: Alignment.center,
      children: [
        Opacity(opacity: 0, child: row),
        SizedBox(
          width: 18,
          height: 18,
          child: CircularProgressIndicator(
            strokeWidth: 2,
            color: Theme.of(context).colorScheme.onPrimary,
          ),
        ),
      ],
    );
  }
}

/// Icon-only control with a guaranteed 48 dp target and a name for screen
/// readers: the tooltip doubles as the semantic label, so there is exactly one
/// string to translate.
class AppIconButton extends StatelessWidget {
  const AppIconButton({
    super.key,
    required this.icon,
    required this.tooltip,
    required this.onPressed,
    this.color,
    this.size = 22,
  });

  final IconData icon;

  /// Localized label, from the catalog. Also the tooltip.
  final String tooltip;
  final VoidCallback? onPressed;
  final Color? color;
  final double size;

  @override
  Widget build(BuildContext context) {
    return Semantics(
      button: true,
      enabled: onPressed != null,
      label: tooltip,
      child: Tooltip(
        message: tooltip,
        // The IconButton would otherwise contribute its own button node with
        // an empty label — a screen reader would announce "button" twice, once
        // unnamed. The Semantics above is the single labelled node.
        child: ExcludeSemantics(
          child: IconButton(
            icon: Icon(icon, size: size),
            color: color,
            tooltip: null,
            constraints: const BoxConstraints(
              minWidth: AppBreakpoints.touchTarget,
              minHeight: AppBreakpoints.touchTarget,
            ),
            onPressed: onPressed,
          ),
        ),
      ),
    );
  }
}
