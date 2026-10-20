/// Card primitive: `surface2`, radius `md`, elevation 1, 16 dp padding, an
/// optional header row with one title and one action.
///
/// Screens use this instead of `Card` + hand-rolled `Padding`/`BoxDecoration`,
/// which is how the app grew three different card paddings and four border
/// colours. Colours come from [AppPalette] by role, so the same card is correct
/// in the light theme without a second code path.
library;

import 'package:flutter/material.dart';

import '../core/design_tokens.dart';

class AppCard extends StatelessWidget {
  const AppCard({
    super.key,
    required this.child,
    this.title,
    this.subtitle,
    this.icon,
    this.trailing,
    this.padding = const EdgeInsets.all(AppSpacing.lg),
    this.onTap,
    this.accent,
    this.elevation = 1,
    this.semanticLabel,
  });

  final Widget child;

  /// Header title. When null the card has no header row at all.
  final String? title;
  final String? subtitle;
  final IconData? icon;

  /// The single header action (an icon button, a badge, a menu).
  final Widget? trailing;

  final EdgeInsets padding;
  final VoidCallback? onTap;

  /// Overrides the border colour — used for the "enabled" rule accent.
  final Color? accent;

  /// Elevation step from the ladder (1 = card, 2 = popover, 3 = modal).
  final int elevation;

  /// Announced by screen readers when the card is a single tappable unit.
  final String? semanticLabel;

  @override
  Widget build(BuildContext context) {
    final palette = AppColors.of(context);
    final theme = Theme.of(context);
    final border = accent ?? palette.border;

    Widget content = Padding(padding: padding, child: _content(context, theme));

    if (onTap != null) {
      content = Material(
        type: MaterialType.transparency,
        child: InkWell(
          onTap: onTap,
          borderRadius: AppRadius.allMd,
          child: content,
        ),
      );
    }

    final card = Container(
      decoration: BoxDecoration(
        color: palette.surface2,
        borderRadius: AppRadius.allMd,
        border: Border.all(color: border),
        boxShadow: AppElevation.shadows(context, level: elevation),
      ),
      clipBehavior: Clip.antiAlias,
      child: content,
    );

    if (semanticLabel == null) return card;
    return Semantics(
      container: true,
      label: semanticLabel,
      button: onTap != null,
      child: card,
    );
  }

  Widget _content(BuildContext context, ThemeData theme) {
    if (title == null) return child;
    final palette = AppColors.of(context);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            if (icon != null) ...[
              Icon(icon, size: 18, color: palette.primary),
              const SizedBox(width: AppSpacing.sm),
            ],
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(
                    title!,
                    // Two lines: a translated title that wraps is readable,
                    // one that is ellipsised mid-word is not.
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    style: theme.textTheme.titleMedium,
                  ),
                  if (subtitle != null && subtitle!.isNotEmpty)
                    Padding(
                      padding: const EdgeInsets.only(top: 2),
                      child: Text(
                        subtitle!,
                        maxLines: 3,
                        style: theme.textTheme.bodySmall,
                      ),
                    ),
                ],
              ),
            ),
            ?trailing,
          ],
        ),
        const SizedBox(height: AppSpacing.md),
        child,
      ],
    );
  }
}
